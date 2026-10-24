"""Optional AGY secondary review scheduling over the Native host-owned V4 queue.

No global state lives here. AGY is only an acceleration worker for atomic/deep
review items; a failed request is left in the durable queue for the host Agent.
The host still owns all role generation, P6 global decisions, patches and report.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
import time

from .agent_queue import renew_items_in_state

from .cloud import request_completion
from .response_json import normalize_json_response
from .schema import digest

MAX_SECONDARY_WORKERS = 8


def restore_secondary_availability(workflow, identity, *, reason):
    """Explicitly restore a quota-disabled secondary after operator verification."""
    if not isinstance(identity, str) or not identity.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError("identity and operator reason are required")
    with workflow.queue.transaction() as state:
        availability = state.setdefault("secondary_availability", {})
        current = availability.get(identity)
        if not current or current.get("status") != "unavailable":
            return False
        availability[identity] = dict(status="available", restored_reason=reason.strip(),
                                      generation=current.get("generation", 0) + 1)
    return True


def dispatch_secondary_reviews(workflow, client, *, max_workers=4, max_items=16,
                               batch_size=1, continuous=False, lease_seconds=3600):
    """Refill bounded worker slots; durable leases prevent host/AGY duplicates.

    Library callers retain single-packet compatibility by default. The CLI
    enables bounded wire batching and refill across packet boundaries. Every
    response is archived as actually received, then normalized per Claim.
    """
    if type(max_workers) is not int or not 1 <= max_workers <= MAX_SECONDARY_WORKERS:
        raise ValueError("max_workers must be 1..8")
    if type(max_items) is not int or max_items < 1:
        raise ValueError("max_items must be a positive integer")
    identity = getattr(client, "identity", None)
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("secondary client requires a declared identity")
    packet = workflow.next()
    kind = packet.get("kind")
    dependency_mode = (kind == "dependency" or
        (kind == "global" and set(packet.get("items", {})) == {"global_dependency_completion"}))
    if kind not in ("atomic", "deep") and not dependency_mode:
        return dict(status="host_owned", kind=kind, packet=packet,
                    reason="this stage remains with the host Agent")
    if packet.get("blocked") and not (dependency_mode and packet.get("needs_retry")):
        return dict(status="fallback_to_main", kind=kind, packet=packet,
                    warnings=[workflow.record_secondary_event(packet, code='workflow_blocked', requested_identity=identity)],
                    reason="canonical workflow is blocked, host intervention required")
    with workflow.queue.transaction() as state:
        availability = state.get("secondary_availability", {}).get(identity, {})
        disabled = availability.get("status") == "unavailable"
        generation = availability.get("generation", 0)
        if type(generation) is not int or generation < 0:
            raise ValueError("invalid secondary restore generation")
    if disabled:
        return dict(status="fallback_to_main", kind=kind, packet=packet,
                    failures={}, outstanding=packet, warnings=[],
                    reason="secondary quota is unavailable; operator restore is required")
    accepted, failures, leases = [], {}, []
    outstanding_requests = {}
    dispatched = 0
    quota_failure = False
    initial_packet_id = packet.get("packet_id")
    legacy_limit = min(max_items, len(packet.get("items", {}))) if not dependency_mode else max_items
    limit = max_items if continuous else legacy_limit

    def answer(request):
        session_dir = (workflow.directory / 'secondary-sessions' / digest(dict(
            identity=identity, generation=generation, prompt=request['prompt'], schema=request['response_schema']))
            if generation else None)
        raw = request_completion(client, request["prompt"], request["response_schema"], session_dir=session_dir)
        parsed = json.loads(normalize_json_response(raw)) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            raise ValueError("review response must be a JSON object")
        return parsed

    def renew():
        now = time.time()
        due = [owned for owned in leases
               if outstanding_requests.get(owned["lease_id"], 0) > 0
               and owned["expires_at"] - now <= lease_seconds / 3]
        if not due:
            return
        # One locked state read/write for all due leases. Fast model replies
        # need no renewal I/O; long-running requests keep their ownership.
        with workflow.queue.transaction() as state:
            for owned in due:
                renewed = renew_items_in_state(state, owned["packet_id"], identity,
                                               owned["lease_id"], lease_seconds)
                if renewed["expires_at"] is not None:
                    owned["expires_at"] = renewed["expires_at"]

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            while True:
                while not quota_failure and len(futures) < max_workers and dispatched < limit:
                    # A small reservation per free slot leaves work available
                    # to the main Agent and refills without waiting for a tail.
                    if dependency_mode:
                        claim = workflow.claim_dependency_reviews(identity,
                            max_items=min(batch_size, limit-dispatched), lease_seconds=lease_seconds,
                            exclude_claim_ids=list(failures))
                        if claim.get("status") != "dependency_reviews_claimed":
                            break
                        role = "global_dependency_completion"
                        requests = [dict(request_id=role, claim_ids=claim["target_claim_ids"],
                                         prompt=claim["items"][role]["prompt"], response_schema=None)]
                    else:
                        claim = workflow.claim_reviews(identity, max_items=min(batch_size, limit-dispatched),
                            batch_size=batch_size, lease_seconds=lease_seconds, exclude_claim_ids=list(failures))
                        if claim.get("status") != "reviews_claimed":
                            break
                        requests = claim["requests"]
                    leases.append(claim["lease"])
                    outstanding_requests[claim["lease"]["lease_id"]] = len(requests)
                    for request in requests:
                        futures[pool.submit(answer, request)] = (claim, request)
                        dispatched += len(request["claim_ids"])
                if not futures:
                    break
                completed, _ = wait(futures, timeout=min(30, lease_seconds / 3), return_when=FIRST_COMPLETED)
                renew()
                for future in completed:
                    claim, request = futures.pop(future)
                    ids = request["claim_ids"]
                    try:
                        result = future.result()
                        if dependency_mode:
                            outcome = workflow.submit(claim["packet_id"],
                                {"global_dependency_completion": result}, identity, claim["input_hash"])
                            if outcome.get("status") == "blocked:global_contract":
                                raise ValueError(outcome.get("reason"))
                            returned = [row["claim_id"] for row in result.get("dependency_reviews", [])]
                            outcome = {**outcome, "returned_claim_ids": returned,
                                       "omitted_claim_ids": sorted(set(ids) - set(returned))}
                        else:
                            outcome = workflow.submit_review_request(claim["packet_id"], request["request_id"],
                                result, identity, claim["input_hash"])
                        invalid = outcome.get("review_errors", {})
                        for cid in outcome["returned_claim_ids"]:
                            if cid in invalid:
                                failures[cid] = invalid[cid]["reason"]
                            else:
                                accepted.append(cid)
                        for cid in outcome["omitted_claim_ids"]:
                            failures[cid] = "missing batched Claim response; retained for main Agent"
                    except Exception as exc:
                        code = getattr(exc, "code", None)
                        if code == "quota_exhausted":
                            if not quota_failure:
                                with workflow.queue.transaction() as state:
                                    state.setdefault("secondary_availability", {})[identity] = dict(
                                        status="unavailable", reason_code=code, generation=generation,
                                        event_reference=getattr(exc, "event_reference", None))
                                workflow.record_secondary_event(packet, code=code, requested_identity=identity,
                                                                failed_claim_ids=ids)
                            quota_failure = True
                        failures.update({cid: code or f"{type(exc).__name__}: {exc}" for cid in ids})
                    finally:
                        lease_id = claim["lease"]["lease_id"]
                        outstanding_requests[lease_id] -= 1
                        if outstanding_requests[lease_id] == 0:
                            workflow.queue.release_items(claim["packet_id"], identity, lease_id)
    finally:
        for owned in leases:
            lease_id = owned["lease_id"]
            if outstanding_requests[lease_id] > 0:
                workflow.queue.release_items(owned["packet_id"], identity, lease_id)
    warning = workflow.record_secondary_event(packet,
        code='secondary_failed' if failures else 'secondary_completed', requested_identity=identity,
        accepted_claim_ids=accepted, failed_claim_ids=failures)
    outstanding = workflow.next()
    return dict(status="fallback_to_main" if failures else "secondary_complete",
                packet_id=initial_packet_id, input_hash=packet.get("input_hash"), kind=kind,
                accepted_claim_ids=sorted(set(accepted)), failures=failures,
                dispatched_claim_count=dispatched, outstanding=outstanding,
                warnings=[warning] if failures else [])
