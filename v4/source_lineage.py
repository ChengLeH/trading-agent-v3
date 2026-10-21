"""Declared V4 source relationships, without certifying independence."""
from copy import deepcopy


_TEXT_FIELDS = {"origin_id", "publisher", "reason", "content_fingerprint"}
_LIST_FIELDS = {"derived_from", "evidence_refs"}
_FIELDS = _TEXT_FIELDS | _LIST_FIELDS


def normalize_source_lineage(value):
    """Validate and copy the optional source/evidence keyed annotation map."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("source_lineage must be a mapping")
    normalized = {}
    for ref_id, annotation in value.items():
        if not isinstance(ref_id, str) or not ref_id.strip():
            raise ValueError("source_lineage keys must be nonempty source or Evidence IDs")
        if not isinstance(annotation, dict):
            raise ValueError(f"source_lineage[{ref_id}] must be an object")
        extra = set(annotation) - _FIELDS
        if extra:
            raise ValueError(f"source_lineage[{ref_id}] has unsupported fields: {sorted(extra)}")
        if not annotation:
            raise ValueError(f"source_lineage[{ref_id}] must declare at least one field")
        row = {}
        for field in _TEXT_FIELDS.intersection(annotation):
            text = annotation[field]
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"source_lineage[{ref_id}].{field} must be nonempty text")
            row[field] = text
        for field in _LIST_FIELDS.intersection(annotation):
            refs = annotation[field]
            if (not isinstance(refs, list)
                    or any(not isinstance(ref, str) or not ref.strip() for ref in refs)):
                raise ValueError(f"source_lineage[{ref_id}].{field} must be a list of nonempty IDs")
            if len(refs) != len(set(refs)):
                raise ValueError(f"source_lineage[{ref_id}].{field} contains duplicate IDs")
            row[field] = list(refs)
        normalized[ref_id] = row

    visiting, done = set(), set()

    def visit(ref_id):
        if ref_id in visiting:
            raise ValueError("source_lineage derived_from cycle")
        if ref_id in done:
            return
        visiting.add(ref_id)
        for parent in normalized.get(ref_id, {}).get("derived_from", []):
            if parent in normalized:
                visit(parent)
        visiting.remove(ref_id)
        done.add(ref_id)

    for ref_id in normalized:
        visit(ref_id)
    return normalized


def validate_source_lineage(value, *, source_ids, evidence_owners, fingerprints=None):
    """Check internal references, Evidence ownership, and declared fingerprints."""
    normalized = normalize_source_lineage(value)
    source_ids = set(source_ids)
    evidence_owners = dict(evidence_owners)
    fingerprints = {} if fingerprints is None else dict(fingerprints)
    referenced = set(normalized)
    for annotation in normalized.values():
        referenced.update(annotation.get("derived_from", []))
        referenced.update(annotation.get("evidence_refs", []))
    if source_ids.intersection(evidence_owners) & referenced:
        raise ValueError("source_lineage reference IDs are ambiguous between source and Evidence")
    for evidence_id in set(evidence_owners) & referenced:
        owner_id = evidence_owners[evidence_id]
        if owner_id not in source_ids:
            raise ValueError(f"Evidence {evidence_id} has no existing source owner {owner_id}")
    known_refs = source_ids | set(evidence_owners)
    for ref_id, annotation in normalized.items():
        if ref_id not in known_refs:
            raise ValueError(f"source_lineage owner ref does not exist: {ref_id}")
        for parent in annotation.get("derived_from", []):
            if parent not in known_refs:
                raise ValueError(f"source_lineage derived_from ref does not exist: {parent}")
        for evidence_id in annotation.get("evidence_refs", []):
            if evidence_id not in evidence_owners:
                raise ValueError(f"source_lineage evidence ref does not exist: {evidence_id}")
        fingerprint = annotation.get("content_fingerprint")
        if fingerprint is not None and fingerprints.get(ref_id) != fingerprint:
            raise ValueError(f"source_lineage content_fingerprint does not match canonical content for {ref_id}")
    return deepcopy(normalized)


def validate_run_source_lineage(run, value):
    """Resolve IDs and content fingerprints from a V4 run's actual objects."""
    source_ids, fingerprints = set(), {}
    for record in getattr(run, "records", []):
        source_id = getattr(record, "id", None)
        source_hash = getattr(record, "source_hash", None)
        if isinstance(source_id, str) and source_hash is not None:
            source_ids.add(source_id)
            fingerprints[source_id] = source_hash
    evidence_owners = {}
    for evidence_id, evidence in run.evidence_store.to_dict().items():
        evidence_owners[evidence_id] = evidence.get("source_id")
        fingerprints[evidence_id] = evidence.get("source_hash", "")
    return validate_source_lineage(value, source_ids=source_ids,
                                   evidence_owners=evidence_owners,
                                   fingerprints=fingerprints)


def project_source_lineage(lineage, referenced_ids):
    """Return only declared lineage reachable from a review's explicit refs."""
    normalized = normalize_source_lineage(lineage)
    pending = list(dict.fromkeys(ref for ref in referenced_ids
                                 if isinstance(ref, str) and ref))
    included = set()
    while pending:
        ref_id = pending.pop()
        if ref_id in included:
            continue
        included.add(ref_id)
        annotation = normalized.get(ref_id)
        if annotation is not None:
            pending.extend(annotation.get("derived_from", []))
            pending.extend(annotation.get("evidence_refs", []))
    return {ref_id: deepcopy(normalized[ref_id]) for ref_id in normalized if ref_id in included}
