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
from .semantic_contracts import (REFERENCE_INSTRUCTIONS, normalize_reference_checks,
                                 SEMANTIC_REVIEW_CONTRACT, semantic_review_schema, review_ref_ids)


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


def build_atomic_tasks(run) -> List[AtomicTask]:
    """Create one small review task per claim from explicit refs only."""

    facts = run.fact_store.to_dict()
    calcs = run.calc_store.to_dict()
    evidence = _evidence_dict(run.evidence_store)
    events = getattr(run, "events", {})
    claims = claim_index(run.claims)
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

        tasks.append(AtomicTask(
            claim_id=cid,
            claim=deepcopy(claim),
            resolved_refs=deepcopy(resolved),
            dependency_claims=deps,
            unresolved_refs=unresolved,
            context={"as_of": getattr(run, "manifest", {}).get("as_of"),
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
7. findings 必須綁定本 claim_id。不要自行改寫其他 claim。
8. 本任務只查這條 Claim 對直接前提的使用；每個上游 Claim 另有必做驗證，不重審整條祖先鏈。
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
  ]
}
PASS 時 findings 必須是空陣列。
""".strip() + "\n\n" + REFERENCE_INSTRUCTIONS


def build_atomic_prompt(task: AtomicTask, *, verification_epoch=None) -> str:
    payload = task.review_dict()
    payload.update(response_contract=SEMANTIC_REVIEW_CONTRACT,
                   expected_reference_ids=review_ref_ids(task.claim), verification_epoch=verification_epoch or uuid4().hex)
    text = (
        ATOMIC_INSTRUCTIONS
        + "\n\n## Claim 與局部證據上下文\n```json\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        + "\n```\n\n只回 JSON object，不要 markdown。"
    )
    return structured_prompt(text, json.dumps(semantic_review_schema(task.claim), ensure_ascii=False, separators=(",", ":")))


def normalize_atomic_result(claim_id: str, result: Dict[str, Any], *, expected_ref_ids=()) -> Dict[str, Any]:
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
    out = dict(result)
    out["claim_id"] = claim_id
    out["verdict"] = verdict
    out["findings"] = findings
    out["reference_checks"] = normalize_reference_checks(expected_ref_ids, out)
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

    normalized, invalid = {}, {}
    for cid in expected:
        if cid not in results_by_claim:
            continue
        try:
            normalized[cid] = normalize_atomic_result(cid, results_by_claim[cid],
                                                      expected_ref_ids=(expected_refs_by_claim or {}).get(cid, ()))
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
