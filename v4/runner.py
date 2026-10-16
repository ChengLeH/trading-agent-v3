#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V4 runner: all claims get semantic LLM review; no prose hard filter.

This subclasses V3 to keep ingestion, deterministic finance calculations,
rendering and existing role flow intact while replacing Phase 6 routing.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Tuple

from v3.runner import PipelineRunner, load_role_template
from .atomic_phase6 import (
    build_atomic_prompt,
    build_atomic_tasks,
    build_deep_review_payload,
    summarize_atomic_results,
)
from .bindings import validate_claim_bindings


class PipelineRunnerV4(PipelineRunner):
    """V4 Phase 6 coordinator.

    Sequence:
      1) deterministic structured binding validation
      2) EVERY claim -> one small semantic LLM review (parallelizable)
      3) only SUSPICIOUS/FAIL claims -> expensive deep reviewer
      4) exact claim-id surgical revision
      5) final global semantic consistency sweep

    No regex or keyword text filter decides whether a claim gets reviewed.
    """

    def prepare_phase6_atomic(self) -> Dict[str, Any]:
        run = self.orch.runs[self.run_id]

        # Deterministic checks are allowed only on explicit structured metadata.
        binding_findings = [
            f.to_dict() for f in validate_claim_bindings(
                run.claims, run.fact_store, require_bindings=False
            )
        ]

        tasks = build_atomic_tasks(run)
        prompts = [(task.claim_id, build_atomic_prompt(task)) for task in tasks]
        self._v4_atomic_claim_ids = [task.claim_id for task in tasks]
        self._v4_binding_findings = binding_findings
        return {
            "binding_findings": binding_findings,
            "atomic_prompts": prompts,
            "claim_count": len(prompts),
        }

    def collect_phase6_atomic(self, results_by_claim: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        expected = getattr(self, "_v4_atomic_claim_ids", None)
        if expected is None:
            expected = [
                c.get("claim_id") for c in self.orch.runs[self.run_id].claims
                if c.get("claim_id")
            ]
        summary = summarize_atomic_results(expected, results_by_claim)
        summary["binding_findings"] = getattr(self, "_v4_binding_findings", [])
        self._v4_atomic_summary = summary

        if not summary["complete"]:
            summary["blocked"] = True
            summary["reason"] = "not every claim received semantic LLM verification"
            return summary

        suspicious = summary["suspicious_claim_ids"] + summary["fail_claim_ids"]
        summary["blocked"] = False
        summary["needs_deep_review"] = bool(suspicious)
        summary["deep_review_claim_ids"] = suspicious
        return summary

    def prepare_phase6_deep_review(self, claim_ids: Iterable[str] = None) -> Tuple[str, str]:
        """Expensive second-model review only after every claim had atomic review."""
        run = self.orch.runs[self.run_id]
        summary = getattr(self, "_v4_atomic_summary", {})
        if claim_ids is None:
            claim_ids = summary.get("deep_review_claim_ids", [])
        claim_ids = list(claim_ids)

        payload = build_deep_review_payload(
            run, claim_ids, summary.get("findings", [])
        )
        template = load_role_template("adversarial_reviewer")
        prompt = (
            f"{template}\n\n"
            "## V4 深度覆核範圍\n"
            "下面只包含已經由第一層語義 LLM 判為 SUSPICIOUS/FAIL 的 claims。\n"
            "你要獨立判斷第一層是否成立；不得因立場不同判錯。\n"
            "每一條 finding 必須回傳精確 claim_id；禁止用模糊 location 取代 claim_id。\n"
            "如果第一層是誤報，明確回傳 dispute=true。\n\n"
            "```json\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            + "\n```\n\n直接回傳 JSON array，不要 markdown。"
        )
        return ("adversarial_reviewer", prompt)

    def _claim_index(self) -> Dict[str, Dict[str, Any]]:
        run = self.orch.runs[self.run_id]
        return {c.get("claim_id"): c for c in run.claims if c.get("claim_id")}

    def _finding_claim_ids(self, finding: Dict[str, Any]) -> List[str]:
        """Read explicit IDs only. Never infer from prose."""
        out: List[str] = []
        if finding.get("claim_id"):
            out.append(finding["claim_id"])
        for cid in finding.get("claim_ids", []) or []:
            if cid not in out:
                out.append(cid)
        return out

    def prepare_finding_linker(self, findings: List[Dict[str, Any]]) -> Tuple[str, str]:
        """Semantic LLM linker for rare global findings without claim IDs.

        This replaces V3's regex/number/keyphrase back-search.
        """
        claims = self._claim_index()
        claim_catalog = [
            {
                "claim_id": cid,
                "author_role": c.get("author_role"),
                "text": c.get("text", ""),
                "refs": c.get("refs", []),
            }
            for cid, c in claims.items()
        ]
        prompt = (
            "你是 finding→claim 語義路由器。不要用關鍵字、regex 或數字字面相等來判。\n"
            "理解 finding 指的是哪些論述，回傳精確 claim_id。不要修內容。\n"
            "如果沒有足夠資訊定位，回 unresolved=true，不得亂猜。\n\n"
            "輸出 JSON array，每項格式："
            '{"finding_index":0,"claim_ids":["..."],"unresolved":false,"reason":"..."}'
            "\n\n## findings\n```json\n"
            + json.dumps(findings, ensure_ascii=False, indent=2, default=str)
            + "\n```\n\n## claim catalog\n```json\n"
            + json.dumps(claim_catalog, ensure_ascii=False, indent=2, default=str)
            + "\n```\n"
        )
        return ("finding_linker", prompt)

    def apply_finding_links(
        self,
        findings: List[Dict[str, Any]],
        links: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Attach semantic linker output structurally. No text matching."""
        linked = [dict(f) for f in findings]
        claims = self._claim_index()
        for item in links:
            idx = item.get("finding_index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(linked):
                raise ValueError(f"bad finding_index from linker: {idx!r}")
            if item.get("unresolved"):
                linked[idx]["routing_unresolved"] = True
                linked[idx]["routing_reason"] = item.get("reason", "")
                continue
            ids = item.get("claim_ids", []) or []
            bad = [cid for cid in ids if cid not in claims]
            if bad:
                raise ValueError(f"linker returned unknown claim_ids: {bad}")
            linked[idx]["claim_ids"] = ids
            linked[idx]["routing_reason"] = item.get("reason", "")
        return linked

    def prepare_phase6_revision_v4(self, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Exact-ID surgical patching; unresolved findings go to semantic linker."""
        claims = self._claim_index()
        unrouted = [f for f in findings if not self._finding_claim_ids(f)]
        if unrouted:
            return {
                "needs_linking": True,
                "linker_prompt": self.prepare_finding_linker(unrouted),
                "unrouted_findings": unrouted,
            }

        by_role: Dict[str, Dict[str, Any]] = {}
        for finding in findings:
            for cid in self._finding_claim_ids(finding):
                claim = claims.get(cid)
                if claim is None:
                    raise ValueError(f"finding references unknown claim_id {cid!r}")
                role = claim.get("author_role") or "phase6_reviser"
                bucket = by_role.setdefault(role, {"findings": [], "claims": {}})
                bucket["findings"].append(finding)
                bucket["claims"][cid] = claim

        run = self.orch.runs[self.run_id]
        prompts: List[Tuple[str, str]] = []
        for role, bucket in by_role.items():
            template = load_role_template(role) or load_role_template("phase6_reviser")
            prompt = (
                f"{template}\n\n"
                "## V4 修訂規則\n"
                "只修被精確 claim_id 點名的 Claim Object；不得重寫整章。\n"
                "保持原立場，只修硬錯。直接回傳 JSON array。\n\n"
                "## findings\n```json\n"
                + json.dumps(bucket["findings"], ensure_ascii=False, indent=2, default=str)
                + "\n```\n\n## claims\n```json\n"
                + json.dumps(list(bucket["claims"].values()), ensure_ascii=False, indent=2, default=str)
                + "\n```\n\n## canonical FACT store\n```json\n"
                + json.dumps(run.fact_store.to_dict(), ensure_ascii=False, indent=2, default=str)
                + "\n```\n"
            )
            prompts.append((role, prompt))

        return {"needs_linking": False, "revision_prompts": prompts}

    def prepare_phase6_global_sweep(self) -> Tuple[str, str]:
        """Final whole-report LLM sweep only for cross-claim/state consistency.

        Atomic review already checked each claim. This pass asks only whether
        independently valid claims contradict each other or revisions failed to
        propagate. It is intentionally semantic, not regex based.
        """
        run = self.orch.runs[self.run_id]
        report = getattr(self, "_phase6_report", "")
        catalog = [
            {
                "claim_id": c.get("claim_id"),
                "author_role": c.get("author_role"),
                "text": c.get("text", ""),
                "refs": c.get("refs", []),
            }
            for c in run.claims
        ]
        prompt = (
            "你是 V4 最終全局一致性審查員。前面每條 claim 已獨立驗過；"
            "這一輪只抓跨 claim/跨章矛盾、修正未傳遞、舊 state 殘留、"
            "互斥交易規則同時存在。不要重做多空立場評審。\n"
            "任何 finding 必須列出 claim_ids；無法定位就標 unresolved，禁止猜。\n\n"
            "## claim catalog\n```json\n"
            + json.dumps(catalog, ensure_ascii=False, indent=2, default=str)
            + "\n```\n\n## rendered report\n```\n"
            + report
            + "\n```\n\n直接回傳 JSON array，不要 markdown。"
        )
        return ("global_consistency_reviewer", prompt)
