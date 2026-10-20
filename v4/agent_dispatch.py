"""Optional AGY secondary review scheduling over the Native host-owned V4 queue.

No global state lives here. AGY is only an acceleration worker for atomic/deep
review items; a failed request is left in the durable queue for the host Agent.
The host still owns all role generation, P6 global decisions, patches and report.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

from .cloud import request_completion
from .response_json import normalize_json_response

MAX_SECONDARY_WORKERS = 8


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
    items = list(packet["items"].items())[:max_items]
    packet_id, input_hash = packet["packet_id"], packet["input_hash"]
    accepted, failures = [], {}

    def answer(item):
        cid, task = item
        raw = request_completion(client, task["prompt"], task.get("schema"))
        if isinstance(raw, str):
            parsed = json.loads(normalize_json_response(raw))
        else:
            parsed = raw
        if not isinstance(parsed, dict):
            raise ValueError("one Claim review must return one JSON object")
        return cid, parsed

    # Results are committed to the queue individually, not as an all-or-nothing
    # batch, so one AGY failure never invalidates successful independent checks.
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        futures = {pool.submit(answer, item): item[0] for item in items}
        for future in as_completed(futures):
            cid = futures[future]
            try:
                _, result = future.result()
                outcome = workflow.submit(packet_id, {cid: result}, identity, input_hash)
                if outcome.get("blocked") and not outcome.get("needs_retry"):
                    failures[cid] = str(outcome.get("status", "collector rejected response"))
                else:
                    accepted.append(cid)
            except (Exception) as exc:
                # Tool availability, provider quota, session errors, invalid
                # response and input-staleness are all host-recoverable.
                failures[cid] = f"{type(exc).__name__}: {exc}"
    warning = workflow.record_secondary_event(packet,
        code='secondary_failed' if failures else 'secondary_completed', requested_identity=identity,
        accepted_claim_ids=accepted, failed_claim_ids=failures)
    outstanding = workflow.next()
    return dict(status="fallback_to_main" if failures else "secondary_complete",
                packet_id=packet_id, input_hash=input_hash, kind=kind,
                accepted_claim_ids=sorted(accepted),
                failures=failures, outstanding=outstanding, warnings=[warning] if failures else [])
