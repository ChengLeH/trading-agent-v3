#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Atomic Phase 6 semantic verification.

Every claim is reviewed by an LLM. There is no regex/keyword pre-filter.
Python only resolves the claim's explicit refs into a compact context bundle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Tuple
from copy import deepcopy
from uuid import uuid4
from .cloud import structured_prompt
from .schema import claim_index, digest
from .review_context import calculation_definitions, indicator_definitions
from .semantic_contracts import (REFERENCE_INSTRUCTIONS, EVIDENCE_INSTRUCTIONS, MissingReferenceChecks,
                                 normalize_reference_checks, SEMANTIC_REVIEW_CONTRACT,
                                 semantic_review_schema, review_ref_ids, require_text,
                                 normalize_semantic_finding_fields, EVIDENCE_REQUEST_INSTRUCTIONS)

from .financial_review import FINANCIAL_REVIEW_CONTRACT, FINANCIAL_REVIEW_VERSION, ingestion_diagnostics
from .source_lineage import project_source_lineage
from .review_diagnostics import (REVIEW_CONTEXT_FORMAT, DIAGNOSTICS_INSTRUCTIONS,
                                 project_diagnostics)
from .review_wire import project_review_payload, review_wire_instructions

CATALOG_DETAIL_MAX_BYTES = 4096
CATALOG_SCOPES = frozenset({"referenced_sources", "full_catalog"})


@dataclass
class AtomicTask:
    claim_id: str
    claim: Dict[str, Any]
    resolved_refs: Dict[str, Any]
    dependency_claims: List[Dict[str, Any]]
    unresolved_refs: List[str]
    context: Dict[str, Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def review_dict(self):
        """Wire context is local; the receipt fingerprint still covers ancestors.

        Every ancestor remains a separate mandatory LLM task. A child checks
        its use of direct premises instead of recursively re-reviewing them.
        """
        direct = set(self.claim.get("dependencies", []))
        parents = [c for c in self.dependency_claims if c["claim_id"] in direct]
        all_claim_ids = {c["claim_id"] for c in self.dependency_claims}
        resolved, unresolved, visited = {}, [], set()
        def source(ref):
            if ref in visited:
                return
            visited.add(ref)
            if ref in all_claim_ids:
                return
            if ref not in self.resolved_refs:
                unresolved.append(ref)
                return
            item = self.resolved_refs[ref]
            resolved[ref] = deepcopy(item)
            if item["kind"] == "calc":
                for parent in item["value"].get("input_refs", []):
                    source(parent)
            if item["kind"] == "event":
                source(item["value"]["source_ref"])
                if item["value"].get("aggregate_ref"):
                    source(item["value"]["aggregate_ref"])
        for claim in [self.claim] + parents:
            for ref in claim.get("refs", []) + claim.get("fact_refs", []):
                source(ref)
        return dict(claim_id=self.claim_id, claim=deepcopy(self.claim), resolved_refs=resolved,
                    dependency_claims=deepcopy(parents), unresolved_refs=unresolved, context=deepcopy(self.context),
                    verification_input_hash=digest(self.to_dict()))



def _evidence_dict(store) -> Dict[str, Any]:
    if store is None:
        return {}
    raw = store.to_dict()
    return raw if isinstance(raw, dict) else {}


def _evidence_scope(run, facts, calcs, evidence, events):
    """Small metadata inventory, never a prose classifier or absence proof.

    Exclude generated trade resources so a trade patch does not invalidate
    unrelated analysts merely by changing the inventory counts.
    """
    trade_facts, trade_calcs = getattr(run, "trade_generated", ([], []))
    inputs = [f for fid, f in facts.items() if fid not in trade_facts]
    calculations = [c for cid, c in calcs.items() if cid not in trade_calcs]
    records = [dict(id=r.id, type=r.type, source_hash=r.source_hash)
               for r in getattr(run, "records", []) if hasattr(r, "source_hash")]
    concepts = sorted({f["concept"] for f in inputs if f.get("concept")})
    include_concepts = len(json.dumps(concepts, ensure_ascii=False).encode("utf-8")) <= CATALOG_DETAIL_MAX_BYTES
    return dict(scope="current_ingested_catalog_metadata", includes_source_contents=False,
                counts=dict(facts=len(inputs), calcs=len(calculations), evidence=len(evidence), events=len(events)),
                fact_concepts=concepts if include_concepts else None,
                fact_concept_count=len(concepts), fact_concepts_hash=digest(concepts),
                source_record_count=len(records), source_types=sorted({r["type"] for r in records}),
                source_records_hash=digest(sorted(records, key=lambda r: r["id"])))


def _full_catalog_content_hash(run, facts, calcs, evidence, events):
    """Hash complete catalog content only when a new claim explicitly needs it."""
    trade_facts, trade_calcs = getattr(run, "trade_generated", ([], []))
    records = [record.to_dict() if callable(getattr(record, "to_dict", None)) else
               dict(id=getattr(record, "id", None), type=getattr(record, "type", None),
                    source_hash=getattr(record, "source_hash", None))
               for record in getattr(run, "records", []) if hasattr(record, "source_hash")]
    records.sort(key=lambda row: row.get("id") or "")
    return digest(dict(
        facts={fid: facts[fid] for fid in sorted(facts) if fid not in trade_facts},
        calcs={cid: calcs[cid] for cid in sorted(calcs) if cid not in trade_calcs},
        evidence=evidence,
        events=events,
        source_records=records,
    ))


def _declared_catalog_scope(claim):
    """Read an explicit typed scope; legacy claims stay catalog-sensitive."""
    assertion = claim.get("assertion")
    if not isinstance(assertion, dict) or "catalog_scope" not in assertion:
        return "full_catalog"
    scope = assertion["catalog_scope"]
    if type(scope) is not str or scope not in CATALOG_SCOPES:
        raise ValueError("assertion.catalog_scope must be 'referenced_sources' or 'full_catalog'")
    return scope


def _referenced_evidence_scope(run, source_ids):
    """Fingerprint only the source records reached through explicit refs."""
    source_ids = sorted(source_ids)
    records = [dict(id=record.id, type=record.type, source_hash=record.source_hash)
               for record in getattr(run, "records", [])
               if getattr(record, "id", None) in source_ids and hasattr(record, "source_hash")]
    records.sort(key=lambda row: row["id"])
    source_ids_bytes = json.dumps(source_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    records_bytes = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return dict(scope="referenced_sources", includes_source_contents=False,
                referenced_source_count=len(source_ids),
                referenced_source_ids=source_ids if len(source_ids_bytes) <= CATALOG_DETAIL_MAX_BYTES else None,
                referenced_source_ids_hash=digest(source_ids),
                source_record_count=len(records),
                source_records=records if len(records_bytes) <= CATALOG_DETAIL_MAX_BYTES else None,
                source_records_included=len(records_bytes) <= CATALOG_DETAIL_MAX_BYTES,
                source_records_hash=digest(records))


def _catalog_scope_instructions(task):
    """Explain the typed scope to the reviewer without changing legacy prompts."""
    scope = (task.context or {}).get("evidence_scope", {})
    declared = scope.get("catalog_scope")
    if declared not in CATALOG_SCOPES:
        return ""

    common = (
        "此欄位只宣告作者希望使用的目錄範圍，不是證據，也不能免除你核對 Claim 與實際證據是否相符。"
        "仍須按 text 與 assertion 的實際語義檢查此宣告是否適當；不得從 prose 自動推定或改寫範圍。"
    )
    if declared == "referenced_sources" and scope.get("effective_catalog_scope") == "referenced_sources":
        return ("\n\n## Typed catalog scope\n"
                "此 Claim 宣告 catalog_scope=referenced_sources。只根據 Claim 明確引用、且在 resolved_refs "
                "或 dependency_claims 中提供的來源判斷；不使用全目錄計數或未引用項目。"
                "若命題實際依賴完整目錄、來源不存在或未提供，而引用來源未能證明，請回 SUSPICIOUS；"
                "不得因作者宣告 referenced_sources 就略過這項缺失判斷。" + common)
    if declared == "referenced_sources":
        inherited = scope.get("inherited_full_catalog_claim_ids", [])
        return ("\n\n## Typed catalog scope\n"
                "此 Claim 宣告 catalog_scope=referenced_sources，但因依賴以下 full_catalog 前提，"
                f"本次任務繼承 full_catalog 敏感性：{', '.join(inherited)}。"
                "檢查已附全目錄 metadata 與依賴；metadata 的計數、指紋及概念摘要不是來源全文，"
                "不能單獨證明任何資料缺席。" + common)
    return ("\n\n## Typed catalog scope\n"
            "此 Claim 宣告 catalog_scope=full_catalog，必須保留對整體目錄完整性與缺席判斷的敏感性。"
            "evidence_scope 是全目錄 metadata 摘要，不含來源全文；計數、指紋及概念摘要不能單獨證明資料缺席。"
            "若命題依賴完整性或缺席，而目前 context 沒有足以判斷的來源，請回 SUSPICIOUS。" + common)


def _local_calculation_definitions(claims, resolved):
    """Describe only calculations reachable from the reviewed Claim and its direct parents."""
    calculations, visited = {}, set()

    def visit(ref):
        if ref in visited:
            return
        visited.add(ref)
        item = resolved.get(ref)
        if not item:
            return
        value = item["value"]
        if item["kind"] == "calc":
            calculations[ref] = value
            for input_ref in value.get("input_refs", []):
                visit(input_ref)
        elif item["kind"] == "event":
            visit(value["source_ref"])
            if value.get("aggregate_ref"):
                visit(value["aggregate_ref"])

    for claim in claims:
        for ref in claim.get("refs", []) + claim.get("fact_refs", []):
            visit(ref)
    return calculation_definitions(calculations)


def build_atomic_tasks(run) -> List[AtomicTask]:
    """Create one small review task per claim from explicit refs only."""

    facts = run.fact_store.to_dict()
    calcs = run.calc_store.to_dict()
    evidence = _evidence_dict(run.evidence_store)
    events = getattr(run, "events", {})
    evidence_scope = _evidence_scope(run, facts, calcs, evidence, events)
    diagnostics = ingestion_diagnostics(run)
    from .financial_statements import source_declarations
    declarations = source_declarations(run)
    claims = claim_index(run.claims)
    catalog_scopes = {cid: _declared_catalog_scope(claim) for cid, claim in claims.items()}
    catalog_content_hash = None
    tasks: List[AtomicTask] = []

    for claim in run.claims:
        cid = claim.get("claim_id")
        if not cid:
            continue
        resolved: Dict[str, Any] = {}
        deps: List[Dict[str, Any]] = []
        unresolved: List[str] = []
        references = claim.get("refs", []) + claim.get("fact_refs", [])
        seen_refs = set()

        def resolve(ref):
            if ref in seen_refs:
                return
            seen_refs.add(ref)
            if ref in facts:
                resolved[ref] = {"kind": "fact", "value": facts[ref]}
            elif ref in calcs:
                resolved[ref] = {"kind": "calc", "value": calcs[ref]}
                for source in calcs[ref].get("input_refs", []):
                    resolve(source)
            elif ref in evidence:
                resolved[ref] = {"kind": "evidence", "value": evidence[ref]}
            elif ref in events:
                resolved[ref] = {"kind": "event", "value": events[ref]}
                resolve(events[ref]["source_ref"])
                if events[ref].get("aggregate_ref"):
                    resolve(events[ref]["aggregate_ref"])
            elif ref.startswith("evidence.") and ref[9:] in evidence:
                resolved[ref] = {"kind": "evidence", "value": evidence[ref[9:]]}
            elif ref in claims:
                return  # dependency objects are resolved below
            else:
                unresolved.append(ref)

        seen_deps = set()

        def dependency(cid):
            if cid in seen_deps:
                return
            seen_deps.add(cid)
            dep = claims[cid]
            deps.append(deepcopy(dep))
            for ref in dep.get("refs", []) + dep.get("fact_refs", []):
                resolve(ref)
            for parent in dep.get("dependencies", []):
                dependency(parent)

        for ref in references:
            resolve(ref)
        for dep in claim.get("dependencies", []):
            dependency(dep)

        declared_scope = catalog_scopes[cid]
        inherited_full_catalog_claim_ids = sorted(
            dep["claim_id"] for dep in deps if catalog_scopes[dep["claim_id"]] == "full_catalog")
        effective_scope = ("full_catalog" if declared_scope == "full_catalog"
                           or inherited_full_catalog_claim_ids else "referenced_sources")
        assertion = claim.get("assertion")
        explicit_scope = isinstance(assertion, dict) and "catalog_scope" in assertion
        explicit_full_catalog = explicit_scope and declared_scope == "full_catalog"
        explicit_full_ancestors = [
            dep for dep in deps
            if (isinstance(dep.get("assertion"), dict)
                and dep["assertion"].get("catalog_scope") == "full_catalog")]
        needs_full_content_hash = explicit_full_catalog or bool(explicit_full_ancestors)

        direct = set(claim.get("dependencies", []))
        local_claims = [claim] + [parent for parent in deps if parent["claim_id"] in direct]
        definitions = _local_calculation_definitions(local_claims, resolved)
        indicators = indicator_definitions({ref: item['value'] for ref, item in resolved.items()
                                             if item['kind'] == 'fact'})
        lineage_refs = set(resolved)
        source_ids = {item["value"].get("source_id") for item in resolved.values()
                      if item.get("kind") in {"fact", "evidence"}
                      and isinstance(item.get("value"), dict)
                      and isinstance(item["value"].get("source_id"), str)
                      and item["value"].get("source_id")}
        lineage_refs.update(source_ids)
        source_lineage = project_source_lineage(
            getattr(run, "source_lineage", {}), lineage_refs)
        task_evidence_scope = (deepcopy(evidence_scope) if effective_scope == "full_catalog"
                               else _referenced_evidence_scope(run, source_ids))
        task_declarations = (declarations if effective_scope == "full_catalog"
                             else {source_id: declarations[source_id] for source_id in sorted(source_ids)
                                   if source_id in declarations})
        if explicit_scope:
            task_evidence_scope["catalog_scope"] = declared_scope
            task_evidence_scope["effective_catalog_scope"] = effective_scope
            if declared_scope == "referenced_sources" and inherited_full_catalog_claim_ids:
                task_evidence_scope["inherited_full_catalog_claim_ids"] = inherited_full_catalog_claim_ids
        if needs_full_content_hash:
            if catalog_content_hash is None:
                catalog_content_hash = _full_catalog_content_hash(run, facts, calcs, evidence, events)
            task_evidence_scope["catalog_contents_hash"] = catalog_content_hash
        tasks.append(AtomicTask(
            claim_id=cid,
            claim=deepcopy(claim),
            resolved_refs=deepcopy(resolved),
            dependency_claims=deps,
            unresolved_refs=unresolved,
            context={"financial_review_contract": FINANCIAL_REVIEW_VERSION,
                     "review_context_format": REVIEW_CONTEXT_FORMAT,
                     "as_of": getattr(run, "manifest", {}).get("as_of"),
                     **({'analysis_mandate': deepcopy(run.manifest['analysis_mandate'])} if 'analysis_mandate' in getattr(run, 'manifest', {}) else {}),
                     **({'source_declarations': task_declarations} if task_declarations else {}),
                     **({"calculation_definitions": definitions} if definitions else {}),
                     **({'indicator_definitions': indicators} if indicators else {}),
                     **({'source_lineage': source_lineage} if source_lineage else {}),
                     "evidence_scope": {**task_evidence_scope,
                         "ingestion_diagnostics": project_diagnostics(diagnostics, [claim] + deps, resolved)},
                     "trade": deepcopy(getattr(run, "trade", {}))
                     if claim.get("author_role") in {"trader", "risk_manager"} else {}},
        ))
    return tasks


ATOMIC_INSTRUCTIONS = r"""
你是 V4 的原子化語義驗證員。你只審一條 claim。

鐵律：
1. 不做關鍵字/regex 判斷；要理解整句語義。
2. 不因多空立場、語氣或投資觀點不同判錯。
3. 只檢查可驗證硬問題：主體、指標、期間、scope、unit、basis、日期、數值、
   source→claim 扭曲、dependency stale、計算/風險模型語義。
4. Claim 的每個引用來源都已附在 context；不得把不存在的來源當成存在。
5. 如果 context 不足以判定真假，回 SUSPICIOUS，不得猜 PASS。
6. 如果是 interpretation/forecast，只在它把來源事實說錯、或推論明確超出所引證據時才報。
7. 只有會改變事實、歸因、因果、範圍、條件或決策的問題才列 findings。輕微籠統或措辭不夠清楚，
   若合理解讀不會改變上述內容，可用 clarity_notes 留下簡短提醒，也可不記；不可因此判 FAIL 或要求修補。
   若含糊可能改變數值、責任、前提或動作，仍是實質問題，回 SUSPICIOUS/FAIL。
8. findings 與 clarity_notes 都必須綁定本 claim_id。不要自行改寫其他 claim。
9. 本任務只查這條 Claim 對直接前提的使用；每個上游 Claim 另有必做驗證，不重審整條祖先鏈。
   父 Claim 出錯會阻擋總體交付並在修補後重驗下游；沒有提供的來源仍不能猜測。

輸出單一 JSON object：
{
  "claim_id": "原 claim_id",
  "verdict": "PASS|SUSPICIOUS|FAIL",
  "reference_checks": [{"ref_id":"實際引用或相依 ID","relation":"SUPPORTS|CONTRADICTS|INSUFFICIENT",
    "attributed_assertion":"原文實際歸因給此來源的命題","source_evidence":"context 內來源內容／位置",
    "reason":"為何支持、矛盾或不足"}],
  "findings": [
    {
      "category": "entity|metric|period|scope|unit|basis|date|value|source_distortion|dependency_state|risk_model|other",
      "issue": "具體問題",
      "evidence": "context 中支持判斷的內容",
      "suggested_fix": "只寫必要修正"
    }
  ],
  "clarity_notes": [{"issue":"不影響事實或決策的輕微表達提醒","reason":"為何合理解讀不會改變主張"}]
}
clarity_notes 可省略。PASS 時 findings 必須是空陣列；小幅措辭提醒不算 finding。
""".strip() + "\n\n" + REFERENCE_INSTRUCTIONS + "\n" + FINANCIAL_REVIEW_CONTRACT + "\n" + EVIDENCE_REQUEST_INSTRUCTIONS + "\n" + DIAGNOSTICS_INSTRUCTIONS


def build_atomic_prompt(task: AtomicTask, *, verification_epoch=None) -> str:
    payload = task.review_dict()
    payload.update(response_contract=SEMANTIC_REVIEW_CONTRACT,
                   expected_reference_ids=review_ref_ids(task.claim), verification_epoch=verification_epoch or uuid4().hex)
    payload = project_review_payload(payload)
    text = (
        ATOMIC_INSTRUCTIONS
        + _catalog_scope_instructions(task)
        + review_wire_instructions(payload)
        + "\n\n## Claim 與局部證據上下文\n```json\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n```\n\n只回 JSON object，不要 markdown。"
    )
    return structured_prompt(text, json.dumps(semantic_review_schema(task.claim), ensure_ascii=False, separators=(",", ":")))


def build_reference_supplement_prompt(task: AtomicTask, prior_result, missing_ref_ids, *,
                                      verification_epoch=None, layer="atomic", first_review=None,
                                      previous_error=None):
    """Ask the LLM to fill only missing reference checks and refresh its verdict."""
    if layer not in {"atomic", "deep"} or not missing_ref_ids:
        raise ValueError("reference supplement needs a review layer and missing IDs")
    payload = task.review_dict()
    payload.update(response_contract=SEMANTIC_REVIEW_CONTRACT,
                   expected_reference_ids=list(missing_ref_ids),
                   accepted_reference_checks=deepcopy(prior_result.get("reference_checks", [])),
                   prior_verdict=prior_result.get("verdict"),
                   prior_findings=deepcopy(prior_result.get("findings", [])),
                   verification_epoch=verification_epoch or uuid4().hex)
    if previous_error:
        payload["previous_supplement_validation_error"] = previous_error
    if first_review is not None:
        payload["first_layer_review"] = deepcopy(first_review)
    role_instructions = (
        "你是 V4 的深度對抗覆核員。重新判斷 Claim 的整體 verdict，並檢查第一層判斷；"
        "但 accepted_reference_checks 是已完成且保留的檢查，不要重審、改寫或重複它們。"
        if layer == "deep" else
        "你是 V4 的原子化語義驗證員。這條 Claim 已由同一驗證流程檢查過部分引用。"
    )
    instructions = (
        role_instructions
        + _catalog_scope_instructions(task)
        + EVIDENCE_INSTRUCTIONS
        + FINANCIAL_REVIEW_CONTRACT
        + EVIDENCE_REQUEST_INSTRUCTIONS
        + "只補 expected_reference_ids 所列的缺漏引用；reference_checks 必須只含這些 ID。"
        + "逐項比對原文歸因與 context 內實際證據，不相關的真實摘錄不構成支持。"
        + "relation 只能是 SUPPORTS、CONTRADICTS、INSUFFICIENT；矛盾必須判 FAIL，"
        + "證據不足不可判 PASS。根據全部已接受檢查和本次補審，重新回傳整條 Claim 的 verdict 與 findings。"
        + "PASS 時 findings=[]；SUSPICIOUS/FAIL 必須有具體 findings。不要因多空立場或文字風格判錯。"
        + "不得捏造來源，不得修改 accepted_reference_checks。只回完整 JSON object。"
    )
    payload = project_review_payload(payload)
    text = (instructions + review_wire_instructions(payload)
            + "\n\n## Claim、局部證據及已接受引用檢查\n```json\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
            + "\n```\n")
    schema = semantic_review_schema(task.claim, reference_ids=missing_ref_ids)
    return structured_prompt(text, json.dumps(schema, ensure_ascii=False, separators=(",", ":")))


def build_reference_verdict_repair_prompt(task: AtomicTask, accepted_reference_checks, *,
                                         validation_error, verification_epoch=None,
                                         layer="atomic", first_review=None):
    """Repair a malformed overall verdict without repeating accepted source checks."""
    if layer not in {"atomic", "deep"}:
        raise ValueError("semantic verdict repair needs a review layer")
    payload = task.review_dict()
    payload.update(response_contract=SEMANTIC_REVIEW_CONTRACT,
                   expected_reference_ids=[],
                   accepted_reference_checks=deepcopy(accepted_reference_checks),
                   validation_error=validation_error,
                   verification_epoch=verification_epoch or uuid4().hex)
    if first_review is not None:
        payload["first_layer_review"] = deepcopy(first_review)
    role = ("你是 V4 的深度對抗覆核員。重新判斷 Claim 的整體語義與 verdict，並檢查第一層判斷。"
            if layer == "deep" else
            "你是 V4 的原子化語義驗證員。重新判斷 Claim 的整體語義與 verdict。")
    instructions = (
        role
        + _catalog_scope_instructions(task)
        + EVIDENCE_INSTRUCTIONS
        + FINANCIAL_REVIEW_CONTRACT
        + EVIDENCE_REQUEST_INSTRUCTIONS
        + "accepted_reference_checks 是已完成且保存的來源審查；使用它們判斷主張，但不要重審、改寫或重複回傳。"
        + "只修正 validation_error 指出的 verdict/findings/結構契約問題。"
        + "所有 Claim 仍須由你作語義判斷；來源支持不足、矛盾或不確定時，不可判 PASS。"
        + "若 verdict=PASS，findings=[]；若 verdict=SUSPICIOUS/FAIL，提供具體 findings。"
        + "不要輸出 reference_checks。只回完整 JSON object。"
    )
    payload = project_review_payload(payload)
    text = (instructions + review_wire_instructions(payload)
            + "\n\n## Claim、局部證據及已接受引用檢查\n```json\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
            + "\n```\n")
    schema = semantic_review_schema(task.claim, reference_ids=[])
    return structured_prompt(text, json.dumps(schema, ensure_ascii=False, separators=(",", ":")))


def normalize_atomic_result(claim_id: str, result: Dict[str, Any], *, expected_ref_ids=(),
                            allow_missing_references=False) -> Dict[str, Any]:
    """Strict structural normalization; no semantic judgment in Python."""
    if not isinstance(result, dict):
        raise ValueError(f"atomic result for {claim_id} must be an object")
    got = result.get("claim_id")
    if got != claim_id:
        raise ValueError(f"atomic result claim_id mismatch: expected {claim_id}, got {got}")
    verdict = str(result.get("verdict", "")).upper()
    if verdict not in {"PASS", "SUSPICIOUS", "FAIL"}:
        raise ValueError(f"invalid atomic verdict for {claim_id}: {verdict!r}")
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise ValueError(f"findings for {claim_id} must be a list")
    if verdict == "PASS" and findings:
        raise ValueError(f"PASS result for {claim_id} must have empty findings")
    if verdict != "PASS" and not findings:
        raise ValueError(f"{verdict} result must explain its findings")
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("issue"), str) or not finding["issue"]:
            raise ValueError("every finding requires issue text")
        if finding.get("claim_id", claim_id) != claim_id:
            raise ValueError("finding targets another claim")
    findings = [normalize_semantic_finding_fields(finding, allowed_evidence_refs=expected_ref_ids)
                for finding in findings]
    if "clarity_notes" in result:
        notes = result["clarity_notes"]
        if not isinstance(notes, list):
            raise ValueError("clarity_notes must be a list")
        for note in notes:
            if not isinstance(note, dict):
                raise ValueError("clarity note must be an object")
            require_text(note.get("issue"), "clarity_notes.issue")
            require_text(note.get("reason"), "clarity_notes.reason")
    out = dict(result)
    out["claim_id"] = claim_id
    out["verdict"] = verdict
    out["findings"] = findings
    if "clarity_notes" in result:
        out["clarity_notes"] = deepcopy(result["clarity_notes"])
    out["reference_checks"] = normalize_reference_checks(
        expected_ref_ids, out, allow_missing=allow_missing_references)
    return out


def summarize_atomic_results(
    expected_claim_ids: Iterable[str],
    results_by_claim: Dict[str, Dict[str, Any]],
    *, expected_refs_by_claim=None, report_invalid=False,
) -> Dict[str, Any]:
    """Require an LLM result for every claim; missing reviews never auto-pass."""

    expected = list(expected_claim_ids)
    if len(set(expected)) != len(expected):
        raise ValueError("duplicate expected claim IDs")
    if set(results_by_claim) - set(expected):
        raise ValueError("unexpected atomic review IDs")
    missing = [cid for cid in expected if cid not in results_by_claim]
    if missing and not report_invalid:
        return {
            "complete": False,
            "missing_claim_ids": missing,
            "pass_claim_ids": [],
            "suspicious_claim_ids": [],
            "fail_claim_ids": [],
            "findings": [],
        }

    normalized, invalid, partial_results, missing_reference_ids = {}, {}, {}, {}
    for cid in expected:
        if cid not in results_by_claim:
            continue
        try:
            normalized[cid] = normalize_atomic_result(cid, results_by_claim[cid],
                                                      expected_ref_ids=(expected_refs_by_claim or {}).get(cid, ()))
        except MissingReferenceChecks as exc:
            if not report_invalid:
                raise
            partial_results[cid] = normalize_atomic_result(
                cid, results_by_claim[cid], expected_ref_ids=(expected_refs_by_claim or {}).get(cid, ()),
                allow_missing_references=True)
            missing_reference_ids[cid] = exc.missing_ref_ids
            invalid[cid] = str(exc)
        except (ValueError, TypeError) as exc:
            if not report_invalid:
                raise
            invalid[cid] = str(exc)
    passed, suspicious, failed, findings = [], [], [], []
    for cid, result in normalized.items():
        verdict = result["verdict"]
        if verdict == "PASS":
            passed.append(cid)
        elif verdict == "SUSPICIOUS":
            suspicious.append(cid)
        else:
            failed.append(cid)
        for finding in result.get("findings", []):
            f = dict(finding)
            f["claim_id"] = cid
            f["source"] = "atomic_semantic_verifier"
            findings.append(f)

    return {
        "complete": not missing and not invalid,
        "missing_claim_ids": missing,
        "invalid_claim_ids": list(invalid),
        "validation_errors": invalid,
        "partial_results": partial_results,
        "missing_reference_ids": missing_reference_ids,
        "pass_claim_ids": passed,
        "suspicious_claim_ids": suspicious,
        "fail_claim_ids": failed,
        "findings": findings,
        "results": normalized,
    }


def build_deep_review_payload(run, claim_ids: Iterable[str], findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact context for expensive second-model debate, only after all claims were checked."""

    wanted = set(claim_ids)
    tasks = {t.claim_id: t.to_dict() for t in build_atomic_tasks(run)}
    return {
        "claims": [tasks[cid] for cid in wanted if cid in tasks],
        "atomic_findings": [f for f in findings if f.get("claim_id") in wanted],
    }
