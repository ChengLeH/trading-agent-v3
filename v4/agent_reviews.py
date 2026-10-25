"""Native atomic/deep/global semantic review exchange on top of the role workflow.

One coarse packet covers a review GROUP (default 8 Claims), never one spawned
agent per Claim. Submitted rows are candidates: each row is validated against
the current task hash and archived with its ACTUAL declared executor identity
before the real collector normalizes it. Partial groups keep accepted
siblings; invalid rows stay explicit outstanding work. Global P6 requests use one durable JSON packet at a time. Accepted answers are
replayed from matching raw response archives; the real runner alone decides
revision/finalization gates. No model/provider or tool is started in this unit.
"""
import json
import time
from copy import deepcopy
from uuid import uuid4
from types import SimpleNamespace

from .agent_workflow import NativeWorkflow, json_read, _require_identity
from .agent_queue import submit_items_in_state
from .native_scheduling import NativeScheduling, leased_review_ids, retire_unowned_review_packets
from .native_dependency_batches import NativeDependencyBatchMixin, dependency_identity, collect_dependency_packet
from .journal import RunJournal
from .schema import digest
from .review_budget import review_budget_for, review_budget_row, review_wire_breakdown

DEFAULT_REVIEW_GROUP_SIZE = 8
DEFAULT_REVIEW_PACKET_MAX_BYTES = 262144
REVIEW_KINDS = {"atomic": "atomic", "deep": "deep"}
PATCH_ROLE = "claim_patcher"
LINK_ROLE = "finding_linker"
PATCH_WAIT_STATUSES = {"blocked:pending_revision", "blocked:revision_limit",
                       "blocked:revision_no_changes"}


def _item_wire_bytes(prompt, schema):
    return review_wire_breakdown(prompt, schema)["wire_bytes"]


def configure_review_budget(workflow, *, max_item_bytes=None, max_packet_bytes=None, reason):
    """Change delivery limits without altering claims or accepted proof archives."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('operator reason required')
    changes = {k: v for k, v in dict(max_item_bytes=max_item_bytes, max_packet_bytes=max_packet_bytes).items() if v is not None}
    if not changes or any(type(v) is not int or v < 1024 for v in changes.values()):
        raise ValueError('provide at least one review byte budget >=1024')
    with workflow.queue.transaction() as state:
        runner = workflow._load(state)
        before = dict(max_item_bytes=runner.review_batch_max_bytes,
                      max_deep_item_bytes=runner.review_deep_batch_max_bytes,
                      max_packet_bytes=workflow._options(state)[1])
        if max_item_bytes is not None:
            runner.review_batch_max_bytes = max_item_bytes
            runner.review_deep_batch_max_bytes = max_item_bytes
        if max_packet_bytes is not None:
            state.setdefault('options', {})['review_packet_max_bytes'] = max_packet_bytes
        for packet in state.get('packets', {}).values():
            if packet.get('kind') in REVIEW_KINDS and not packet.get('retired'):
                packet.update(retired=True, retirement_reason='review_budget_changed')
                if state.get('active_packet_id') == packet['packet_id']:
                    state['active_packet_id'] = None
        after = dict(max_item_bytes=runner.review_batch_max_bytes,
                     max_deep_item_bytes=runner.review_deep_batch_max_bytes,
                     max_packet_bytes=workflow._options(state)[1])
        event = dict(before=before, after=after, reason=reason.strip())
        state.setdefault('review_budget_changes', []).append(event)
        workflow._save(state, runner)
        return dict(status='review_budget_updated', **event)


class NativeReviewWorkflow(NativeDependencyBatchMixin, NativeScheduling, NativeWorkflow):
    def _options(self, state):
        options = state.get("options", {})
        size = options.get("review_group_size", DEFAULT_REVIEW_GROUP_SIZE)
        cap = options.get("review_packet_max_bytes", DEFAULT_REVIEW_PACKET_MAX_BYTES)
        if type(size) is not int or not 1 <= size <= 16:
            raise ValueError("review_group_size must be 1..16")
        if type(cap) is not int or cap < 1024:
            raise ValueError("review_packet_max_bytes must be >=1024")
        return size, cap

    def _current_layer(self, runner, tasks):
        """Return (layer, pending_ids, prepared) for the real runner state."""
        missing = [cid for cid in tasks if cid not in runner.receipts]
        if missing:
            prepared = runner._prepare_phase6_atomic(tasks, enforce_budget=False)
            prompts = dict(prepared["atomic_prompts"])
            return "atomic", list(prompts), prompts, prepared["response_schemas"]
        deep_wanted = [cid for cid in tasks if runner.receipts[cid]["result"]["verdict"] != "PASS"
                       and cid not in runner.deep_receipts]
        if deep_wanted:
            prepared = runner._prepare_phase6_deep_review(tasks, deep_wanted, enforce_budget=False)
            prompts = dict(prepared["deep_prompts"])
            return "deep", list(prompts), prompts, prepared["response_schemas"]
        return None, [], {}, {}

    def _issue_review_packet(self, state, runner, layer, prompts, schemas, size, cap, tasks):
        items, used_bytes, oversized = {}, 0, []
        for cid in prompts:
            if len(items) >= size:
                break
            context = runner.review_context_for(layer, [cid], tasks=tasks)
            item = dict(prompt=prompts[cid], schema=deepcopy(schemas[cid]), review_context=context)
            if cid not in items:
                budget = min(cap, review_budget_for(runner, layer))
                budget_row = review_budget_row(cid, item["prompt"], item["schema"], budget)
                if budget_row is not None:
                    oversized.append(budget_row)
                    continue
                item_bytes = _item_wire_bytes(item["prompt"], item["schema"])
                if items and used_bytes + item_bytes > cap:
                    break
                items[cid] = item
                used_bytes += item_bytes
        if oversized and not items:
            diagnostic_block = any(row["reason"] == "related_diagnostics_incomplete" for row in oversized)
            return dict(blocked=True, status="blocked:review_context_budget", layer=layer,
                        reason=("related_diagnostics_incomplete: omitted related diagnostics require a narrower "
                                "structured ref set or a relevant diagnostics-cap change; increasing the general "
                                "item budget cannot restore omitted diagnostics"
                                if diagnostic_block else
                                f"review_context_too_large: {len(oversized)} items exceed the declared context budget; "
                                f"budget={cap}; no evidence or IDs were truncated"),
                        oversized_items=oversized,
                        next_action=("reduce_structured_refs_or_adjust_related_diagnostics_cap"
                                     if diagnostic_block else
                                     "configure_reviews_or_reduce_structured_context"))
        packet_id = uuid4().hex
        state["packets"][packet_id] = dict(packet_id=packet_id, kind=layer,
                                           input_hash=state["input_hash"], items=items, received={})
        state["active_packet_id"] = packet_id
        response = dict(status="reviews", packet_id=packet_id, kind=layer,
                        input_hash=state["input_hash"], items=deepcopy(items), oversized_items=oversized)
        if oversized:
            diagnostic_block = any(row["reason"] == "related_diagnostics_incomplete" for row in oversized)
            response["next_action"] = ("reduce_structured_refs_or_adjust_related_diagnostics_cap"
                                       if diagnostic_block else
                                       "configure_reviews_or_reduce_structured_context")
            if diagnostic_block:
                response["reason"] = ("related_diagnostics_incomplete: omitted related diagnostics require a narrower "
                                       "structured ref set or a relevant diagnostics-cap change; increasing the general "
                                       "item budget cannot restore omitted diagnostics")
        return response

    def next(self):
        result = self._next()
        result['delivery_ready'] = result.get('status') == 'complete' and not result.get('blocked')
        result['verification_status'] = 'complete' if result['delivery_ready'] else 'incomplete'
        disclosure = self.execution_disclosure()
        events = disclosure['secondary_events']
        if events:
            result['secondary_execution'] = dict(event_count=len(events), latest_event=deepcopy(events[-1]),
                                                independent_review_guaranteed=False)
            if any(event['code'] != 'secondary_completed' for event in events):
                result.setdefault('warnings', []).append(dict(code='secondary_review_degraded',
                    next_action='inspect_execution_disclosure_and_current_review_executors',
                    message='Secondary review was unavailable or incomplete during this run; independent review is not guaranteed.'))
        return result

    def _next(self):
        role_state = super().next()
        if role_state.get("status") not in PATCH_WAIT_STATUSES:
            if role_state.get("status") != "phase6_ready":
                # A coarse role/evidence stage must not hide a durable repair
                # already bound to the current canonical state.
                with self.queue.transaction() as state:
                    runner = self._load(state)
                    pending = runner._pending_revision_block()
                    if pending:
                        return self._next_patch_packet(state, runner) or pending
                return role_state
        elif not self._has_pending_patch():
            with self.queue.transaction() as state:
                runner = self._load(state)
                if runner.pending_patch is None:
                    return role_state
                return self._next_patch_packet(state, runner) or role_state
        with self.queue.transaction() as state:
            runner = self._load(state)
            pending = runner._pending_revision_block()
            if pending:
                return self._next_patch_packet(state, runner) or pending
            retire_unowned_review_packets(state)
            active = state["packets"].get(state.get("active_packet_id"))
            processed = state.setdefault("processed_items", {})
            if (active and active.get("kind") in REVIEW_KINDS and not active.get("retired")
                    and any(cid not in active.get("processed_item_ids", {}) and processed.get(cid) != active["packet_id"]
                            for cid in active["received"])):
                outcome = self._ingest_reviews(state, runner, active)
                self._save(state, runner)
                if outcome.get("blocked") and not outcome.get("needs_retry"):
                    return outcome
                active = state["packets"].get(state.get("active_packet_id"))
            if state.get("review_blocked"):
                return dict(status="blocked:review_contract", blocked=True,
                            blocked_claims=deepcopy(state["review_blocked"]))
            tasks = runner._tasks()
            layer, order, prompts, schemas = self._current_layer(runner, tasks)
            if layer is None:
                return self._next_global(state, runner, active)
            size, cap = self._options(state)
            held = leased_review_ids(state, layer)
            if active and active.get("kind") in REVIEW_KINDS and not active.get("retired"):
                outstanding = {cid: deepcopy(item) for cid, item in active["items"].items()
                               if cid not in active["received"]}
                current = (active["kind"] == layer and active["input_hash"] == state["input_hash"]
                           and all(cid in prompts and item["prompt"] == prompts[cid]
                                   and item["schema"] == schemas[cid]
                                   and item["review_context"] == runner.review_context_for(layer, [cid], tasks=tasks)
                                   for cid, item in outstanding.items()))
                available = {cid: item for cid, item in outstanding.items() if cid not in held}
                if available and current:
                    return dict(status="reviews", packet_id=active["packet_id"], kind=layer,
                                input_hash=state["input_hash"], items=available)
                if not any(lease["expires_at"] > time.time()
                           for lease in active.get("item_leases", {}).values()):
                    active["retired"] = True
                    state["active_packet_id"] = None
            prompts = {cid: prompt for cid, prompt in prompts.items() if cid not in held}
            if not prompts and layer == "atomic":
                ready = [cid for cid in tasks if cid in runner.receipts
                         and runner.receipts[cid]["result"]["verdict"] != "PASS"
                         and cid not in runner.deep_receipts and cid not in leased_review_ids(state, "deep")]
                if ready:
                    prepared = runner._prepare_phase6_deep_review(tasks, ready, enforce_budget=False)
                    layer, prompts, schemas = "deep", dict(prepared["deep_prompts"]), prepared["response_schemas"]
            if not prompts:
                return dict(status="reviews_in_flight", kind=layer, items={},
                            reason="ready work is reserved; renew or await its owner, or reclaim after lease expiry")
            return self._issue_review_packet(state, runner, layer, prompts, schemas, size, cap, tasks)

    @staticmethod
    def _global_identity(runner):
        pending = runner.pending_global
        if not isinstance(pending, dict):
            raise ValueError("global runner request is not prepared")
        # Bind each request to the canonical claims, history, report, epoch,
        # target IDs and coverage round. Text formatting is not a state key.
        return dict(input_hash=runner._hash(),
                    history_hash=pending["history_hash"],
                    report_hash=pending["report_hash"],
                    verification_epoch=runner._verification_epoch,
                    coverage_round=pending["coverage_round"],
                    target_claim_ids=deepcopy(pending["target_claim_ids"]),
                    resolution_completion_hash=(digest(pending["resolution_completion"])
                                                if pending.get("resolution_completion") else None))

    @staticmethod
    def _prepare_global_packet(runner, packet):
        if packet.get("parallel_dependency"):
            return runner.prepare_phase6_global_sweep(
                dependency_claim_ids=packet["global_context"]["target_claim_ids"])
        return runner.prepare_phase6_global_sweep()

    @staticmethod
    def _collect_global_packet(runner, packet, row):
        if packet.get("parallel_dependency"):
            return collect_dependency_packet(runner, packet, row)
        return runner.collect_phase6_global_sweep(deepcopy(row["response"]), model_identity=row["executor"])

    def _matches_global(self, runner, packet, prepared):
        if packet.get("parallel_dependency"):
            return (isinstance(prepared, tuple) and prepared[0] == "global_dependency_completion"
                    and packet["global_context"] == dependency_identity(
                        runner, packet["global_context"]["target_claim_ids"]))
        role = next(iter(packet["items"]))
        return (isinstance(prepared, tuple) and len(prepared) == 2
                and prepared[0] == role and packet["input_hash"] == runner._hash()
                and packet.get("global_context") == self._global_identity(runner))

    def _load(self, state):
        """Rebuild global receipts from the exact archived responses, never audit hints.

        Standard checkpoints intentionally do not trust serialized global
        receipts. Native queue owns the candidate records: each processed
        packet must be proved against the original raw archive then replayed
        through the real P6 collector.
        """
        runner = super()._load(state)
        # Reconstruct historical requests on an isolated runner, before the
        # current repair was opened. Never weaken the live pending-patch guard
        # or remove the durable repair request from authoritative queue state.
        pending_patch = deepcopy(runner.pending_patch)
        pending_status = runner.status
        runner = deepcopy(runner)
        runner.pending_patch = None
        journal = RunJournal(self.directory)
        processed = state.get("processed_items", {})
        current_hash = runner._hash()

        def current_epoch(packet):
            context = packet.get("global_context")
            return (isinstance(context, dict)
                    and context.get("verification_epoch") == runner._verification_epoch)

        def raw_global_proof(packet):
            role = next(iter(packet["items"]))
            row = packet.get("received", {}).get(role)
            if not isinstance(row, dict) or not {"executor", "response"} <= row.keys():
                return None
            return journal.response_provenance(
                runner, SimpleNamespace(identity=row["executor"]), role,
                packet["items"][role]["prompt"], row["response"])

        needs_global_replay = any(p.get('kind') == 'global' and p.get('input_hash') == current_hash
            and current_epoch(p)
            and (p['packet_id'] in state.get('global_outcomes', {}) or not p.get('retired'))
            for p in state.get('packets', {}).values())
        deterministic_blocked = False
        if needs_global_replay:
            _, deterministic_blocked = runner.orch.validate_stage(runner.run_id, 'final')
        restored_final_global = None
        current_report_hash = None
        if (not pending_patch and not deterministic_blocked
                and not runner.deferred_global_findings):
            has_open_global = any(packet.get("kind") == "global" and not packet.get("retired")
                                  and packet.get("input_hash") == current_hash and current_epoch(packet)
                                  for packet in state.get("packets", {}).values())
            if not has_open_global:
                for packet in reversed(list(state.get("packets", {}).values())):
                    stored = state.get("global_outcomes", {}).get(packet.get("packet_id"))
                    snapshot = state.get("global_receipt_snapshots", {}).get(packet.get("packet_id"))
                    if (packet.get("kind") != "global" or packet.get("input_hash") != current_hash
                            or not current_epoch(packet)
                            or not isinstance(stored, dict) or stored.get("status") != "verified"
                            or not isinstance(snapshot, dict) or snapshot.get("input_hash") != current_hash
                            or snapshot.get("findings") != []
                            or not snapshot.get("resolved_external_findings")):
                        continue
                    from .reporting import render_report
                    from .runner import require_model_identity
                    if current_report_hash is None:
                        current_report_hash = render_report(
                            runner.orch.snapshot(runner.run_id))["sidecar"]["report_hash"]
                    if (snapshot.get("report_hash") != current_report_hash
                            or snapshot.get("history_hash") != digest(runner.history)):
                        continue
                    require_model_identity(snapshot.get("model"))
                    proofs_complete = True
                    for candidate in state.get("packets", {}).values():
                        outcome = state.get("global_outcomes", {}).get(candidate.get("packet_id"))
                        if (candidate.get("kind") != "global" or candidate.get("input_hash") != current_hash
                                or not current_epoch(candidate)
                                or not isinstance(outcome, dict)
                                or outcome.get("status") == "blocked:global_contract"):
                            continue
                        if raw_global_proof(candidate) is None:
                            proofs_complete = False
                            break
                    if not proofs_complete:
                        continue
                    runner.global_receipt = deepcopy(snapshot)
                    runner.pending_global = None
                    runner.global_review_progress = None
                    runner.status = "verified"
                    restored_final_global = packet["packet_id"]
                    break
        acceptance_order = state.get("global_acceptance_order", [])
        if (not isinstance(acceptance_order, list) or len(set(acceptance_order)) != len(acceptance_order)
                or any(pid not in state["packets"] for pid in acceptance_order)):
            raise ValueError("invalid global acceptance order")
        acceptance_index = {pid: idx for idx, pid in enumerate(acceptance_order)}
        # Pre-upgrade accepted packets precede the explicit new acceptance log.
        # Within parallel chunks, submission order (not issue order) is replayed.
        packets = list(state.get("packets", {}).values())
        packets.sort(key=lambda p: ((1, acceptance_index[p["packet_id"]])
                     if p["packet_id"] in acceptance_index else (0, 0)))
        for packet in packets:
            if packet.get("kind") != "global" or not current_epoch(packet):
                continue
            role = next(iter(packet["items"]))
            row = packet.get("received", {}).get(role)
            # The same dependency-completion role may issue multiple packets.
            # A per-role marker identifies only the latest packet; replay all
            # canonically accepted packet IDs in order instead.
            stored = state.get("global_outcomes", {}).get(packet["packet_id"])
            if stored is None or stored.get("status") == "blocked:global_contract":
                continue
            # An accepted outcome always stays bound to its verified raw archive.
            proof = raw_global_proof(packet)
            if proof is None:
                raise ValueError(f"global packet {packet['packet_id']} has no verified raw response archive")
            if packet["input_hash"] != current_hash:
                # A sweep accepted for a superseded graph version is history;
                # the repair collector already re-derived or invalidated its
                # findings against the current canonical state.
                continue
            if deterministic_blocked:
                continue  # old receipts remain audit evidence, never bypass a current invariant
            if restored_final_global is not None:
                continue
            snapshot = state.get("global_receipt_snapshots", {}).get(packet["packet_id"])
            if (stored.get("status") == "revision" and isinstance(snapshot, dict)
                    and snapshot.get("input_hash") == current_hash):
                # Findings were already deferred into canonical state when the
                # sweep was accepted; re-running that collector is not
                # idempotent, so restore the exact finding-bearing receipt.
                runner.global_receipt = deepcopy(snapshot)
                runner.pending_global = None
                runner.global_review_progress = None
                runner.status = 'revision'
                continue
            request = self._prepare_global_packet(runner, packet)
            if not self._matches_global(runner, packet, request):
                raise ValueError("global packet replay request mismatch")
            outcome = self._collect_global_packet(runner, packet, row)
            if digest(outcome) != digest(stored):
                raise ValueError("replayed global outcome differs from accepted candidate")
        runner.pending_patch = pending_patch
        if pending_patch is not None:
            runner.status = pending_status
        active = state.get("packets", {}).get(state.get("active_packet_id"))
        if active and active.get("kind") == "global" and not active.get("retired"):
            if not current_epoch(active):
                active.update(retired=True, retirement_reason='verification_epoch_changed')
                state['active_packet_id'] = None
            elif deterministic_blocked:
                active.update(retired=True, retirement_reason='deterministic_revalidation_required')
                state['active_packet_id'] = None
            elif (runner.global_receipt is not None and runner.global_receipt.get('findings')
                    and runner.pending_global is None):
                # Older versions issued the last completion again after the
                # round had closed. Preserve its candidate/audit, but do not
                # reopen a proven closed round or dispatch it another time.
                active['retired'] = True
                active['retirement_reason'] = 'global_round_already_closed_with_findings'
                state['active_packet_id'] = None
            else:
                role = next(iter(active["items"]))
                if processed.get(role) != active["packet_id"]:
                    request = runner.prepare_phase6_global_sweep()
                    if not self._matches_global(runner, active, request):
                        raise ValueError("pending global packet mismatches canonical request")
        linked = state.get("accepted_links")
        if linked is not None and linked.get("input_hash") == current_hash:
            linked_epoch = linked.get("verification_epoch")
            if linked_epoch is None:
                source_epochs = set()
                for packet_id, snapshot in state.get("global_receipt_snapshots", {}).items():
                    packet = state["packets"].get(packet_id, {})
                    context = packet.get("global_context", {})
                    if not isinstance(snapshot, dict) or not isinstance(context, dict):
                        continue
                    if (snapshot.get("input_hash") == current_hash
                            and digest(snapshot.get("findings")) == linked.get("source_hash")
                            and context.get("verification_epoch")):
                        source_epochs.add(context["verification_epoch"])
                if len(source_epochs) == 1:
                    linked_epoch = next(iter(source_epochs))
            if linked_epoch != runner._verification_epoch:
                state.setdefault("historical_links", []).append(deepcopy(state.pop("accepted_links")))
                deferred = {f["finding_id"]: deepcopy(f) for f in runner.deferred_global_findings}
                for finding in linked["findings"]:
                    deferred.setdefault(finding["finding_id"], deepcopy(finding))
                runner.deferred_global_findings = list(deferred.values())
                self._save(state, runner)
                linked = None
        if linked is not None and linked.get("input_hash") == current_hash:
            packet = state["packets"].get(linked.get("packet_id"))
            if packet is None or packet.get("kind") != "link":
                raise ValueError("accepted linker packet missing")
            role = LINK_ROLE
            row = packet.get("received", {}).get(role)
            if row is None or row.get("executor") != linked["executor"]:
                raise ValueError("accepted linker response missing")
            if row.get("response") != linked["response"]:
                raise ValueError("accepted linker response altered")
            original = deepcopy(runner.global_receipt["findings"])
            if digest(original) != linked.get("source_hash"):
                raise ValueError("accepted linker source findings altered")
            unmapped = [f for f in original if not runner._finding_claim_ids(f)]
            new_role, prompt = runner.prepare_finding_linker(unmapped)
            if new_role != role or prompt != packet["items"][role]["prompt"]:
                raise ValueError("accepted linker request changed")
            if journal.response_provenance(
                    runner, SimpleNamespace(identity=row["executor"]), role,
                    prompt, row["response"]) is None:
                raise ValueError("accepted linker has no verified raw response archive")
            reproduced = runner.apply_finding_links(unmapped, deepcopy(row["response"]))
            by_id = {f["finding_id"]: f for f in reproduced}
            expected = [deepcopy(by_id.get(f.get("finding_id"), f)) for f in original]
            if digest(expected) != digest(linked["findings"]):
                raise ValueError("accepted linker finding mapping was altered")
        return runner

    def _next_global(self, state, runner, active):
        """Advance the existing P6 runner using one durable JSON packet per sweep."""
        deterministic, _ = runner.orch.validate_stage(runner.run_id, 'final')
        source_failures = [deepcopy(f) for f in deterministic if f['status'] == 'FAIL' and not f.get('claim_id')]
        if source_failures:
            runner.status = runner.run.stage = 'blocked:deterministic'
            self._save(state, runner)
            return dict(status='blocked:deterministic', blocked=True, findings=source_failures,
                        next_action='repair_source_or_calculation_before_global')
        claim_failures = [dict(claim_id=f['claim_id'], category=f.get('check_id', f.get('dimension', 'binding')),
            issue=f.get('reason', 'deterministic Claim validation failed'), evidence=deepcopy(f),
            suggested_fix='Repair the structured assertion or register the required canonical evidence.',
            resolution_route='claim_patch') for f in deterministic
            if f['status'] == 'FAIL' and f.get('claim_id')]
        if claim_failures:
            return self._prepare_global_repair(state, runner, findings=claim_failures)
        semantic_findings = runner._claim_review_findings()
        if semantic_findings:
            evidence_work = runner.prepare_evidence_work(semantic_findings)
            if evidence_work:
                self._save(state, runner)
                return evidence_work
            return self._prepare_global_repair(state, runner, findings=semantic_findings)
        # Recover a candidate persisted by an older two-transaction submit
        # that crashed before collection. New global submits commit atomically.
        for candidate in state["packets"].values():
            if (candidate.get("parallel_dependency") and not candidate.get("retired")
                    and candidate.get("input_hash") == state["input_hash"]
                    and candidate.get("global_context", {}).get("verification_epoch") == runner._verification_epoch
                    and "global_dependency_completion" in candidate.get("received", {})
                    and candidate["packet_id"] not in state.get("global_outcomes", {})):
                outcome = self._ingest_global(state, runner, candidate)
                self._save(state, runner)
                return outcome
        blocked = state.get("global_blocked")
        if blocked:
            return dict(status="blocked:global_contract", blocked=True,
                        reason=deepcopy(blocked), input_hash=state["input_hash"])
        if active and active.get("kind") == "global" and not active.get("retired"):
            key = next(iter(active["items"]))
            processed = state.setdefault("processed_items", {})
            if key in active["received"] and processed.get(key) != active["packet_id"]:
                outcome = self._ingest_global(state, runner, active)
                self._save(state, runner)
                return outcome
            if key not in active["received"] and active["input_hash"] == state["input_hash"]:
                try:
                    prepared = runner.prepare_phase6_global_sweep()
                except ValueError as exc:
                    return dict(status="blocked:global_preflight", blocked=True, reason=str(exc))
                if (self._matches_global(runner, active, prepared)
                        and active["items"][key]["prompt"] == prepared[1]):
                    self._save(state, runner)
                    return dict(status="global", kind="global", packet_id=active["packet_id"],
                                input_hash=state["input_hash"], items=deepcopy(active["items"]))
            active["retired"] = True
            if state.get("active_packet_id") == active["packet_id"]:
                state["active_packet_id"] = None

        if runner.global_receipt is not None and runner.pending_global is None:
            if runner.global_receipt.get("findings"):
                return self._prepare_global_repair(state, runner)
            # Re-render from the canonical graph for every delivery, including
            # after a restart. An already-complete checkpoint is not a reason
            # to return only a hash or lose the human-readable report.
            try:
                result = runner.finalize()
            except ValueError as exc:
                return dict(status="blocked:delivery_gate", blocked=True, reason=str(exc))
            self._save(state, runner)
            # The caller receives exactly the canonical final rendering and sidecar.
            disclosure = dict(secondary_events=deepcopy(state.get('secondary_events', [])),
                              independent_review_guaranteed=False,
                              identity_assurance='executor identities are declared, not independently attested')
            result['execution_disclosure'] = disclosure
            result['sidecar']['execution_disclosure'] = deepcopy(disclosure)
            return result

        if (state.get("parallel_dependency_mode") and runner.pending_global
                and runner.pending_global.get("review") and not runner.pending_global.get("resolution_completion")):
            coverage = runner._global_coverage_status()
            missing = coverage["missing_dependency_claim_ids"]
            if missing:
                current_root = dependency_identity(runner, missing[:1])
                current_root.pop("target_claim_ids")
                if current_root == state["parallel_dependency_mode"]:
                    self._save(state, runner)
                    return ({**coverage, "status": "global_dependency_ready", "kind": "dependency", "blocked": False,
                             "next_action": "claim-dependencies or dispatch-secondary"}
                            if coverage.get("needs_retry") else coverage)
                state.pop("parallel_dependency_mode", None)
        try:
            prepared = runner.prepare_phase6_global_sweep()
        except ValueError as exc:
            return dict(status="blocked:global_preflight", blocked=True, reason=str(exc))
        if isinstance(prepared, dict):
            self._save(state, runner)
            return deepcopy(prepared)
        if (not isinstance(prepared, tuple) or len(prepared) != 2
                or not all(isinstance(value, str) and value for value in prepared)):
            return dict(status="blocked:global_contract", blocked=True,
                        reason="global prepare did not produce a single role/prompt")
        role, prompt = prepared
        packet_id = uuid4().hex
        state["packets"][packet_id] = dict(packet_id=packet_id, kind="global",
                                           input_hash=state["input_hash"],
                                           global_context=self._global_identity(runner),
                                           items={role: dict(prompt=prompt)}, received={})
        state["active_packet_id"] = packet_id
        self._save(state, runner)
        return dict(status="global", kind="global", packet_id=packet_id,
                    input_hash=state["input_hash"], items={role: dict(prompt=prompt)})

    def _has_pending_patch(self):
        state = json_read(self.queue.state_path)
        return state.get("patch_prompt") is not None

    def _patch_view(self, state, packet):
        manual = bool(packet.get("manual_patch_only"))
        return dict(status="blocked:revision_limit" if manual else "patch", kind="patch",
                    blocked=manual, manual_patch_only=manual, packet_id=packet["packet_id"],
                    input_hash=state["input_hash"],
                    allowed_claim_ids=deepcopy(packet["allowed_claim_ids"]),
                    items=deepcopy(packet["items"]))

    def _link_view(self, state, packet):
        return dict(status="link", kind="link", packet_id=packet["packet_id"],
                    input_hash=state["input_hash"],
                    items=deepcopy(packet["items"]))

    def _prepare_global_repair(self, state, runner, *, findings=None):
        """Bind P6 global findings to the canonical Claim patch request.

        The real runner owns finding routing and allowed Claim IDs. Nothing is
        marked resolved here; findings stay open until a patch changes the
        graph and affected reviews are re-issued and re-accepted.
        """
        if state.get("link_blocked"):
            return dict(status="blocked:link_contract", blocked=True,
                        reason=deepcopy(state["link_blocked"]),
                        input_hash=state["input_hash"])
        if runner.pending_patch is None:
            findings = (deepcopy(findings) if findings is not None else
                        [deepcopy(f) for f in runner.global_receipt["findings"]])
            accepted = state.get("accepted_links")
            if accepted is not None:
                if (accepted.get("input_hash") != state["input_hash"]
                        or accepted.get("source_hash") != digest(findings)):
                    raise ValueError("saved finding links do not match the current global findings")
                findings = deepcopy(accepted["findings"])
            if not findings:
                return dict(status="blocked:global_findings", blocked=True,
                            findings=deepcopy(runner.global_receipt["findings"]))
            evidence_work = runner.prepare_evidence_work(findings)
            if evidence_work:
                # Carry every current finding across source ingestion. The
                # global receipt will be invalidated when evidence changes.
                merged = {row.get("finding_id"): deepcopy(row)
                          for row in runner.deferred_global_findings
                          if row.get("finding_id")}
                for finding in findings:
                    if finding.get("finding_id"):
                        merged[finding["finding_id"]] = deepcopy(finding)
                runner.deferred_global_findings = list(merged.values())
                self._save(state, runner)
                return evidence_work
            prepared = runner.prepare_phase6_revision_v4(findings)
            if prepared.get("needs_linking"):
                role, prompt = prepared["linker_prompt"]
                if role != LINK_ROLE:
                    raise ValueError("linker request role differs from the finding_linker contract")
                active = state["packets"].get(state.get("active_packet_id"))
                if (active and active.get("kind") == "link" and not active.get("retired")
                        and active["input_hash"] == state["input_hash"]
                        and active.get("verification_epoch") == runner._verification_epoch
                        and active["items"][LINK_ROLE]["prompt"] == prompt):
                    if LINK_ROLE not in active["received"]:
                        self._save(state, runner)
                        return self._link_view(state, active)
                    outcome = self._ingest_link(state, runner, active)
                    self._save(state, runner)
                    if outcome.get("blocked"):
                        return outcome
                    return self._prepare_global_repair(state, runner, findings=findings)
                    if state.get("active_packet_id") == active["packet_id"]:
                        state["active_packet_id"] = None
                for stale in state["packets"].values():
                    if (stale.get("kind") == "link" and not stale.get("retired")
                            and (stale["items"][LINK_ROLE]["prompt"] != prompt
                                 or stale.get("verification_epoch") != runner._verification_epoch)):
                        stale["retired"] = True
                packet_id = uuid4().hex
                state["packets"][packet_id] = dict(
                    packet_id=packet_id, kind="link", input_hash=state["input_hash"],
                    verification_epoch=runner._verification_epoch,
                    unrouted_finding_ids=[finding.get("finding_id")
                                          for finding in prepared.get("unrouted_findings", [])],
                    items={LINK_ROLE: dict(prompt=prompt)}, received={})
                state["active_packet_id"] = packet_id
                self._save(state, runner)
                return self._link_view(state, state["packets"][packet_id])
            if prepared.get("blocked") and "revision_prompts" not in prepared:
                if prepared.get("status") != "blocked:revision_limit":
                    self._save(state, runner)
                    return {**deepcopy(prepared), "input_hash": state["input_hash"]}
                # Spending is capped but the state-bound repair request stays
                # open for explicit operator patches; keep it deliverable.
                tasks = runner._tasks()
                prompt = "\n".join([json.dumps(f, ensure_ascii=False, sort_keys=True)
                                     for f in runner.pending_patch["findings"]]
                                    + [json.dumps(tasks[cid].review_dict(), ensure_ascii=False, sort_keys=True)
                                       for cid in runner.pending_patch["ids"]])
                allowed = list(runner.pending_patch["ids"])
            else:
                prompt = prepared["revision_prompts"][0][1]
                allowed = list(prepared["allowed_claim_ids"])
        else:
            if runner.pending_patch["input_hash"] != runner._hash():
                raise ValueError("stale pending claim repairs")
            prompt = state.get("patch_prompt")
            allowed = list(runner.pending_patch["ids"])
            if prompt is None:
                prepared = runner.prepare_phase6_revision_v4(deepcopy(runner.pending_patch["findings"]))
                if "revision_prompts" not in prepared:
                    self._save(state, runner)
                    return prepared
                prompt = prepared["revision_prompts"][0][1]
        state["patch_prompt"] = prompt
        packet_id = uuid4().hex
        state["packets"][packet_id] = dict(packet_id=packet_id, kind="patch", manual_patch_only=runner.revision_round >= runner.max_revisions,
                                           input_hash=state["input_hash"],
                                           allowed_claim_ids=allowed,
                                           items={PATCH_ROLE: dict(prompt=prompt)}, received={})
        state["active_packet_id"] = packet_id
        self._save(state, runner)
        return self._patch_view(state, state["packets"][packet_id])

    def _next_patch_packet(self, state, runner):
        """Redeliver or reissue the durable patch request awaiting explicit patches."""
        blocked = state.get("patch_blocked")
        if blocked:
            return dict(status="blocked:patch_contract", blocked=True,
                        reason=deepcopy(blocked), input_hash=state["input_hash"],
                        failed_packet_id=blocked['packet_id'], next_action='retry-patch',
                        retry_requires='operator reason; canonical pending repair and used rounds are retained')
        if runner.pending_patch is None or runner.pending_patch["input_hash"] != runner._hash():
            return None
        active = state["packets"].get(state.get("active_packet_id"))
        if (active and active.get("kind") == "patch" and not active.get("retired")
                and active["input_hash"] == state["input_hash"]
                and state.get("patch_prompt") == active["items"][PATCH_ROLE]["prompt"]):
            if PATCH_ROLE not in active["received"]:
                return self._patch_view(state, active)
            active["retired"] = True
            if state.get("active_packet_id") == active["packet_id"]:
                state["active_packet_id"] = None
        prompt = state.get("patch_prompt")
        if prompt is None:
            return self._prepare_global_repair(state, runner)
        packet_id = uuid4().hex
        state["packets"][packet_id] = dict(packet_id=packet_id, kind="patch", manual_patch_only=runner.revision_round >= runner.max_revisions,
                                           input_hash=state["input_hash"],
                                           allowed_claim_ids=list(runner.pending_patch["ids"]),
                                           items={PATCH_ROLE: dict(prompt=prompt)}, received={})
        state["active_packet_id"] = packet_id
        self._save(state, runner)
        return self._patch_view(state, state["packets"][packet_id])

    def submit_patch(self, packet_id, patches, executor, input_hash):
        """Accept one Claim patch array against the version-bound repair request.

        The real collector decides everything: no-op, unknown IDs, or stale
        state keeps findings open and spends no false PASS. A successful patch
        invalidates changed and dependent receipts and returns to atomic
        revalidation, never straight to done.
        """
        _require_identity(executor, "executor")
        if not isinstance(patches, list) or not patches:
            raise ValueError("patches must be a nonempty JSON array")
        for position, patch in enumerate(patches):
            if (not isinstance(patch, dict) or not isinstance(patch.get("claim_id"), str)
                    or not patch["claim_id"].strip()):
                raise ValueError(f"patch at index {position} requires a nonempty claim_id")
        state = json_read(self.queue.state_path)
        packet = state.get("packets", {}).get(packet_id)
        if not isinstance(packet, dict) or packet.get("kind") != "patch":
            raise ValueError("unknown patch packet")
        self.queue.submit(packet_id, {PATCH_ROLE: deepcopy(patches)}, executor, input_hash)
        with self.queue.transaction() as state:
            runner = self._load(state)
            outcome = self._ingest_patch(state, runner, state["packets"][packet_id])
            self._save(state, runner)
            return outcome

    def _ingest_patch(self, state, runner, packet):
        """Consume an archived patch candidate; the collector alone decides."""
        if not isinstance(packet, dict) or packet.get("kind") != "patch":
            raise ValueError("unknown patch packet")
        if packet["input_hash"] != state["input_hash"]:
            raise ValueError("stale patch packet input")
        row = packet["received"].get(PATCH_ROLE)
        if row is None:
            return self._patch_view(state, packet)
        if packet["packet_id"] in state.get("patch_outcomes", {}):
            return deepcopy(state["patch_outcomes"][packet["packet_id"]])
        try:
            if state.get("patch_prompt") != packet["items"][PATCH_ROLE]["prompt"]:
                raise ValueError("patch packet prompt differs from the canonical repair request")
            if runner.pending_patch is None or runner.pending_patch["input_hash"] != runner._hash():
                raise ValueError("stale or unprepared claim patch")
            if not isinstance(row["response"], list) or not all(isinstance(p, dict) for p in row["response"]):
                raise ValueError("patch response must be a JSON array of objects")
            RunJournal(self.directory).archive_external_submission(
                runner, model=row["executor"], task_id=f"patch.{packet['packet_id']}",
                prompt=packet["items"][PATCH_ROLE]["prompt"], response=deepcopy(row["response"]))
            result = runner.collect_phase6_revision(deepcopy(row["response"]), enforce_budget=False)
        except (ValueError, TypeError, KeyError) as exc:
            failure = dict(packet_id=packet["packet_id"], executor=row["executor"],
                           reason=f"{type(exc).__name__}: {exc}")
            state["patch_blocked"] = failure
            result = dict(status="blocked:patch_contract", blocked=True, reason=deepcopy(failure),
                          failed_packet_id=packet['packet_id'], next_action='retry-patch',
                          retry_requires='operator reason; canonical pending repair and used rounds are retained')
        else:
            state.pop("patch_blocked", None)
        packet["retired"] = True
        if state.get("active_packet_id") == packet["packet_id"]:
            state["active_packet_id"] = None
        state.setdefault("patch_outcomes", {})[packet["packet_id"]] = deepcopy(result)
        if not result.get("blocked"):
            state.pop("patch_prompt", None)
            if "accepted_links" in state:
                state.setdefault("historical_links", []).append(
                    deepcopy(state.pop("accepted_links")))
        return result

    def retry_patch(self, reason):
        """Explicit operator retry only after a rejected patch candidate.

        The rejected packet and its failure stay in the audit trail. No
        pending repair request is discarded and no model is called here.
        """
        _require_identity(reason, "retry reason")
        with self.queue.transaction() as state:
            failure = state.get("patch_blocked")
            if not isinstance(failure, dict):
                raise ValueError("no rejected patch candidate requires retry")
            packet_id = failure.get("packet_id")
            packet = state.get("packets", {}).get(packet_id)
            if (not isinstance(packet, dict) or packet.get("kind") != "patch"
                    or not packet.get("retired")):
                raise ValueError("rejected patch packet no longer matches current state")
            current_id = state.get("active_packet_id")
            if current_id is not None:
                raise ValueError("another packet is active; cannot retry patch")
            runner = self._load(state)
            if runner.pending_patch is None:
                raise ValueError("cannot retry a patch after its repair request closed")
            state.setdefault("patch_retry_history", []).append(
                dict(packet_id=packet_id, failure=deepcopy(failure), reason=reason.strip()))
            state.pop("patch_blocked", None)
            self._save(state, runner)
            return dict(status="retry_patch", retired_packet_id=packet_id,
                        input_hash=state["input_hash"])

    def _ingest_global(self, state, runner, packet):
        """Consume a previously archived candidate, never infer PASS from submission."""
        if not isinstance(packet, dict) or packet.get("kind") != "global":
            raise ValueError("unknown global packet")
        if packet["input_hash"] != state["input_hash"]:
            raise ValueError("stale global packet input")
        role = next(iter(packet["items"]))
        row = packet["received"].get(role)
        if row is None:
            return dict(status="global", kind="global", packet_id=packet["packet_id"],
                        input_hash=state["input_hash"], items=deepcopy(packet["items"]))
        processed = state.setdefault("processed_items", {})
        if packet["packet_id"] in state.get("global_outcomes", {}):
            return deepcopy(state["global_outcomes"][packet["packet_id"]])
        try:
            RunJournal(self.directory).archive_external_submission(
                runner, model=row["executor"], task_id=role,
                prompt=packet["items"][role]["prompt"], response=deepcopy(row["response"]))
            prepared = self._prepare_global_packet(runner, packet)
            if not self._matches_global(runner, packet, prepared):
                raise ValueError("global work packet no longer matches canonical request")
            if not isinstance(row["response"], dict):
                raise ValueError("global response must be a JSON object")
            result = self._collect_global_packet(runner, packet, row)
        except (ValueError, TypeError, KeyError) as exc:
            failure = dict(packet_id=packet["packet_id"], executor=row["executor"],
                           reason=f"{type(exc).__name__}: {exc}")
            state["global_blocked"] = failure
            result = dict(status="blocked:global_contract", blocked=True,
                          reason=deepcopy(failure))
        processed[role] = packet["packet_id"]
        packet["retired"] = True
        if state.get("active_packet_id") == packet["packet_id"]:
            state["active_packet_id"] = None
        if result.get("status") in {"revision", "verified"} and runner.global_receipt is not None:
            if result.get("status") == "revision" or runner.global_receipt.get("resolved_external_findings"):
                state.setdefault("global_receipt_snapshots", {})[packet["packet_id"]] = \
                    deepcopy(runner.global_receipt)
        state.setdefault("global_outcomes", {})[packet["packet_id"]] = deepcopy(result)
        state.setdefault("global_acceptance_order", []).append(packet["packet_id"])
        return result

    def retry_global(self, reason):
        """Explicit operator retry only after a rejected P6 candidate.

        The rejected packet and its failure stay in the audit trail. No
        accepted global result is discarded, and no model is called here.
        """
        _require_identity(reason, "retry reason")
        with self.queue.transaction() as state:
            failure = state.get("global_blocked")
            if not isinstance(failure, dict):
                raise ValueError("no rejected global candidate requires retry")
            packet_id = failure.get("packet_id")
            packet = state.get("packets", {}).get(packet_id)
            if (not isinstance(packet, dict) or packet.get("kind") != "global"
                    or not packet.get("retired") or packet.get("input_hash") != state["input_hash"]):
                raise ValueError("rejected global packet no longer matches current state")
            current_id = state.get("active_packet_id")
            if current_id is not None:
                raise ValueError("another packet is active; cannot retry global review")
            runner = self._load(state)
            if runner.global_receipt is not None:
                raise ValueError("cannot retry a completed global review")
            state.setdefault("global_retry_history", []).append(
                dict(packet_id=packet_id, failure=deepcopy(failure), reason=reason.strip()))
            state.pop("global_blocked", None)
            self._save(state, runner)
            return dict(status="retry_global", retired_packet_id=packet_id,
                        input_hash=state["input_hash"])

    def _ingest_link(self, state, runner, packet):
        """Validate and archive one semantic Claim mapping, without claiming PASS."""
        role = LINK_ROLE
        if (packet.get("kind") != "link" or packet.get("retired")
                or packet["input_hash"] != state["input_hash"]
                or packet.get("verification_epoch") != runner._verification_epoch):
            raise ValueError("stale or retired finding linker packet")
        row = packet.get("received", {}).get(role)
        if row is None:
            return self._link_view(state, packet)
        if state.get("link_blocked"):
            return dict(status="blocked:link_contract", blocked=True,
                        reason=deepcopy(state["link_blocked"]))
        original_deferred = deepcopy(runner.deferred_global_findings)
        try:
            original = deepcopy(runner.global_receipt["findings"])
            unrouted = [finding for finding in original
                        if not runner._finding_claim_ids(finding)]
            if [f.get("finding_id") for f in unrouted] != packet["unrouted_finding_ids"]:
                raise ValueError("linker finding IDs no longer match canonical findings")
            role_name, prompt = runner.prepare_finding_linker(unrouted)
            if role_name != role or prompt != packet["items"][role]["prompt"]:
                raise ValueError("linker response does not match the current prompt")
            if not isinstance(row["response"], list):
                raise ValueError("finding linker response must be a JSON array")
            linked = runner.apply_finding_links(unrouted, deepcopy(row["response"]))
            linked_by_id = {f["finding_id"]: f for f in linked}
            resolved = [deepcopy(linked_by_id.get(f.get("finding_id"), f)) for f in original]
            if any(f.get("routing_unresolved") for f in resolved):
                raise ValueError("semantic mapping unresolved; operator intervention required")
            RunJournal(self.directory).archive_external_submission(
                runner, model=row["executor"], task_id=role,
                prompt=prompt, response=deepcopy(row["response"]))
            state["accepted_links"] = dict(
                input_hash=state["input_hash"], source_hash=digest(original),
                verification_epoch=runner._verification_epoch,
                packet_id=packet["packet_id"], findings=resolved,
                executor=row["executor"], response=deepcopy(row["response"]))
        except (ValueError, TypeError, KeyError) as exc:
            runner.deferred_global_findings = original_deferred
            failure = dict(packet_id=packet["packet_id"], executor=row["executor"],
                           reason=f"{type(exc).__name__}: {exc}")
            state["link_blocked"] = failure
            result = dict(status="blocked:link_contract", blocked=True, reason=deepcopy(failure))
        else:
            result = dict(status="linked", linked_finding_ids=packet["unrouted_finding_ids"],
                          input_hash=state["input_hash"])
        packet["retired"] = True
        if state.get("active_packet_id") == packet["packet_id"]:
            state["active_packet_id"] = None
        return result

    def retry_link(self, reason):
        """Operator-authorized new packet after rejected semantic mapping."""
        _require_identity(reason, "retry reason")
        with self.queue.transaction() as state:
            failure = state.get("link_blocked")
            if not isinstance(failure, dict):
                raise ValueError("no rejected linker candidate requires retry")
            packet = state["packets"].get(failure.get("packet_id"))
            if not packet or packet.get("kind") != "link" or not packet.get("retired"):
                raise ValueError("rejected linker packet not found")
            if packet["input_hash"] != state["input_hash"]:
                raise ValueError("linker packet input has changed")
            state.setdefault("link_retry_history", []).append(
                dict(failure=deepcopy(failure), reason=reason.strip()))
            state.pop("link_blocked", None)
            return dict(status="retry_link", input_hash=state["input_hash"],
                        retired_packet_id=packet["packet_id"])

    def submit(self, packet_id, responses, executor, input_hash):
        state = json_read(self.queue.state_path)
        packet = state.get("packets", {}).get(packet_id)
        if isinstance(packet, dict) and packet.get("kind") == "link":
            _require_identity(executor, "executor")
            self.queue.submit(packet_id, responses, executor, input_hash)
            with self.queue.transaction() as state:
                runner = self._load(state)
                outcome = self._ingest_link(state, runner, state["packets"][packet_id])
                self._save(state, runner)
                return outcome
        if isinstance(packet, dict) and packet.get("kind") == "global":
            _require_identity(executor, "executor")
            with self.queue.transaction() as state:
                runner = self._load(state)
                packet = state["packets"][packet_id]
                accepted = state.get("global_outcomes", {}).get(packet_id)
                if accepted is not None:
                    context = packet["global_context"]
                    if (state["input_hash"] != input_hash or packet["input_hash"] != input_hash
                            or context.get("verification_epoch") != runner._verification_epoch
                            or context.get("history_hash") != digest(runner.history)):
                        raise ValueError("stale accepted global response")
                    if (not isinstance(responses, dict) or set(responses) != set(packet["items"])
                            or any(packet["received"].get(role, {}).get("executor") != executor
                                   or packet["received"].get(role, {}).get("response") != response
                                   for role, response in responses.items())):
                        raise ValueError("conflicting duplicate global response")
                    for role, response in responses.items():
                        if RunJournal(self.directory).response_provenance(runner,
                                SimpleNamespace(identity=executor), role,
                                packet["items"][role]["prompt"], response) is None:
                            raise ValueError("accepted global response has no verified raw archive")
                    return {**deepcopy(accepted), "idempotent": True}
                submit_items_in_state(state, packet_id, responses, executor, input_hash)
                outcome = self._ingest_global(state, runner, state["packets"].get(packet_id))
                self._save(state, runner)
                return outcome
        if isinstance(packet, dict) and packet.get("kind") == "patch":
            if packet.get('manual_patch_only'):
                raise ValueError('revision limit: use explicit submit_patch for operator patches')
            _require_identity(executor, "executor")
            patches = responses.get(PATCH_ROLE) if isinstance(responses, dict) else None
            return self.submit_patch(packet_id, patches, executor, input_hash)
        if not isinstance(packet, dict) or packet.get("kind") not in REVIEW_KINDS:
            return super().submit(packet_id, responses, executor, input_hash)
        _require_identity(executor, "executor")
        self.queue.submit(packet_id, responses, executor, input_hash)
        with self.queue.transaction() as state:
            runner = self._load(state)
            outcome = self._ingest_reviews(state, runner, state["packets"].get(packet_id))
            self._save(state, runner)
            return outcome

    def _ingest_reviews(self, state, runner, packet):
        layer = packet["kind"]
        packet_id = packet["packet_id"]
        review_errors = state.setdefault("review_errors", {})
        blocked = state.setdefault("review_blocked", {})
        processed = state.setdefault("processed_items", {})
        tasks = runner._tasks()
        if layer == "atomic":
            prepared = runner._prepare_phase6_atomic(tasks, enforce_budget=False)
            prompts, _schemas = dict(prepared["atomic_prompts"]), prepared["response_schemas"]
        else:
            wanted = [cid for cid in tasks if cid in runner.receipts and runner.receipts[cid]["result"]["verdict"] != "PASS"
                      and cid not in runner.deep_receipts]
            prepared = runner._prepare_phase6_deep_review(tasks, wanted, enforce_budget=False)
            prompts, _schemas = dict(prepared["deep_prompts"]), prepared["response_schemas"]
        journal = RunJournal(self.directory)
        for cid in list(packet["received"]):
            if cid in packet.get("processed_item_ids", {}) or processed.get(cid) == packet_id:
                continue
            packet.setdefault("processed_item_ids", {})[cid] = True
            row = packet["received"][cid]
            item = packet["items"][cid]
            context = item["review_context"]
            task_hash = digest(tasks[cid].to_dict()) if cid in tasks else None
            if task_hash is None or context["task_hashes"].get(cid) != task_hash:
                review_errors[cid] = dict(executor=row["executor"],
                                          reason="review packet predates the current Claim object")
                processed[cid] = packet_id
                continue
            if layer == "deep" and (cid not in runner.receipts or context.get(
                    "first_review_hashes", {}).get(cid) != digest(runner.receipts[cid]["result"])):
                review_errors[cid] = dict(executor=row["executor"],
                                          reason="deep review packet differs from the current first-layer result")
                processed[cid] = packet_id
                continue
            if context["verification_epoch"] != runner._verification_epoch:
                review_errors[cid] = dict(executor=row["executor"],
                                          reason="verification epoch differs; explicit fresh verification required")
                processed[cid] = packet_id
                continue
            if (cid not in prompts or item["prompt"] != prompts[cid]
                    or item["schema"] != _schemas[cid]
                    or context != runner.review_context_for(layer, [cid], tasks=tasks)):
                review_errors[cid] = dict(executor=row["executor"], reason="stale review request")
                processed[cid] = packet_id
                continue
            supplement = runner.reference_supplements[layer].get(cid)
            if supplement and supplement.get("model") != row["executor"]:
                blocked[cid] = dict(executor=row["executor"],
                    reason="reference supplement requires its original reviewer; explicit restart required")
                processed[cid] = packet_id
                continue
            prompt = item["prompt"]
            try:
                proof = packet.get("batch_sources", {}).get(cid)
                if proof is None:
                    proof = journal.archive_external_submission(
                        runner, model=row["executor"], task_id=f"{layer}.{cid}", prompt=prompt,
                        response=deepcopy(row["response"]), response_schema=item["schema"],
                        review_context=context)
                runner._latest_review_sources[layer][cid] = {**proof, "claim_id": cid, "layer": layer}
                summary = (runner.collect_phase6_atomic({cid: deepcopy(row["response"])},
                                                        model_identity=row["executor"], allow_partial=True)
                           if layer == "atomic" else
                           runner.collect_phase6_deep_review({cid: deepcopy(row["response"])},
                                                             model_identity=row["executor"], allow_partial=True))
            except ValueError as exc:
                review_errors[cid] = dict(executor=row["executor"], reason=f"{type(exc).__name__}: {exc}")
                processed[cid] = packet_id
                continue
            processed[cid] = packet_id
            accepted = runner.receipts if layer == 'atomic' else runner.deep_receipts
            if accepted.get(cid, {}).get('input_hash') == task_hash:
                review_errors.pop(cid, None)
                blocked.pop(cid, None)
            elif summary.get("blocked"):
                review_errors[cid] = dict(executor=row["executor"],
                                          reason=summary.get("validation_errors", {}).get(cid, "pending retry"))
                if not summary.get("needs_retry", True):
                    blocked[cid] = dict(executor=row["executor"],
                                        retry_claim_ids=summary.get("retry_claim_ids", [cid]),
                                        reason="semantic contract attempts exhausted; explicit operator route required")
                else:
                    blocked.pop(cid, None)
            else:
                review_errors.pop(cid, None)
                blocked.pop(cid, None)
        if all(cid in packet.get("processed_item_ids", {}) or processed.get(cid) == packet_id
               for cid in packet["items"]):
            packet["retired"] = True
            if state.get("active_packet_id") == packet_id:
                state["active_packet_id"] = None
        missing = [cid for cid in packet["items"] if cid not in packet["received"]]
        result = dict(status=("blocked:review_contract" if blocked else
                              "blocked:reviews" if review_errors else "reviews_staged"),
                      packet_id=packet_id, layer=layer,
                      received_item_ids=sorted(packet["received"]),
                      missing_item_ids=missing,
                      review_errors=deepcopy(review_errors), blocked_claims=deepcopy(blocked),
                      atomic_receipts=len(runner.receipts), deep_receipts=len(runner.deep_receipts),
                      input_hash=state["input_hash"])
        result["blocked"] = bool(blocked) or bool(review_errors)
        result["needs_retry"] = bool(review_errors) and not bool(blocked)
        return result
