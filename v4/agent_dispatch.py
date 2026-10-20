"""Optional AGY secondary review scheduling over the Native host-owned V4 queue.

No global state lives here. AGY is only an acceleration worker for atomic/deep
review items; a failed request is left in the durable queue for the host Agent.
The host still owns all role generation, P6 global decisions, patches and report.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json

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


def dispatch_secondary_reviews(workflow, client, *, max_workers=4, max_items=16):
    """Dispatch bounded independent Claim reviews, never auto-approve failures.

    One work packet may contain many Claims and every accepted result gets a
    separate collector verdict and durable per-Claim provenance. Failures never
    submit a placeholder PASS. The host may continue with remaining items.
    """
    if type(max_workers) is not int or not 1 <= max_workers <= MAX_SECONDARY_WORKERS:
        raise ValueError("max_workers must be 1..8")
    if type(max_items) is not int or not 1 <= max_items <= 16:
        raise ValueError("max_items must be 1..16")
    identity = getattr(client, "identity", None)
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("secondary client requires a declared identity")

    packet = workflow.next()
    kind = packet.get("kind")
    if kind not in ("atomic", "deep"):
        return dict(status="host_owned", kind=kind, packet=packet,
                    reason="AGY receives only independent atomic/deep review items")
    if packet.get("blocked"):
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
    items = list(packet["items"].items())[:max_items]
    packet_id, input_hash = packet["packet_id"], packet["input_hash"]
    accepted, failures = [], {}

    def answer(item):
        cid, task = item
        # A new namespace requires explicit operator restore; ordinary retries
        # keep the same durable session and never discard previous work.
        session_dir = (workflow.directory / 'secondary-sessions' / digest(dict(
            identity=identity, generation=generation, prompt=task['prompt'], schema=task.get('schema')))
            if generation else None)
        raw = request_completion(client, task["prompt"], task.get("schema"), session_dir=session_dir)
        if isinstance(raw, str):
            parsed = json.loads(normalize_json_response(raw))
        else:
            parsed = raw
        if not isinstance(parsed, dict):
            raise ValueError("one Claim review must return one JSON object")
        return cid, parsed

    # Results are committed to the queue individually, not as an all-or-nothing
    # batch, so one AGY failure never invalidates successful independent checks.
    quota_failure = None
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        iterator = iter(items)
        futures = {}
        for _ in range(min(max_workers, len(items))):
            item = next(iterator, None)
            if item is not None:
                futures[pool.submit(answer, item)] = item[0]
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                cid = futures.pop(future)
                try:
                    _, result = future.result()
                    outcome = workflow.submit(packet_id, {cid: result}, identity, input_hash)
                    if outcome.get("blocked") and not outcome.get("needs_retry"):
                        failures[cid] = str(outcome.get("status", "collector rejected response"))
                    else:
                        accepted.append(cid)
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    if code == "quota_exhausted":
                        if quota_failure is None:
                            with workflow.queue.transaction() as state:
                                state.setdefault("secondary_availability", {})[identity] = dict(
                                    status="unavailable", reason_code=code, generation=generation,
                                    event_reference=getattr(exc, "event_reference", None))
                            workflow.record_secondary_event(packet, code=code, requested_identity=identity,
                                                            failed_claim_ids=[cid])
                        quota_failure = exc
                        failures[cid] = code
                    elif code == "rate_limited":
                        failures[cid] = code
                    else:
                        failures[cid] = f"{type(exc).__name__}: {exc}"
            if quota_failure is None:
                for _ in completed:
                    item = next(iterator, None)
                    if item is not None:
                        futures[pool.submit(answer, item)] = item[0]
    warning = workflow.record_secondary_event(packet,
        code='secondary_failed' if failures else 'secondary_completed', requested_identity=identity,
        accepted_claim_ids=accepted, failed_claim_ids=failures)
    outstanding = workflow.next()
    return dict(status="fallback_to_main" if failures else "secondary_complete",
                packet_id=packet_id, input_hash=input_hash, kind=kind,
                accepted_claim_ids=sorted(accepted),
                failures=failures, outstanding=outstanding, warnings=[warning] if failures else [])
