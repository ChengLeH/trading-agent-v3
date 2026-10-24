"""Structural contracts for LLM-authored semantic judgments.

Python checks coverage, IDs and consistency of the model's answers. It never
infers meaning from prose, invents edges or decides source entailment itself.
"""
from copy import deepcopy
from .schema import Claim, claim_index, id_list

SEMANTIC_REVIEW_CONTRACT = "v4-semantic-review/4"
SEMANTIC_RESOLUTION_ROUTES = frozenset({
    "claim_patch", "evidence_needed", "transport_recovery", "role_redeliberation",
    "human_decision", "uncertain",
})
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
            "properties": {
                "issue": {"type": "string", "minLength": 1},
                "resolution_route": {"type": "string", "enum": sorted(SEMANTIC_RESOLUTION_ROUTES)},
                "resolution_reason": {"type": "string", "minLength": 1},
                "evidence_request": {
                    "type": "object", "required": ["proposition"],
                    "properties": {
                        "proposition": {"type": "string", "minLength": 1},
                        "refs": {"type": "array", "items": {"type": "string", "minLength": 1}},
                        **{field: {"type": "string"} for field in
                           ("entity", "metric", "period", "unit", "basis", "scope")},
                        "known_data_range": {"type": "object", "properties": {
                            "start": {"type": "string"}, "end": {"type": "string"},
                        }},
                    },
                },
            },
        }},
        "clarity_notes": {"type": "array", "items": {
            "type": "object", "required": ["issue", "reason"],
            "properties": {"issue": {"type": "string", "minLength": 1},
                           "reason": {"type": "string", "minLength": 1}},
        }},
    },
}

EVIDENCE_INSTRUCTIONS = """證據義務按主張的實際語義判斷，不由 claim_type 或 assertion.kind 標籤決定。
標籤只是作者的描述，不能讓任何 Claim 跳過語義審查，也不能因標錯類型而單獨判錯。
refs 為空不等於主張錯誤，也不等於無須證據；有引用也不等於證據足夠。
外部可驗證事實需要能支持它的來源；不得用常識、模型記憶或改貼 interpretation/forecast 等標籤代替證據。
只有上游 Claim 的推論可以由該前提支持，不必為每個結論重貼原始來源；上游仍須獨立驗證，
不得將上游的猜測、資料不足或流程自述變成已證實的外部事實。
資料限制須限定到實際可見的資料範圍；缺資料不等於數值為零、事件未發生或公司未揭露。
context.evidence_scope 是本次已匯入資料的精簡目錄中繼資料，不含來源全文、原始角色輸入或傳輸紀錄。
目錄只能支持其明示範圍內的結構化資料觀察，不能證明來源全文缺資料、原始文件未提供或傳輸曾截斷。
fact_concepts=null 表示目錄細節因大小未附上，只有數量與指紋，不能把未列出的概念當成不存在。
若聲稱流程中斷、內容遺失或截斷，須有實際流程／傳輸紀錄支持；其他 Claim 的相同自述不是獨立證明。
缺少判斷所需依據時回 SUSPICIOUS，已知與證據矛盾回 FAIL，不能因它是後設說明而免驗或猜 PASS。
將問題寫入既有 findings，點名哪個命題、缺什麼依據或與什麼矛盾；不另造來源或替作者改寫後判通過。
"""

REFERENCE_INSTRUCTIONS = EVIDENCE_INSTRUCTIONS + """逐引用語義驗收：每個 refs/fact_refs/dependencies 聯集 ID 恰有一筆 reference_checks，
欄位為 ref_id/relation(SUPPORTS|CONTRADICTS|INSUFFICIENT)/attributed_assertion/source_evidence/reason。
逐一理解 text 對該來源實際聲稱的內容，再與 assertion/binding 及實際來源比對。
attributed_assertion 必須反映原文歸因，不能偷偷把錯誤原文改成正確說法再判 SUPPORTS。
source_evidence 必須指出 context 內的實際證據；不相關的真實摘錄不構成支持。
引用用於反證時，檢查原文對反證內容的歸因是否正確，不要求每個來源獨立支持整個投資結論。
來源矛盾回 CONTRADICTS 且 verdict=FAIL；證據不足回 INSUFFICIENT，禁止 PASS。
無任何引用或相依時可省略 reference_checks 或回 []，仍需完整審查 Claim 語義；有任何引用時不可省略或少審。"""

EVIDENCE_REQUEST_INSTRUCTIONS = """完成當前層級的語義判斷後，若原命題需要補足來源證據才可標 resolution_route="evidence_needed"；只改文字／口徑能修正的問題使用 claim_patch。
先區分「目前局部 context 沒附證據」、「已匯入但 Claim 未引用」與「需要補外部資料」。能在提供的內容中確認既有 canonical ID 且只需補引用時，使用 claim_patch 並點名該 ID，不要求重複匯入。
evidence_scope 僅是目錄中繼資料，未出現不證明整個 catalog 不存在。局部 context 看不到必要證據時仍可提出 evidence_needed，但 resolution_reason 必須限定可見範圍；請主 Agent 先核對 canonical catalog，已有來源就補引用，確實未匯入才搜尋及新增來源。不可要求本次審查員自行呼叫工具或假裝看過完整 catalog。
evidence_needed 須附 resolution_reason 及 evidence_request：proposition 用一句話寫明待查命題，refs 只列你在 context 中能精確識別的相關 ID，entity/metric/period/unit/basis/scope 與 known_data_range 只填已知值，未知留空。不可從 finding prose 猜 ID、不可捏造來源或把搜尋結果當證據。補證據紀錄不代表問題已解決；仍須修訂 Claim，並完成必要重驗與 Global。
"""


def review_ref_ids(claim):
    return list(dict.fromkeys(claim.get("refs", []) + claim.get("fact_refs", []) + claim.get("dependencies", [])))


def semantic_review_schema(claim, *, reference_ids=None):
    schema = deepcopy(SEMANTIC_REVIEW_SCHEMA)
    all_refs = review_ref_ids(claim)
    refs = all_refs if reference_ids is None else list(reference_ids)
    if len(set(refs)) != len(refs) or set(refs) - set(all_refs):
        raise ValueError("semantic review schema reference IDs must be unique Claim references")
    schema["properties"]["claim_id"]["enum"] = [claim["claim_id"]]
    request_refs = schema["properties"]["findings"]["items"]["properties"]["evidence_request"]["properties"]["refs"]
    if all_refs:
        request_refs["items"]["enum"] = all_refs
    else:
        request_refs["maxItems"] = 0
    checks = schema["properties"]["reference_checks"]
    checks.update(minItems=len(refs), maxItems=len(refs))
    if refs:
        checks["items"]["properties"]["ref_id"]["enum"] = refs
    else:
        schema["required"].remove("reference_checks")
    return schema


def require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


class MissingReferenceChecks(ValueError):
    def __init__(self, missing_ref_ids):
        self.missing_ref_ids = list(missing_ref_ids)
        super().__init__(f"every direct source and dependency requires semantic reference checking; missing_ref_ids={self.missing_ref_ids}")


def normalize_reference_checks(expected_refs, result, *, allow_missing=False):
    rows = result.get("reference_checks")
    if not expected_refs and rows is None:
        return []
    if rows is None and allow_missing:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("reference_checks must be an array when supplied; nonempty refs require checks")
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
    missing = [ref for ref in expected_refs if ref not in seen]
    if missing and not allow_missing:
        raise MissingReferenceChecks(missing)
    return deepcopy(rows)


def normalize_semantic_finding_fields(finding, *, allowed_evidence_refs=None):
    """Validate optional typed finding routes without classifying its prose."""
    from .evidence_requests import normalize_evidence_request

    row = deepcopy(finding)
    route = row.get("resolution_route")
    if route is not None and (not isinstance(route, str) or route not in SEMANTIC_RESOLUTION_ROUTES):
        raise ValueError("invalid semantic finding resolution_route")
    if route is not None and route != "claim_patch":
        require_text(row.get("resolution_reason"), "finding resolution_reason")
    has_request = "evidence_request" in row
    if route == "evidence_needed" and not has_request:
        raise ValueError("evidence_needed requires a typed evidence_request")
    if has_request:
        if route != "evidence_needed":
            raise ValueError("evidence_request requires resolution_route=evidence_needed")
        row["evidence_request"] = normalize_evidence_request(row["evidence_request"])
        if allowed_evidence_refs is not None:
            unknown = set(row["evidence_request"]["refs"]) - set(allowed_evidence_refs)
            if unknown:
                raise ValueError(f"evidence_request references unknown IDs: {sorted(unknown)}")
    return row


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
