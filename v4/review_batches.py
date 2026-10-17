"""Bounded LLM request packing; atomic judgments remain per Claim object.

Only explicit typed IDs are pooled. No source/Claim prose is interpreted,
filtered, summarized or truncated by this module.
"""
from copy import deepcopy
import json
from .cloud import structured_prompt
from .schema import digest
from .semantic_contracts import REFERENCE_INSTRUCTIONS, SEMANTIC_REVIEW_CONTRACT, review_ref_ids, semantic_review_schema

BATCH_INSTRUCTIONS = """你是 V4 語義驗證員。對 tasks 中每一條 Claim 分別完整審查，不得省略或以整批多數決。
只根據輸入證據，理解每條 text 的語義，對照 assertion、binding、來源及相依論述。
檢查主體、指標、期間、scope、unit、basis、日期、數值、來源扭曲及舊相依狀態。
投資觀點不同不構成錯誤；證據不足回 SUSPICIOUS，不可猜 PASS。禁止使用外部工具。
resolved_ref_ids 指向 evidence_catalog；dependency_claim_ids 指向 dependency_catalog；兩者均保留完整物件。
每條只使用自己列出的來源與相依 ID；其他 task 或未列出的共用來源不是它的證據，不得偷偷借用。
每條的 findings 只指向該 claim_id；PASS 的 findings=[]，其他 verdict 必須解釋問題。
只回嚴格 JSON object：{"results":[{"claim_id":"原ID","verdict":"PASS|SUSPICIOUS|FAIL",
"reference_checks":[{"ref_id":"引用ID","relation":"SUPPORTS|CONTRADICTS|INSUFFICIENT",
"attributed_assertion":"原文歸因命題","source_evidence":"實際來源證據","reason":"理由"}],
"findings":[{"issue":"問題","evidence":"證據","suggested_fix":"必要修正"}]}]}。
逐條回報，不能把不相關真實來源綁到錯誤命題而判 PASS。
""" + REFERENCE_INSTRUCTIONS


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
        for ref, value in task.resolved_refs.items():
            pool(evidence, ref, value)
        for claim in task.dependency_claims:
            pool(dependencies, claim["claim_id"], claim)
        row = dict(claim=deepcopy(task.claim), resolved_ref_ids=list(task.resolved_refs),
                   dependency_claim_ids=[c["claim_id"] for c in task.dependency_claims],
                   unresolved_refs=task.unresolved_refs, context=task.context,
                   expected_reference_ids=review_ref_ids(task.claim))
        if layer == "deep":
            row["first_review"] = first_reviews[task.claim_id]
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


def unpack_review_result(result, expected_ids):
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return {}  # contract retry must ask the LLM, never invent a verdict
    output = {}
    for row in result["results"]:
        if not isinstance(row, dict) or not isinstance(row.get("claim_id"), str):
            continue  # unidentifiable row is a missing Claim, not prose routing
        cid = row["claim_id"]
        if cid not in expected_ids or cid in output:
            raise ValueError("unknown or duplicate batched review Claim ID")
        output[cid] = row
    return output
