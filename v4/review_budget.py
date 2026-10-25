"""Layer-aware review byte limits and exact request-size diagnostics."""
import json

from .cloud import structured_prompt
from .review_wire import decode_review_payload

DEFAULT_ATOMIC_REVIEW_MAX_BYTES = 128 * 1024
DEFAULT_DEEP_REVIEW_MAX_BYTES = 128 * 1024


def review_budget_for(runner, layer):
    if layer == "atomic":
        return runner.review_batch_max_bytes
    if layer == "deep":
        return runner.review_deep_batch_max_bytes
    raise ValueError("unknown review layer")


def _prompt_payload(prompt):
    """Decode a known review payload marker without interpreting prose."""
    markers = ("<context-data>\n", "```json\n")
    for marker in markers:
        start = prompt.find(marker)
        if start < 0:
            continue
        try:
            payload, _ = json.JSONDecoder().raw_decode(prompt[start + len(marker):].lstrip())
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _own_evidence(payload):
    """Count canonical item content, not a prose-derived estimate.

    Values are encoded independently as compact UTF-8 JSON. The remaining
    measured wire bytes are attributed to shared prompt/serialization
    overhead, so each row remains an exact additive accounting of the actual
    structured request.
    """
    if not isinstance(payload, dict):
        return 0

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                   .encode("utf-8"))

    total = 0

    def item_content(item, *, first_review=None):
        nonlocal total
        if not isinstance(item, dict):
            return
        if "claim" in item:
            total += size(item["claim"])
        dependencies = item.get("dependency_claims")
        if isinstance(dependencies, list):
            total += sum(size(row) for row in dependencies)
        resolved = item.get("resolved_refs")
        if isinstance(resolved, dict):
            total += sum(size(row) for row in resolved.values())
        for key in ("first_review", "first_layer_review"):
            if isinstance(item.get(key), dict):
                total += size(item[key])
        if isinstance(first_review, dict):
            total += size(first_review)

    dimension_catalog = payload.get("binding_dimension_catalog")
    if isinstance(dimension_catalog, dict):
        total += sum(size(value) for value in dimension_catalog.values())

    if isinstance(payload.get("tasks"), list):
        for field in ('evidence_catalog', 'dependency_catalog'):
            catalog = payload.get(field)
            if isinstance(catalog, dict):
                total += sum(size(value) for value in catalog.values())
        for row in payload["tasks"]:
            item_content(row)
    elif isinstance(payload.get("task"), dict):
        item_content(payload["task"], first_review=payload.get("first_review"))
    else:
        item_content(payload)
    return total


def context_requires_scoped_review(context):
    if not isinstance(context, dict):
        return False
    scope = context.get("evidence_scope")
    diagnostics = scope.get("ingestion_diagnostics") if isinstance(scope, dict) else None
    return (isinstance(diagnostics, dict)
            and diagnostics.get("requires_scoped_review") is True)


def task_requires_scoped_review(task):
    return context_requires_scoped_review(getattr(task, "context", None))


def requires_scoped_review(prompt):
    """Whether a review item depends on a diagnostics projection with omissions."""
    payload = _prompt_payload(prompt)
    if not isinstance(payload, dict):
        return False
    payload = decode_review_payload(payload)
    contexts = []
    if isinstance(payload.get("context"), dict):
        contexts.append(payload["context"])
    task = payload.get("task")
    if isinstance(task, dict) and isinstance(task.get("context"), dict):
        contexts.append(task["context"])
    rows = payload.get("tasks")
    if isinstance(rows, list):
        contexts.extend(row["context"] for row in rows
                        if isinstance(row, dict) and isinstance(row.get("context"), dict))
    for context in contexts:
        if context_requires_scoped_review(context):
            return True
    return False


def review_wire_breakdown(prompt, schema):
    """Return exact wire bytes with an additive evidence/context/schema split.

    The full prompt-plus-schema length is measured as transmitted. Resolved
    reference objects are attributed to own evidence using their exact compact
    JSON encoding; shared context is the measured remainder after schema and
    evidence bytes are accounted for.
    """
    serialized_schema = (None if schema is None else
                         json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    wire = structured_prompt(prompt, serialized_schema)
    wire_bytes = len(wire.encode("utf-8"))
    schema_bytes = 0 if serialized_schema is None else len(serialized_schema.encode("utf-8"))
    available_prompt_bytes = max(0, wire_bytes - schema_bytes)
    payload = _prompt_payload(prompt)
    evidence_bytes = _own_evidence(payload)
    own_evidence_bytes = min(evidence_bytes, available_prompt_bytes)
    shared_context_bytes = wire_bytes - schema_bytes - own_evidence_bytes
    return dict(wire_bytes=wire_bytes, own_evidence_bytes=own_evidence_bytes,
                shared_context_bytes=shared_context_bytes, schema_bytes=schema_bytes)


def review_budget_row(claim_id, prompt, schema, budget_bytes):
    """Return a blocking item diagnostic, or None when it is safe to dispatch."""
    breakdown = review_wire_breakdown(prompt, schema)
    diagnostics_incomplete = requires_scoped_review(prompt)
    exceeds_budget = breakdown["wire_bytes"] > budget_bytes
    if diagnostics_incomplete:
        reason = "related_diagnostics_incomplete"
        next_action = "reduce_structured_refs_or_adjust_related_diagnostics_cap"
    elif exceeds_budget:
        reason = "wire_bytes_exceeds_budget"
        next_action = "inspect_wire_breakdown_then_configure_reviews_or_reduce_structured_context"
    else:
        return None
    return dict(claim_id=claim_id, **breakdown, budget_bytes=budget_bytes,
                reason=reason, budget_exceeded=exceeds_budget, next_action=next_action)
