"""Pure data helpers for durable evidence-recovery work records.

These records track operator work only. Findings and receipts remain canonical;
search outcomes and submitted source IDs never resolve a finding.
"""
from copy import deepcopy

from .schema import digest


_REQUEST_FIELDS = frozenset({
    "proposition", "refs", "entity", "metric", "period", "unit", "basis", "scope",
    "known_data_range",
})
_DIMENSION_FIELDS = ("entity", "metric", "period", "unit", "basis", "scope")
_SEARCH_OUTCOMES = frozenset({"found", "not_found", "unavailable"})
_RECORD_FIELDS = frozenset({
    "request_id", "finding_id", "claim_ids", "claim_hashes", "issue", "evidence_request",
    "search_results", "submitted_source_ids", "resolution_status",
})


def _identifier(value, label):
    if (not isinstance(value, str) or not value or value.strip() != value
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"{label} must be a nonempty exact ID")
    return value


def _claims_by_id(claims):
    if isinstance(claims, dict):
        rows = claims.items()
    elif isinstance(claims, (list, tuple)):
        rows = ((claim.get("claim_id"), claim) for claim in claims if isinstance(claim, dict))
    else:
        raise ValueError("claims must be a Claim sequence or ID mapping")
    result = {}
    for key, claim in rows:
        if not isinstance(claim, dict):
            raise ValueError("each Claim must be an object")
        claim_id = _identifier(claim.get("claim_id", key), "claim_id")
        if isinstance(claims, dict) and key != claim_id:
            raise ValueError("Claim mapping key must match claim_id")
        if claim_id in result:
            raise ValueError("duplicate claim_id")
        result[claim_id] = claim
    return result


def _finding_claim_ids(finding):
    claim_ids = finding.get("claim_ids")
    if claim_ids is None and finding.get("claim_id") is not None:
        claim_ids = [finding["claim_id"]]
    if not isinstance(claim_ids, list) or not claim_ids:
        raise ValueError("evidence finding requires explicit claim_ids")
    normalized = [_identifier(claim_id, "claim_id") for claim_id in claim_ids]
    if len(set(normalized)) != len(normalized):
        raise ValueError("claim_ids must be unique")
    return normalized


def _normalize_evidence_request(request):
    if not isinstance(request, dict) or set(request) - _REQUEST_FIELDS:
        raise ValueError("evidence_request must be a typed object")
    proposition = request.get("proposition", "")
    if not isinstance(proposition, str) or not proposition.strip():
        raise ValueError("evidence_request proposition is required")
    refs = request.get("refs", [])
    if not isinstance(refs, list):
        raise ValueError("evidence_request refs must be an array")
    refs = [_identifier(ref, "evidence ref") for ref in refs]
    if len(set(refs)) != len(refs):
        raise ValueError("evidence_request refs must be unique")
    dimensions = {}
    for field in _DIMENSION_FIELDS:
        value = request.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"evidence_request {field} must be text or blank")
        dimensions[field] = value
    known_range = request.get("known_data_range", {})
    if known_range is None:
        known_range = {}
    if not isinstance(known_range, dict) or set(known_range) - {"start", "end"}:
        raise ValueError("evidence_request known_data_range must contain start/end bounds")
    bounds = {}
    for field in ("start", "end"):
        value = known_range.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"evidence_request known_data_range {field} must be text or blank")
        bounds[field] = value
    return dict(proposition=proposition, refs=refs, **dimensions, known_data_range=bounds)


def normalize_evidence_request(request):
    """Public typed normalization used by review-result contracts."""
    return _normalize_evidence_request(request)


def _request_id(finding_id):
    return "evidence." + digest({"finding_id": finding_id})


def validate_work_log(records):
    """Validate and copy a JSON-compatible evidence-work log."""
    if not isinstance(records, list):
        raise ValueError("evidence work log must be an array")
    normalized = []
    finding_ids, request_ids = set(), set()
    for raw in records:
        if (not isinstance(raw, dict) or set(raw) - _RECORD_FIELDS
                or not (_RECORD_FIELDS - {"evidence_request"}) <= set(raw)):
            raise ValueError("evidence work record has an invalid shape")
        row = deepcopy(raw)
        finding_id = _identifier(row["finding_id"], "finding_id")
        request_id = _identifier(row["request_id"], "request_id")
        if request_id != _request_id(finding_id):
            raise ValueError("request_id does not match finding occurrence")
        if finding_id in finding_ids or request_id in request_ids:
            raise ValueError("duplicate evidence-work identity")
        finding_ids.add(finding_id)
        request_ids.add(request_id)
        claim_ids = row["claim_ids"]
        if (not isinstance(claim_ids, list) or not claim_ids
                or any(not isinstance(cid, str) for cid in claim_ids)
                or len(set(claim_ids)) != len(claim_ids)):
            raise ValueError("evidence work requires unique explicit claim_ids")
        claim_ids = [_identifier(cid, "claim_id") for cid in claim_ids]
        if not isinstance(row["claim_hashes"], dict) or set(row["claim_hashes"]) != set(claim_ids):
            raise ValueError("claim_hashes must cover the exact claim_ids")
        for claim_hash in row["claim_hashes"].values():
            if (not isinstance(claim_hash, str) or len(claim_hash) != 64
                    or any(char not in "0123456789abcdef" for char in claim_hash)):
                raise ValueError("invalid Claim version hash")
        if not isinstance(row["issue"], str):
            raise ValueError("finding issue must be text")
        if "evidence_request" in row:
            request = _normalize_evidence_request(row["evidence_request"])
            if request != row["evidence_request"]:
                raise ValueError("evidence_request is not in canonical shape")
        if not isinstance(row["search_results"], list):
            raise ValueError("search_results must be an array")
        for result in row["search_results"]:
            if (not isinstance(result, dict) or set(result) != {"outcome", "query", "details"}
                    or not isinstance(result.get("outcome"), str)
                    or result["outcome"] not in _SEARCH_OUTCOMES
                    or not isinstance(result.get("query"), str)
                    or not isinstance(result.get("details"), str)):
                raise ValueError("invalid evidence search result")
        source_ids = row["submitted_source_ids"]
        if not isinstance(source_ids, list):
            raise ValueError("submitted_source_ids must be an array")
        source_ids = [_identifier(source_id, "source_id") for source_id in source_ids]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("submitted_source_ids must be unique")
        if row["resolution_status"] != "unresolved":
            raise ValueError("evidence work cannot resolve its canonical finding")
        normalized.append(row)
    return normalized


def ensure_evidence_request(work_log, finding, claims):
    """Return ``(new_log, record)`` for a finding occurrence, reusing its ID.

    Finding identity comes only from finding_id. Claim targets come only from
    explicit claim_ids/claim_id fields; issue prose is preserved, never parsed.
    """
    records = validate_work_log(work_log)
    if not isinstance(finding, dict):
        raise ValueError("finding must be an object")
    finding_id = _identifier(finding.get("finding_id"), "finding_id")
    claim_ids = _finding_claim_ids(finding)
    by_id = _claims_by_id(claims)
    if set(claim_ids) - by_id.keys():
        raise ValueError("evidence finding references an unknown Claim")
    existing = next((row for row in records if row["finding_id"] == finding_id), None)
    if existing is not None:
        if existing["claim_ids"] != claim_ids:
            raise ValueError("finding occurrence changed its explicit Claim targets")
        return records, deepcopy(existing)
    issue = finding.get("issue", "")
    if not isinstance(issue, str):
        raise ValueError("finding issue must be text")
    record = dict(request_id=_request_id(finding_id), finding_id=finding_id,
        claim_ids=claim_ids, claim_hashes={cid: digest(by_id[cid]) for cid in claim_ids},
        issue=issue, search_results=[], submitted_source_ids=[], resolution_status="unresolved")
    if "evidence_request" in finding:
        record["evidence_request"] = _normalize_evidence_request(finding["evidence_request"])
    records.append(record)
    return validate_work_log(records), deepcopy(record)


def _update_record(work_log, request_id):
    records = validate_work_log(work_log)
    request_id = _identifier(request_id, "request_id")
    for index, row in enumerate(records):
        if row["request_id"] == request_id:
            return records, index
    raise ValueError("unknown evidence request_id")


def record_search_result(work_log, request_id, outcome, *, query="", details=""):
    """Append an attempt outcome; even not_found/unavailable remain unresolved."""
    if not isinstance(outcome, str) or outcome not in _SEARCH_OUTCOMES:
        raise ValueError("search outcome must be found, not_found, or unavailable")
    if not isinstance(query, str) or not isinstance(details, str):
        raise ValueError("search query and details must be text")
    records, index = _update_record(work_log, request_id)
    records[index]["search_results"].append(dict(outcome=outcome, query=query, details=details))
    return validate_work_log(records)


def record_submitted_source(work_log, request_id, source_id):
    """Record a submitted source ID without treating it as accepted evidence."""
    source_id = _identifier(source_id, "source_id")
    records, index = _update_record(work_log, request_id)
    if source_id not in records[index]["submitted_source_ids"]:
        records[index]["submitted_source_ids"].append(source_id)
    return validate_work_log(records)


def is_request_stale(record, claims):
    """Whether any target Claim differs from the version captured at creation."""
    row = validate_work_log([record])[0]
    by_id = _claims_by_id(claims)
    return any(cid not in by_id or digest(by_id[cid]) != row["claim_hashes"][cid]
               for cid in row["claim_ids"])
