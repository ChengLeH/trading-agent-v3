"""Durable ownership and bounded wire batches over the existing Native queue.

The lease coordinates work, never certifies a verdict. Each accepted row still
passes the ordinary collector and keeps the actual shared model response proof.
"""
from copy import deepcopy
import time
from uuid import uuid4
from types import SimpleNamespace

from .agent_queue import claim_items_in_state, submit_items_in_state
from .journal import RunJournal
from .review_batches import pack_review_requests, unpack_prepared_review_result
from .review_budget import review_budget_for, review_wire_breakdown
from .schema import digest


def leased_review_ids(state, layer):
    now = time.time()
    return {cid for packet in state["packets"].values()
            if packet.get("kind") == layer and not packet.get("retired")
            and packet.get("input_hash") == state["input_hash"]
            for cid, lease in packet.get("item_leases", {}).items()
            if lease["expires_at"] > now and cid not in packet["received"]}


def review_reservations(state, layer):
    """Describe reservations without claiming that an executor has started."""
    now = time.time()
    groups = {}
    for packet in state['packets'].values():
        if (packet.get('kind') != layer or packet.get('retired')
                or packet.get('input_hash') != state['input_hash']):
            continue
        if layer == 'global' and not packet.get('parallel_dependency'):
            continue
        for cid, lease in packet.get('item_leases', {}).items():
            if lease['expires_at'] <= now or cid in packet.get('received', {}):
                continue
            key = (packet['packet_id'], lease['lease_id'], lease['owner'], lease['expires_at'])
            row = groups.setdefault(key, dict(item_ids=[], claim_ids=[]))
            row['item_ids'].append(cid)
            row['claim_ids'].extend(packet.get('global_context', {}).get('target_claim_ids', [])
                                    if layer == 'global' else [cid])
    return [dict(packet_id=packet_id, lease_id=lease_id, owner=owner,
                 expires_at=expires_at, claim_ids=sorted(set(group['claim_ids'])),
                 execution_state='unknown',
                 claimed_at=state['packets'][packet_id]['item_leases'][group['item_ids'][0]].get('claimed_at'))
            for (packet_id, lease_id, owner, expires_at), group in sorted(groups.items())]


def retire_unowned_review_packets(state):
    """Cold-store superseded packets without dropping live work/candidates."""
    now = time.time()
    processed = state.get("processed_items", {})
    for packet in state["packets"].values():
        if (packet.get("kind") not in {"atomic", "deep"} or packet.get("retired")
                or packet["packet_id"] == state.get("active_packet_id")):
            continue
        if any(lease["expires_at"] > now for lease in packet.get("item_leases", {}).values()):
            continue
        if any(cid not in packet.get("processed_item_ids", {}) and processed.get(cid) != packet["packet_id"]
               for cid in packet["received"]):
            continue
        packet.update(retired=True, retirement_reason="review_lease_expired_or_released")


class NativeScheduling:
    def _handoff_unavailable_supplements(self, executor, *, takeover_incomplete=False):
        """Restart only incomplete work pinned to a disabled reviewer.

        Never combine a new reviewer's verdict with another reviewer's partial
        checks. Their raw attempts remain archived and the transfer is explicit.
        """
        with self.queue.transaction() as state:
            unavailable = {model for model, row in state.get("secondary_availability", {}).items()
                           if row.get("status") == "unavailable" and model != executor}
            saved = state.get("checkpoint", {}).get("reference_supplements", {})
            if not any(row.get("model") != executor and (takeover_incomplete or row.get("model") in unavailable)
                       for rows in saved.values() for row in rows.values()):
                return
            runner = self._load(state)
            changed = False
            for layer in ("atomic", "deep"):
                held = leased_review_ids(state, layer)
                completed = runner.receipts if layer == "atomic" else runner.deep_receipts
                for cid, partial in list(runner.reference_supplements[layer].items()):
                    previous = partial.get("model")
                    if (previous == executor or (previous not in unavailable and not takeover_incomplete)
                            or cid in held or cid in completed):
                        continue
                    state.setdefault("review_handoffs", []).append(dict(
                        claim_id=cid, layer=layer, previous_executor=previous, executor=executor,
                        reason=("secondary_unavailable" if previous in unavailable else "explicit_reviewer_takeover")
                               + "; complete fresh Claim review required",
                        prior_supplement_hash=digest(partial),
                        prior_sources=deepcopy(partial.get("reference_sources", [])),
                        verification_epoch=runner._verification_epoch))
                    del runner.reference_supplements[layer][cid]
                    runner._latest_review_sources[layer].pop(cid, None)
                    for key in ("review_errors", "review_blocked"):
                        error = state.get(key, {}).get(cid)
                        if error and error.get("executor") == previous:
                            state[key].pop(cid)
                    changed = True
            if changed:
                # Old attempt counters are retained under the old reviewer;
                # the new reviewer starts its own full review, not a retry reset.
                runner.status = "atomic" if len(runner.receipts) < len(runner.run.claims) else "deep"
                self._save(state, runner)

    def claim_reviews(self, executor, *, max_items=4, batch_size=4, lease_seconds=3600, exclude_claim_ids=(), takeover_incomplete=False, single_request=False):
        if not isinstance(executor, str) or not executor.strip():
            raise ValueError("executor identity required")
        if type(max_items) is not int or not 1 <= max_items <= 16:
            raise ValueError("max_items must be 1..16")
        if type(batch_size) is not int or not 1 <= batch_size <= 16:
            raise ValueError("batch_size must be 1..16")
        if type(takeover_incomplete) is not bool:
            raise ValueError("takeover_incomplete must be boolean")
        if type(single_request) is not bool:
            raise ValueError("single_request must be boolean")
        self._handoff_unavailable_supplements(executor, takeover_incomplete=takeover_incomplete)
        pending = self.next()
        if pending.get("kind") not in {"atomic", "deep"} or pending.get("blocked") or "packet_id" not in pending:
            return pending
        with self.queue.transaction() as state:
            packet = state["packets"][pending["packet_id"]]
            held = leased_review_ids(state, packet["kind"])
            selected = [cid for cid in pending["items"]
                        if cid not in held and cid not in packet["received"] and cid not in exclude_claim_ids][:max_items]
            if not selected:
                return dict(status="reviews_in_flight", kind=packet["kind"], items={})
            runner = self._load(state)
            tasks = runner._tasks()
            # Recheck the snapshot under the same lock that reserves it.
            if packet["input_hash"] != runner._hash():
                raise ValueError("stale review packet")
            layer = packet["kind"]
            requests = []
            regular = []
            for cid in selected:
                item = packet["items"][cid]
                if item["review_context"] != runner.review_context_for(layer, [cid], tasks=tasks):
                    raise ValueError("stale review context")
                if cid in runner.reference_supplements[layer]:
                    requests.append(dict(request_id=uuid4().hex, claim_ids=[cid],
                        prompt=item["prompt"], response_schema=deepcopy(item["schema"])))
                else:
                    regular.append(tasks[cid])
            first = ({task.claim_id: runner.receipts[task.claim_id]["result"] for task in regular}
                     if layer == "deep" else {})
            cap = min(self._options(state)[1], review_budget_for(runner, layer))
            if batch_size == 1:
                for task in regular:
                    item = packet["items"][task.claim_id]
                    requests.append(dict(request_id=uuid4().hex, claim_ids=[task.claim_id],
                        prompt=item["prompt"], response_schema=deepcopy(item["schema"])))
                regular = []
            for request in pack_review_requests(regular, batch_size=batch_size, max_bytes=cap,
                    verification_epoch=runner._verification_epoch, layer=layer, first_reviews=first):
                if review_wire_breakdown(request["prompt"], request["response_schema"])["wire_bytes"] > cap:
                    # A packer preserves oversized singles; Native enforces the
                    # declared delivery budget by using its validated single.
                    for cid in request["claim_ids"]:
                        item = packet["items"][cid]
                        requests.append(dict(request_id=uuid4().hex, claim_ids=[cid],
                            prompt=item["prompt"], response_schema=deepcopy(item["schema"])))
                else:
                    requests.append(request)
            if single_request:
                # A worker owns exactly the items it will start. Renewing a
                # running request must not extend an unrelated queued request.
                if not requests:
                    raise ValueError('no bounded review request could be prepared')
                requests = requests[:1]
                selected = list(requests[0]['claim_ids'])
            lease = claim_items_in_state(state, packet["packet_id"], executor, selected, lease_seconds)
            stored = packet.setdefault("wire_requests", {})
            for request in requests:
                request["request_id"] = uuid4().hex
                request["review_context"] = runner.review_context_for(layer, request["claim_ids"], tasks=tasks)
                request["lease_id"] = lease["lease_id"]
                stored[request["request_id"]] = deepcopy(request)
            return dict(status="reviews_claimed", kind=layer, packet_id=packet["packet_id"],
                        input_hash=packet["input_hash"], lease=lease, requests=requests)

    def submit_review_request(self, packet_id, request_id, response, executor, input_hash):
        with self.queue.transaction() as state:
            packet = state["packets"].get(packet_id)
            if not packet or request_id not in packet.get("wire_requests", {}):
                raise ValueError("unknown wire review request")
            request = packet["wire_requests"][request_id]
            if state["input_hash"] != input_hash or packet["input_hash"] != input_hash:
                raise ValueError("stale wire review input version")
            saved = packet.get("wire_outcomes", {}).get(request_id)
            if saved is not None:
                if saved["executor"] != executor or saved["response_hash"] != digest(response):
                    raise ValueError("conflicting duplicate wire response")
                runner = self._load(state)
                tasks = runner._tasks()
                current = runner.review_context_for(packet["kind"], request["claim_ids"], tasks=tasks)
                # Completed supplements no longer have pending parent state;
                # their immutable archive/composition proofs remain mandatory.
                bound = {k: v for k, v in request["review_context"].items()
                         if k != "reference_parent_sources"}
                if bound != {k: v for k, v in current.items() if k != "reference_parent_sources"}:
                    raise ValueError("stale wire review request")
                proof = RunJournal(self.directory).response_provenance(runner,
                    SimpleNamespace(identity=executor), f"{packet['kind']}.batch.{request_id}",
                    request["prompt"], response, response_schema=request["response_schema"],
                    review_context=request["review_context"])
                if proof is None:
                    raise ValueError("accepted wire response has no verified raw archive")
                return {**deepcopy(saved["outcome"]), "idempotent": True}
            for cid in request["claim_ids"]:
                lease = packet.get("item_leases", {}).get(cid)
                if (not lease or lease["lease_id"] != request["lease_id"]
                        or lease["owner"] != executor or lease["expires_at"] <= time.time()):
                    raise ValueError("wire request lease no longer owns this Claim")
            runner = self._load(state)
            tasks = runner._tasks()
            layer = packet["kind"]
            if request["review_context"] != runner.review_context_for(layer, request["claim_ids"], tasks=tasks):
                raise ValueError("stale wire review request")
            proof = RunJournal(self.directory).archive_external_submission(runner,
                model=executor, task_id=f"{layer}.batch.{request_id}", prompt=request["prompt"],
                response=deepcopy(response), response_schema=request["response_schema"],
                review_context=request["review_context"])
            rows = unpack_prepared_review_result(request, response, preserve_unambiguous=True)
            if not rows:
                raise ValueError("wire response contains no unambiguous Claim review; raw response archived, no verdict invented")
            submit_items_in_state(state, packet_id, rows, executor, input_hash)
            for cid in rows:
                packet.setdefault("batch_sources", {})[cid] = deepcopy(proof)
            # Release omitted rows only; accepted siblings and their exact raw
            # batch proof are durable even when the model skipped another row.
            for cid in request["claim_ids"]:
                lease = packet.get("item_leases", {}).get(cid)
                if cid not in rows and lease and lease["lease_id"] == request["lease_id"]:
                    del packet["item_leases"][cid]
            outcome = self._ingest_reviews(state, runner, packet)
            self._save(state, runner)
            returned_rows = (response["results"] if "results" in request["response_schema"].get("properties", {})
                             else [response])
            outcome["unaccepted_response_row_count"] = max(0, len(returned_rows) - len(rows))
            outcome["returned_claim_ids"] = sorted(rows)
            outcome["omitted_claim_ids"] = sorted(set(request["claim_ids"]) - set(rows))
            packet.setdefault("wire_outcomes", {})[request_id] = dict(executor=executor,
                response_hash=digest(response), outcome=deepcopy(outcome))
            return outcome
