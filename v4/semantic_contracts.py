"""Structural contracts for LLM-authored semantic judgments.

Python checks coverage, IDs and consistency of the model's answers. It never
infers meaning from prose, invents edges or decides source entailment itself.
"""
from copy import deepcopy
from .schema import Claim, claim_index, id_list

SEMANTIC_REVIEW_CONTRACT = "v4-semantic-review/2"
SEMANTIC_REVIEW_SCHEMA = {
    "type": "object",
    "required": ["claim_id", "verdict", "reference_checks", "findings"],
    "properties": {
        "claim_id": {"type": "string", "minLength": 1},
        "verdict": {"type": "string", "enum": ["PASS", "SUSPICIOUS", "FAIL"]},
        "reference_checks": {"type": "array", "items": {
            "type": "object",
            "required": ["ref_id", "relation", "attributed_assertion", "source_evidence", "reason"],
            "properties": {
                "ref_id": {"type": "string", "minLength": 1},
                "relation": {"type": "string", "enum": ["SUPPORTS", "CONTRADICTS", "INSUFFICIENT"]},
                **{k: {"type": "string", "minLength": 1}
                   for k in ("attributed_assertion", "source_evidence", "reason")},
            },
        }},
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["issue"],
            "properties": {"issue": {"type": "string", "minLength": 1}},
        }},
    },
}

REFERENCE_INSTRUCTIONS = """逐引用語義驗收：每個 refs/fact_refs/dependencies 聯集 ID 恰有一筆 reference_checks，
欄位為 ref_id/relation(SUPPORTS|CONTRADICTS|INSUFFICIENT)/attributed_assertion/source_evidence/reason。
逐一理解 text 對該來源實際聲稱的內容，再與 assertion/binding 及實際來源比對。
attributed_assertion 必須反映原文歸因，不能偷偷把錯誤原文改成正確說法再判 SUPPORTS。
source_evidence 必須指出 context 內的實際證據；不相關的真實摘錄不構成支持。
引用用於反證時，檢查原文對反證內容的歸因是否正確，不要求每個來源獨立支持整個投資結論。
來源矛盾回 CONTRADICTS 且 verdict=FAIL；證據不足回 INSUFFICIENT，禁止 PASS。
無任何引用或相依時 reference_checks=[]，仍需完整審查 Claim 語義。"""


def review_ref_ids(claim):
    return list(dict.fromkeys(claim.get("refs", []) + claim.get("fact_refs", []) + claim.get("dependencies", [])))


def semantic_review_schema(claim):
    schema = deepcopy(SEMANTIC_REVIEW_SCHEMA)
    refs = review_ref_ids(claim)
    schema["properties"]["claim_id"]["enum"] = [claim["claim_id"]]
    checks = schema["properties"]["reference_checks"]
    checks.update(minItems=len(refs), maxItems=len(refs))
    if refs:
        checks["items"]["properties"]["ref_id"]["enum"] = refs
    return schema


def require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def normalize_reference_checks(expected_refs, result):
    rows = result.get("reference_checks")
    if not isinstance(rows, list):
        raise ValueError("reference_checks array required, including [] for no refs")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("reference check must be an object")
        ref = row.get("ref_id")
        if not isinstance(ref, str) or ref not in expected_refs or ref in seen:
            raise ValueError("unknown or duplicate reference check")
        seen.add(ref)
        relation = row.get("relation")
        if not isinstance(relation, str) or relation not in {"SUPPORTS", "CONTRADICTS", "INSUFFICIENT"}:
            raise ValueError("invalid reference semantic relation")
        for key in ("attributed_assertion", "source_evidence", "reason"):
            require_text(row.get(key), f"reference_checks.{key}")
        if relation != "SUPPORTS" and result["verdict"] == "PASS":
            raise ValueError("PASS cannot override unsupported reference attribution")
        if relation == "CONTRADICTS" and result["verdict"] != "FAIL":
            raise ValueError("contradicted attribution requires FAIL")
    if seen != set(expected_refs):
        raise ValueError(f"every direct source and dependency requires semantic reference checking; missing_ref_ids={sorted(set(expected_refs) - seen)}")
    return deepcopy(rows)


def normalize_dependency_review(claims, result, *, allow_partial=False, expected_claim_ids=None):
    if not isinstance(result, dict) or not isinstance(result.get("findings"), list):
        raise ValueError("global review object requires findings and dependency_reviews")
    rows = result.get("dependency_reviews")
    if not isinstance(rows, list):
        raise ValueError("dependency_reviews array required")
    index = claim_index(claims)
    expected = set(index) if expected_claim_ids is None else set(id_list(list(expected_claim_ids), "expected dependency review IDs"))
    if expected - index.keys():
        raise ValueError("unknown expected dependency review IDs")
    candidate = dict(index)
    seen, findings, patches, unresolved_findings = set(), [], [], []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("dependency review must be an object")
        cid = row.get("claim_id")
        if not isinstance(cid, str) or cid not in expected or cid in seen:
            raise ValueError(f"unknown, duplicate or unrequested dependency review claim_id: {cid}")
        seen.add(cid)
        deps = id_list(row.get("required_dependencies"), "required_dependencies")
        if cid in deps or set(deps) - index.keys():
            raise ValueError("invalid semantic dependency target")
        if type(row.get("unresolved")) is not bool:
            raise ValueError("dependency review unresolved must be boolean")
        require_text(row.get("reason"), "dependency review reason")
        if row["unresolved"]:
            unresolved_findings.append(dict(
                claim_id=cid, category="dependency_unresolved",
                issue="LLM could not establish the claim's semantic dependencies",
                evidence=row["reason"], dependency_review=deepcopy(row),
                declared_dependencies=deepcopy(index[cid].get("dependencies", [])),
                required_dependencies=deepcopy(deps),
                suggested_fix="clarify or correct this Claim and its premises; revalidate it and descendants, then repeat the global audit"))
            continue  # uncertainty is a repair finding, not a malformed response
        old = index[cid].get("dependencies", [])
        missing = [dep for dep in deps if dep not in old]
        if missing:
            merged = old + missing  # semantic repair is additive only
            candidate[cid] = {**index[cid], "dependencies": merged}
            patches.append(dict(claim_id=cid, dependencies=merged))
            findings.append(dict(claim_id=cid, category="dependency_gap", required_dependencies=deps,
                                 issue="LLM dependency audit identified undeclared premises", evidence=row["reason"],
                                 suggested_fix="add the exact semantic dependency IDs, then revalidate descendants"))
    missing = [cid for cid in index if cid in expected and cid not in seen]
    if missing and not allow_partial:
        raise ValueError(f"every Claim requires semantic dependency completeness review; missing_claim_ids={missing}")
    if patches:
        claim_index(list(candidate.values()))  # reject cyclic combined repairs transactionally
    return dict(dependency_reviews=deepcopy(rows), dependency_findings=findings, dependency_patches=patches,
                unresolved_dependency_findings=unresolved_findings,
                missing_dependency_claim_ids=missing,
                reviewed_dependency_claim_ids=[cid for cid in index if cid in seen])


def validate_revision_history(claims, history):
    """Require original objects for every changed ID, including old checkpoints."""
    current = claim_index(claims)
    latest = {}
    if not isinstance(history, list):
        raise ValueError("revision history must be an array")
    for entry in history:
        if not isinstance(entry, dict) or not isinstance(entry.get("patches"), list) or not entry["patches"]:
            raise ValueError("revision history requires patches and original Claim objects")
        patch_ids = id_list([p.get("claim_id") for p in entry["patches"] if isinstance(p, dict)], "history patch IDs")
        if len(patch_ids) != len(entry["patches"]):
            raise ValueError("invalid historical patch")
        patch_by_id = {p["claim_id"]: p for p in entry["patches"]}
        if "format" in entry and entry["format"] != "v4-revision/2":
            raise ValueError("unsupported revision history format")
        if entry.get("format") == "v4-revision/2" or "invalidated_global_findings" in entry:
            invalidated = entry.get("invalidated_global_findings")
            if not isinstance(invalidated, list):
                raise ValueError("revision history requires invalidated_global_findings array")
            finding_ids = set()
            for finding in invalidated:
                if not isinstance(finding, dict):
                    raise ValueError("invalid historical global finding")
                require_text(finding.get("finding_id"), "historical finding_id")
                require_text(finding.get("issue"), "historical finding issue")
                if finding["finding_id"] in finding_ids:
                    raise ValueError("duplicate historical finding_id")
                finding_ids.add(finding["finding_id"])
                ids = id_list(finding.get("claim_ids", []), "historical finding claim IDs")
                if "claim_id" in finding:
                    require_text(finding["claim_id"], "historical finding claim_id")
                    ids = list(dict.fromkeys([finding["claim_id"]] + ids))
                if not ids or set(ids) - current.keys() or not set(ids).intersection(patch_ids):
                    raise ValueError("historical finding must target a patched Claim")
        changes = entry.get("claim_changes")
        if not isinstance(changes, list) or len(changes) != len(patch_ids):
            raise ValueError("history lacks original Claim objects; regenerate roles from manifest")
        seen = set()
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("history change requires before and after Claim objects")
            cid = change.get("claim_id")
            if cid not in patch_ids or cid in seen or cid not in current:
                raise ValueError("invalid historical claim_id")
            seen.add(cid)
            before = Claim.from_dict(change.get("before")).to_dict()
            after = Claim.from_dict(change.get("after")).to_dict()
            if before["claim_id"] != cid or after["claim_id"] != cid or before["author_role"] != after["author_role"]:
                raise ValueError("historical Claim identity mismatch")
            if cid in latest and latest[cid] != before:
                raise ValueError("broken historical Claim chain")
            expected_after = Claim.from_dict({**before, **patch_by_id[cid]}).to_dict()
            if after != expected_after:
                raise ValueError("historical patch and Claim objects disagree")
            latest[cid] = after
    for cid, after in latest.items():
        if current[cid] != after:
            raise ValueError("current Claim disagrees with revision history")
