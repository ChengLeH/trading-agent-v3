"""Lossless, typed references for repeated review wire data.

This projection only pools byte-identical task-context fields and explicit Claim
binding dimension tuples. Canonical Claims, verification hashes and evidence
objects are copied as-is and remain the source of truth.
"""

from copy import deepcopy
import json

from .schema import DIMENSIONS, digest

_CONTEXT_CATALOG = "context_value_catalog"
_CONTEXT_REFS = "context_refs"
_DIMENSION_CATALOG = "binding_dimension_catalog"
_DIMENSION_REF = "binding_dimension_ref"

CONTEXT_CATALOG_INSTRUCTIONS = (
    "wire context decoding：每個 task 的 context_refs 依 ID 從 context_value_catalog "
    "取值，還原到該 task.context 指定欄位後再審查。只還原明示的欄位和值；不推測，"
    "也不借用其他 task 的 context。"
)

BINDING_DIMENSION_INSTRUCTIONS = (
    "wire binding decoding：binding_dimension_ref 依 ID 從 binding_dimension_catalog "
    "還原 entity/concept/period/scope/unit/basis，並保留原 ref_id 配對。只用目錄值，"
    "不推測或替換維度。"
)


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8"))


def _task_rows(payload):
    rows, seen = [], set()

    def add(value):
        if (isinstance(value, dict) and "claim" in value
                and ("context" in value or _CONTEXT_REFS in value)
                and id(value) not in seen):
            rows.append(value)
            seen.add(id(value))

    add(payload)
    if not isinstance(payload, dict):
        return rows
    add(payload.get("task"))
    task_list = payload.get("tasks")
    if isinstance(task_list, list):
        for row in task_list:
            add(row)
    nested = payload.get("task")
    if isinstance(nested, dict):
        task_list = nested.get("tasks")
        if isinstance(task_list, list):
            for row in task_list:
                add(row)
    return rows


def review_wire_instructions(payload):
    """Return the exact conditional decoder text for a projected payload."""
    if not isinstance(payload, dict):
        return ""
    instructions = []
    if _CONTEXT_CATALOG in payload:
        instructions.append(CONTEXT_CATALOG_INSTRUCTIONS)
    if _DIMENSION_CATALOG in payload:
        instructions.append(BINDING_DIMENSION_INSTRUCTIONS)
    return "\n" + "\n".join(instructions) + "\n" if instructions else ""


def _pool_context_values(payload):
    candidate = deepcopy(payload)
    rows = _task_rows(candidate)
    if not rows or any(_CONTEXT_REFS in row for row in rows):
        return candidate

    groups = {}
    for row_index, row in enumerate(rows):
        context = row.get("context")
        if not isinstance(context, dict):
            continue
        for key, value in context.items():
            if not isinstance(key, str):
                continue
            fingerprint = "context-field." + digest({"field": key, "value": value})
            prior = groups.setdefault(fingerprint, [])
            if prior and prior[0][2] != value:
                raise ValueError(f"conflicting review context values for {fingerprint}")
            prior.append((row_index, key, value))

    current_cost = (_json_bytes(candidate)
                    + len(review_wire_instructions(candidate).encode("utf-8")))
    catalog_index = 0
    for fingerprint in sorted(groups):
        group = groups[fingerprint]
        if len(group) < 2:
            continue
        ref = f"c{catalog_index}"
        step = deepcopy(candidate)
        step_rows = _task_rows(step)
        for row_index, key, value in group:
            context = step_rows[row_index].get("context")
            if not isinstance(context, dict) or context.get(key) != value:
                raise ValueError(f"review context changed while packing {fingerprint}")
            del context[key]
            step_rows[row_index].setdefault(_CONTEXT_REFS, {})[key] = ref
        step.setdefault(_CONTEXT_CATALOG, {})[ref] = deepcopy(group[0][2])
        step_cost = (_json_bytes(step)
                     + len(review_wire_instructions(step).encode("utf-8")))
        if step_cost < current_cost:
            candidate = step
            current_cost = step_cost
            catalog_index += 1
    return candidate


def _claim_objects(payload):
    claims, seen = [], set()

    def add(claim):
        if isinstance(claim, dict) and id(claim) not in seen:
            claims.append(claim)
            seen.add(id(claim))

    def add_task(task):
        if not isinstance(task, dict):
            return
        add(task.get("claim"))
        dependencies = task.get("dependency_claims")
        if isinstance(dependencies, list):
            for dependency in dependencies:
                add(dependency)

    if not isinstance(payload, dict):
        return claims
    add(payload.get("claim"))
    add_task(payload)
    add_task(payload.get("task"))
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            add_task(task)
    nested = payload.get("task")
    if isinstance(nested, dict) and isinstance(nested.get("tasks"), list):
        for task in nested["tasks"]:
            add_task(task)
    catalog = payload.get("dependency_catalog")
    if isinstance(catalog, dict):
        for dependency in catalog.values():
            add(dependency)
    return claims


def _contains_dimension_ref(payload):
    for claim in _claim_objects(payload):
        bindings = claim.get("bindings")
        if isinstance(bindings, list) and any(
                isinstance(binding, dict) and _DIMENSION_REF in binding
                for binding in bindings):
            return True
    return False


def _binding_dimensions(binding):
    if (not isinstance(binding, dict)
            or not isinstance(binding.get("ref_id"), str) or not binding["ref_id"]
            or _DIMENSION_REF in binding
            or any(not isinstance(binding.get(key), str) or not binding[key]
                   for key in DIMENSIONS)):
        return None
    return {key: deepcopy(binding[key]) for key in DIMENSIONS}


def _binding_dimension_counts(payload, counts):
    for claim in _claim_objects(payload):
        bindings = claim.get("bindings")
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            dimensions = _binding_dimensions(binding)
            if dimensions is not None:
                fingerprint = "binding-dim." + digest(dimensions)
                prior = counts.get(fingerprint)
                if prior is not None and prior[1] != dimensions:
                    raise ValueError(f"conflicting binding dimensions for {fingerprint}")
                counts[fingerprint] = (prior[0] + 1 if prior else 1, dimensions)


def _project_binding_dimensions(payload, catalog, repeated_refs):
    for claim in _claim_objects(payload):
        bindings = claim.get("bindings")
        if not isinstance(bindings, list):
            continue
        projected_bindings = []
        for binding in bindings:
            dimensions = _binding_dimensions(binding)
            if dimensions is None:
                projected_bindings.append(binding)
                continue
            fingerprint = "binding-dim." + digest(dimensions)
            if fingerprint not in repeated_refs:
                projected_bindings.append(binding)
                continue
            ref = repeated_refs[fingerprint]
            if ref in catalog and catalog[ref] != dimensions:
                raise ValueError(f"conflicting binding dimensions for {ref}")
            catalog[ref] = dimensions
            projected = {key: item for key, item in binding.items() if key not in DIMENSIONS}
            projected[_DIMENSION_REF] = ref
            projected_bindings.append(projected)
        claim["bindings"] = projected_bindings
    return payload


def project_review_payload(payload):
    """Pool repeated task contexts and binding tuples when their net wire cost falls.

    The input is never mutated. Catalog IDs are deterministic local references ordered by content digests,
    catalog key order is sorted, and task/binding list order is preserved. Each
    projection is accepted only when its compact JSON plus required decoder
    instruction is smaller than the representation it replaces.
    """
    original = deepcopy(payload)
    if not isinstance(original, dict):
        return original
    if _CONTEXT_CATALOG in original or _DIMENSION_CATALOG in original:
        return original

    candidate = _pool_context_values(original)

    if not _contains_dimension_ref(candidate):
        dimension_candidate = deepcopy(candidate)
        catalog = {}
        counts = {}
        _binding_dimension_counts(dimension_candidate, counts)
        repeated = {fingerprint: dimensions for fingerprint, (count, dimensions) in counts.items()
                    if count >= 2}
        repeated_refs = {fingerprint: f"d{index}"
                         for index, fingerprint in enumerate(sorted(repeated))}
        dimension_candidate = _project_binding_dimensions(dimension_candidate, catalog, repeated_refs)
        if catalog and _DIMENSION_CATALOG not in dimension_candidate:
            dimension_candidate[_DIMENSION_CATALOG] = {ref: catalog[ref]
                                                       for ref in sorted(catalog)}
            current_cost = (_json_bytes(candidate)
                            + len(review_wire_instructions(candidate).encode("utf-8")))
            projected_cost = (_json_bytes(dimension_candidate)
                              + len(review_wire_instructions(dimension_candidate).encode("utf-8")))
            if projected_cost < current_cost:
                candidate = dimension_candidate
    return candidate


def decode_review_payload(payload):
    """Expand catalogs into a deep copy of the original review payload."""
    result = deepcopy(payload)
    if not isinstance(result, dict):
        return result

    has_context_catalog = _CONTEXT_CATALOG in result
    context_catalog = result.pop(_CONTEXT_CATALOG, {})
    if not isinstance(context_catalog, dict):
        raise ValueError("context_value_catalog must be an object")
    for row in _task_rows(result) if has_context_catalog else []:
        if _CONTEXT_REFS not in row:
            continue
        refs = row.pop(_CONTEXT_REFS)
        context = row.get("context")
        if not isinstance(refs, dict) or not isinstance(context, dict):
            raise ValueError("invalid review context references")
        for field, ref in refs.items():
            if (not isinstance(field, str) or not isinstance(ref, str)
                    or ref not in context_catalog or field in context):
                raise ValueError("invalid review context reference")
            context[field] = deepcopy(context_catalog[ref])

    if _DIMENSION_CATALOG not in result:
        return result
    dimension_catalog = result.pop(_DIMENSION_CATALOG)
    if not isinstance(dimension_catalog, dict):
        raise ValueError("binding_dimension_catalog must be an object")

    for claim in _claim_objects(result):
        bindings = claim.get("bindings")
        if not isinstance(bindings, list):
            continue
        expanded_bindings = []
        for binding in bindings:
            if not isinstance(binding, dict) or _DIMENSION_REF not in binding:
                expanded_bindings.append(binding)
                continue
            binding = deepcopy(binding)
            ref = binding.pop(_DIMENSION_REF)
            if not isinstance(ref, str):
                raise ValueError("invalid binding dimension reference")
            dimensions = dimension_catalog.get(ref)
            if (not isinstance(dimensions, dict)
                    or set(dimensions) != set(DIMENSIONS)
                    or any(key in binding for key in DIMENSIONS)):
                raise ValueError("invalid binding dimension reference")
            binding.update(deepcopy(dimensions))
            expanded_bindings.append(binding)
        claim["bindings"] = expanded_bindings
    return result
