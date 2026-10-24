"""Bounded LLM request packing; atomic judgments remain per Claim object.

Only explicit typed IDs are pooled. No source/Claim prose is interpreted,
filtered, summarized or truncated by this module.
"""
from copy import deepcopy
import json
from .cloud import structured_prompt
from .schema import digest
from .review_diagnostics import DIAGNOSTICS_INSTRUCTIONS
from .financial_review import FINANCIAL_REVIEW_CONTRACT
from .semantic_contracts import REFERENCE_INSTRUCTIONS, SEMANTIC_REVIEW_CONTRACT, review_ref_ids, semantic_review_schema

BATCH_INSTRUCTIONS = """你是 V4 語義驗證員。對 tasks 中每一條 Claim 分別完整審查，不得省略或以整批多數決。
只根據輸入證據，理解每條 text 的語義，對照 assertion、binding、來源及相依論述。
檢查主體、指標、期間、scope、unit、basis、日期、數值、來源扭曲及舊相依狀態。
投資觀點不同不構成錯誤；證據不足回 SUSPICIOUS，不可猜 PASS。禁止使用外部工具。
resolved_ref_ids 指向 evidence_catalog；dependency_claim_ids 指向 dependency_catalog；兩者均保留完整物件。
每條只使用自己列出的來源與相依 ID；其他 task 或未列出的共用來源不是它的證據，不得偷偷借用。
每條只把會改變事實、歸因、因果、範圍、條件或決策的問題列入 findings；輕微措辭不清但合理解讀不變，
可放 clarity_notes 或忽略。若含糊可能改變主張或動作，仍須列為 SUSPICIOUS/FAIL。
不同角色、立場或條件化情境本身不構成錯誤。每條的 findings 與 clarity_notes 只指向該 claim_id；PASS 的 findings=[]。
只回嚴格 JSON object：{"results":[{"claim_id":"原ID","verdict":"PASS|SUSPICIOUS|FAIL",
"reference_checks":[{"ref_id":"引用ID","relation":"SUPPORTS|CONTRADICTS|INSUFFICIENT",
"attributed_assertion":"原文歸因命題","source_evidence":"實際來源證據","reason":"理由"}],
"findings":[{"issue":"問題","evidence":"證據","suggested_fix":"必要修正"}],
"clarity_notes":[{"issue":"輕微措辭提醒","reason":"不改變主張的原因"}]}]}。
逐條回報，不能把不相關真實來源綁到錯誤命題而判 PASS。
""" + REFERENCE_INSTRUCTIONS + '\n' + FINANCIAL_REVIEW_CONTRACT + '\n' + DIAGNOSTICS_INSTRUCTIONS


def batch_schema(tasks):
    count = len(tasks)
    return dict(type="object", required=["results"], properties={
        "results": dict(type="array", minItems=count, maxItems=count,
                        items=dict(anyOf=[semantic_review_schema(task.claim) for task in tasks]))})


def _request(tasks, *, layer, verification_epoch, first_reviews):
    evidence, dependencies, rows = {}, {}, []
    def pool(catalog, key, value):
        if key in catalog and catalog[key] != value:
            raise ValueError(f"conflicting structured review context for {key}")
        catalog[key] = deepcopy(value)
    for task in tasks:
        local = task.review_dict()
        for ref, value in local["resolved_refs"].items():
            pool(evidence, ref, value)
        for claim in local["dependency_claims"]:
            pool(dependencies, claim["claim_id"], claim)
        row = dict(claim=deepcopy(task.claim), resolved_ref_ids=list(local["resolved_refs"]),
                   verification_input_hash=local["verification_input_hash"],
                   dependency_claim_ids=[c["claim_id"] for c in local["dependency_claims"]],
                   unresolved_refs=local["unresolved_refs"], context=task.context,
                   expected_reference_ids=review_ref_ids(task.claim))
        if layer == "deep":
            first = first_reviews[task.claim_id]
            row["first_review"] = {k: first[k] for k in ("claim_id", "verdict", "findings")}
            row["first_review_hash"] = digest(first)
        rows.append(row)
    payload = dict(response_contract=SEMANTIC_REVIEW_CONTRACT, review_layer=layer,
                   verification_epoch=verification_epoch, tasks=rows,
                   evidence_catalog=evidence, dependency_catalog=dependencies)
    instruction = BATCH_INSTRUCTIONS
    if layer == "deep":
        instruction += "\n這是第二層深度對抗覆核。每條都重新閱讀原始證據，獨立挑戰第一層 findings，不可直接照抄。"
    ids = [task.claim_id for task in tasks]
    return dict(request_id=f"{layer}.batch.{digest(ids)}", claim_ids=ids,
                prompt=instruction + "\n\n```json\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n```",
                response_schema=batch_schema(tasks))


def pack_review_requests(tasks, *, batch_size, max_bytes, verification_epoch, layer="atomic", first_reviews=None):
    if type(batch_size) is not int or not 1 <= batch_size <= 16:
        raise ValueError("review_batch_size must be 1..16")
    if type(max_bytes) is not int or max_bytes < 1024:
        raise ValueError("review_batch_max_bytes must be >=1024")
    if layer not in {"atomic", "deep"}:
        raise ValueError("invalid review layer")
    if len({t.claim_id for t in tasks}) != len(tasks):
        raise ValueError("duplicate review Claim IDs")
    canonical_refs, canonical_claims = {}, {}
    for task in tasks:
        for catalog, rows in ((canonical_refs, task.resolved_refs.items()),
                              (canonical_claims, [(c["claim_id"], c) for c in [task.claim] + task.dependency_claims])):
            for key, value in rows:
                if key in catalog and catalog[key] != value:
                    raise ValueError(f"conflicting canonical review object for {key}")
                catalog[key] = value
    output, current = [], []
    def request(group):
        return _request(group, layer=layer, verification_epoch=verification_epoch, first_reviews=first_reviews or {})
    for task in tasks:
        candidate = request(current + [task])
        wire = structured_prompt(candidate["prompt"], json.dumps(candidate["response_schema"], ensure_ascii=False, separators=(",", ":")))
        if current and (len(current) >= batch_size or len(wire.encode("utf-8")) > max_bytes):
            output.append(request(current))
            current = []
        # An oversized single Claim is retained intact; transport handles its
        # long-prompt timeout. No arbitrary truncation or semantic skipping.
        current.append(task)
    if current:
        output.append(request(current))
    return output


def unpack_review_result(result, expected_ids, *, preserve_unambiguous=False):
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return {}  # contract retry must ask the LLM, never invent a verdict
    output, ambiguous = {}, set()
    for row in result["results"]:
        if not isinstance(row, dict) or not isinstance(row.get("claim_id"), str):
            continue  # unidentifiable row is a missing Claim, not prose routing
        cid = row["claim_id"]
        if cid not in expected_ids:
            if preserve_unambiguous:
                continue
            raise ValueError("unknown or duplicate batched review Claim ID")
        if cid in output or cid in ambiguous:
            if preserve_unambiguous:
                output.pop(cid, None)
                ambiguous.add(cid)
                continue
            raise ValueError("unknown or duplicate batched review Claim ID")
        output[cid] = row
    return output


def unpack_prepared_review_result(request, result, *, preserve_unambiguous=False):
    """Honor the schema issued by prepare, including intact single-item fallback."""
    ids = request['claim_ids']
    if 'results' in request['response_schema'].get('properties', {}):
        return unpack_review_result(result, ids, preserve_unambiguous=preserve_unambiguous)
    if len(ids) != 1:
        raise ValueError('single review schema requires exactly one Claim ID')
    return unpack_review_result(dict(results=[result]), ids, preserve_unambiguous=preserve_unambiguous)
