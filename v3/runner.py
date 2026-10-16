#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 runner.py — Executable pipeline orchestrator (2026-10-15).

The RUNBOOK.md describes the phases. This module makes them executable.

Design:
- The runner does NOT spawn subagents itself. The assistant does that.
- Instead, the runner provides:
  1. `prepare_phases(ticker, manifest)` → runs Phase 1 (Python only),
     returns list of (role, packet) for the assistant to spawn subagents
  2. `collect_results(run_id, claims_dict)` → feeds subagent outputs back,
     runs validation gates
  3. `finalize(run_id)` → renders report + runs final validation

Phase dependency:
    market ──┐
    fund ────┼──→ bull ──┐
    news ────┤          ├──→ RM → trader → risk
    social ──┘          │
                        └──→ bear ──┘
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .pipeline import Orchestrator


# Role execution order (groups that can run in parallel)
PHASE_2_ROLES = ["market_analyst", "fundamentals_analyst",
                 "news_analyst", "social_analyst"]
PHASE_3_ROLES = ["bull", "bear"]
PHASE_4_ROLES = ["research_manager", "trader", "risk_manager"]

# Phase 6 adversarial review: max report chars sent to the reviewer.
# Full 12-chapter reports run ~25-35K chars; keep headroom above that.
# (Segmented review is a future enhancement if reports grow beyond this.)
PHASE6_MAX_REPORT_CHARS = 40000

# Phase 6 revision loop: findings go back to a reviser, re-render,
# re-review. After this many rounds with findings remaining, block and
# report to the user instead of looping forever.
PHASE6_MAX_REVISIONS = 2

# Gate revision loops (Phase 2 analysis gate, final gate): when a gate
# blocks, go back to the original FACTs, revise the flagged claims, re-gate,
# and continue. Same max-rounds policy as Phase 6.
GATE_MAX_REVISIONS = 2

# Role template directory
ROLES_DIR = Path(__file__).parent / "roles"


def load_role_template(role: str) -> str:
    """Load the prompt template for a role."""
    path = ROLES_DIR / f"{role}.md"
    if not path.exists():
        # bull/bear use short names
        return ""
    return path.read_text(encoding="utf-8")


def build_subagent_prompt(role: str, packet: Dict[str, Any],
                          prior_claims: List[Dict] = None) -> str:
    """Build the full prompt to give a subagent for this role."""
    template = load_role_template(role)
    prompt = f"{template}\n\n## 你的證據包\n```json\n"
    prompt += json.dumps(packet, ensure_ascii=False, indent=2, default=str)
    prompt += "\n```\n"
    if prior_claims:
        prompt += f"\n## 之前角色的輸出\n```json\n"
        prompt += json.dumps(prior_claims, ensure_ascii=False,
                             indent=2, default=str)
        prompt += "\n```\n"
    prompt += "\n請按上面的輸出格式，直接回傳 JSON array，不要 markdown。"
    return prompt


class PipelineRunner:
    """Executable orchestrator. The assistant drives subagents; this drives Python."""

    def __init__(self):
        self.orch = Orchestrator()
        self.run_id: str = ""
        self._phase6_revision_round = 0
        self._phase6_revision_history: list = []
        self._gate_revision_rounds: dict = {}
        self._gate_revision_history: list = []

    def prepare(self, ticker: str, manifest: Dict[str, Any]
                ) -> List[Tuple[str, str]]:
        """Phase 1: Python only. Returns [(role, prompt)] for Phase 2.

        The assistant spawns one subagent per (role, prompt),
        collects the JSON claims, then calls .collect_phase2().
        """
        self.run_id = self.orch.start_run(
            {"ticker": ticker}, manifest)
        self.orch.ingest(self.run_id)
        build_report = self.orch.build_facts(self.run_id)
        print(f"Phase 1 done: {build_report}")

        prompts = []
        for role in PHASE_2_ROLES:
            packet = self.orch.get_packet(self.run_id, role)
            prompt = build_subagent_prompt(role, packet)
            prompts.append((role, prompt))
        return prompts

    def collect_phase2(self, claims_by_role: Dict[str, List[Dict]],
                       texts_by_role: Dict[str, str] = None):
        """Feed Phase 2 results back.

        If the analysis gate blocks, returns {"needs_revision": True, ...}
        instead of raising: the coordinator revises the flagged claims
        against the original FACTs (prepare_gate_revision /
        collect_phase2_revision) and the pipeline continues.
        On success returns [(role, prompt)] for Phase 3.
        """
        texts_by_role = texts_by_role or {}
        for role in PHASE_2_ROLES:
            claims = claims_by_role.get(role, [])
            text = texts_by_role.get(role, "")
            self.orch.submit_analysis(self.run_id, role, text, claims)

        if self.orch.gate(self.run_id, "analysis"):
            run = self.orch.runs[self.run_id]
            findings = [f for f in run.findings]
            blockers = [f for f in findings
                        if f.get("status") == "FAIL"
                        ]  # 不分級：有問題就修
            rnd = self._gate_revision_rounds.get("analysis", 0)
            self._gate_revision_history.append({
                "stage": "analysis", "round": rnd, "findings": blockers,
            })
            if rnd < GATE_MAX_REVISIONS:
                return {
                    "needs_revision": True,
                    "stage": "analysis",
                    "revision_round": rnd,
                    "findings": blockers,
                }
            raise RuntimeError(
                f"Phase 2 validation blocked after {GATE_MAX_REVISIONS} "
                f"revision rounds")

        # Phase 3: bull/bear in parallel (no cross-dependency)
        all_claims = self.orch.runs[self.run_id].claims
        prompts = []
        for role in PHASE_3_ROLES:
            packet = self.orch.get_packet(self.run_id, role)
            prompt = build_subagent_prompt(role, packet, all_claims)
            prompts.append((role, prompt))
        return prompts

    def prepare_gate_revision(self, stage: str,
                              findings: List[Dict]) -> Tuple[str, str]:
        """Build a reviser prompt for gate-blocking findings.

        Same reviser role as Phase 6: fix the flagged report content
        against the original FACTs, never touch code.
        Returns (role, prompt).
        """
        template = load_role_template("phase6_reviser")
        run = self.orch.runs[self.run_id]
        claims_by_id = {c.get("claim_id"): c for c in run.claims}

        prompt = f"{template}\n\n## 被擋下的問題（{stage} gate）\n```json\n"
        prompt += json.dumps(findings, ensure_ascii=False, indent=2, default=str)
        prompt += "\n```\n\n## 被點名的原始 claims\n```json\n"
        affected = []
        for f in findings:
            cid = f.get("object_id")
            if cid and cid in claims_by_id:
                affected.append(claims_by_id[cid])
        if not affected:
            affected = run.claims
        prompt += json.dumps(affected, ensure_ascii=False, indent=2, default=str)
        prompt += "\n```\n\n## 原始 FACT（英文，修正時只能用這裡的數字）\n```json\n"
        prompt += json.dumps(run.fact_store.to_dict(),
                             ensure_ascii=False, indent=2, default=str)
        prompt += "\n```\n\n請按上面的輸出格式，直接回傳 JSON array，不要 markdown。"
        return ("phase6_reviser", prompt)

    def collect_phase2_revision(self, revised_claims: List[Dict]):
        """Apply revised Phase 2 claims, re-gate, continue or revise again.

        Returns [(role, prompt)] for Phase 3 on success, or
        {"needs_revision": True, ...} for another round.
        """
        run = self.orch.runs[self.run_id]
        revised_by_id = {c.get("claim_id"): c for c in revised_claims
                         if isinstance(c, dict) and c.get("claim_id")}
        for i, c in enumerate(run.claims):
            cid = c.get("claim_id")
            if cid in revised_by_id:
                # 欄位合併：原 claim 為底，reviser 回傳的欄位疊加上去。
                # reviser 通常只回 claim_id/text/refs，全量替換會丟失
                # author_role、purpose、subsection 等欄位（BUG-002）。
                new_c = dict(c)
                new_c.update(revised_by_id[cid])
                new_c["claim_id"] = cid  # claim_id 不可被改寫
                new_c["_revised_in_gate_round"] = \
                    self._gate_revision_rounds.get("analysis", 0)
                run.claims[i] = new_c

        rnd = self._gate_revision_rounds.get("analysis", 0) + 1
        self._gate_revision_rounds["analysis"] = rnd

        if self.orch.gate(self.run_id, "analysis"):
            findings = [f for f in run.findings]
            blockers = [f for f in findings
                        if f.get("status") == "FAIL"
                        ]  # 不分級：有問題就修
            self._gate_revision_history.append({
                "stage": "analysis", "round": rnd, "findings": blockers,
            })
            if rnd < GATE_MAX_REVISIONS:
                return {
                    "needs_revision": True,
                    "stage": "analysis",
                    "revision_round": rnd,
                    "findings": blockers,
                }
            raise RuntimeError(
                f"Phase 2 validation blocked after {GATE_MAX_REVISIONS} "
                f"revision rounds")

        # Gate passed: continue to Phase 3
        all_claims = run.claims
        prompts = []
        for role in PHASE_3_ROLES:
            packet = self.orch.get_packet(self.run_id, role)
            prompt = build_subagent_prompt(role, packet, all_claims)
            prompts.append((role, prompt))
        return prompts

    def collect_phase3(self, claims_by_role: Dict[str, List[Dict]],
                       texts_by_role: Dict[str, str] = None
                       ) -> List[Tuple[str, str]]:
        """Feed Phase 3 results. Returns [(role, prompt)] for Phase 4 (sequential)."""
        texts_by_role = texts_by_role or {}
        for role in PHASE_3_ROLES:
            claims = claims_by_role.get(role, [])
            text = texts_by_role.get(role, "")
            self.orch.submit_analysis(self.run_id, role, text, claims)

        # Phase 4 is sequential: RM → trader → risk
        # Return RM prompt first; assistant calls collect_rm() then collect_trader()
        packet = self.orch.get_packet(self.run_id, "research_manager")
        all_claims = self.orch.runs[self.run_id].claims
        prompt = build_subagent_prompt("research_manager", packet, all_claims)
        return [("research_manager", prompt)]

    def collect_rm(self, claims: List[Dict], text: str = ""
                   ) -> Tuple[str, str]:
        """Feed RM results. Returns (role, prompt) for trader."""
        self.orch.submit_analysis(self.run_id, "research_manager", text, claims)
        packet = self.orch.get_packet(self.run_id, "trader")
        prompt = build_subagent_prompt("trader", packet)
        return ("trader", prompt)

    def collect_trader(self, claims: List[Dict], text: str = "",
                       trade: Dict[str, Any] = None) -> Tuple[str, str]:
        """Feed trader results + register trade. Returns (role, prompt) for risk."""
        self.orch.submit_analysis(self.run_id, "trader", text, claims)
        if trade:
            self.orch.register_trade(self.run_id, trade)
        packet = self.orch.get_packet(self.run_id, "risk_manager")
        prompt = build_subagent_prompt("risk_manager", packet)
        return ("risk_manager", prompt)

    def collect_risk(self, claims: List[Dict], text: str = "") -> Dict[str, Any]:
        """Feed risk results. Runs final validation + renders report.

        If the final gate blocks, returns {"needs_revision": True, ...}
        instead of dying: the coordinator revises the flagged claims
        against the original FACTs (prepare_gate_revision /
        collect_final_revision) and the pipeline continues to render.
        Only after GATE_MAX_REVISIONS failed rounds does it block hard.
        """
        self.orch.submit_analysis(self.run_id, "risk_manager", text, claims)

        gate_blocked = self.orch.gate(self.run_id, "final")
        if gate_blocked:
            run = self.orch.runs[self.run_id]
            findings = [f for f in run.findings]
            blockers = [f for f in findings
                        if f.get("status") == "FAIL"
                        ]  # 不分級：有問題就修
            rnd = self._gate_revision_rounds.get("final", 0)
            self._gate_revision_history.append({
                "stage": "final", "round": rnd, "findings": blockers,
            })
            if rnd < GATE_MAX_REVISIONS:
                return {
                    "needs_revision": True,
                    "stage": "final",
                    "revision_round": rnd,
                    "findings": blockers,
                    "blocked": False,
                }
            return {
                "error": (f"Final gate blocked after {GATE_MAX_REVISIONS} "
                          f"revision rounds - report NOT delivered"),
                "report_markdown": "",
                "sidecar": {},
                "final_findings": blockers,
                "revision_history": self._gate_revision_history,
                "blocked": True,
            }

        # Gate passed: validate the actual rendered report
        findings, scan_blocked, rendered = self.orch.validate_final(self.run_id)
        # Store for Phase 6
        self._phase6_report = rendered.get("report_markdown", "")
        self._phase6_sidecar = rendered.get("sidecar", {})
        self._phase6_findings = [f for f in findings]
        self._phase6_blocked = scan_blocked
        return {
            "report_markdown": rendered.get("report_markdown", ""),
            "sidecar": rendered.get("sidecar", {}),
            "final_findings": [f for f in findings],
            "blocked": scan_blocked,
            # Phase 6: if not blocked, caller should run prepare_phase6()
            "phase6_ready": not scan_blocked,
        }

    def collect_final_revision(self, revised_claims: List[Dict]) -> Dict[str, Any]:
        """Apply revised claims after final-gate block, re-gate, render.

        HARD BOUNDARY: revises report CONTENT only, never code. If the
        re-gate still blocks after GATE_MAX_REVISIONS rounds, returns
        blocked=True for the user to decide.

        Returns the same dict shape as collect_risk() on success, or
        {"needs_revision": True, ...} for another round.
        """
        run = self.orch.runs[self.run_id]
        revised_by_id = {c.get("claim_id"): c for c in revised_claims
                         if isinstance(c, dict) and c.get("claim_id")}
        for i, c in enumerate(run.claims):
            cid = c.get("claim_id")
            if cid in revised_by_id:
                # 欄位合併：原 claim 為底，reviser 回傳的欄位疊加上去
                #（同 collect_phase2_revision 的 BUG-002 修復）。
                new_c = dict(c)
                new_c.update(revised_by_id[cid])
                new_c["claim_id"] = cid
                new_c["_revised_in_gate_round"] = \
                    self._gate_revision_rounds.get("final", 0)
                run.claims[i] = new_c

        rnd = self._gate_revision_rounds.get("final", 0) + 1
        self._gate_revision_rounds["final"] = rnd

        gate_blocked = self.orch.gate(self.run_id, "final")
        if gate_blocked:
            findings = [f for f in run.findings]
            blockers = [f for f in findings
                        if f.get("status") == "FAIL"
                        ]  # 不分級：有問題就修
            self._gate_revision_history.append({
                "stage": "final", "round": rnd, "findings": blockers,
            })
            if rnd < GATE_MAX_REVISIONS:
                return {
                    "needs_revision": True,
                    "stage": "final",
                    "revision_round": rnd,
                    "findings": blockers,
                    "blocked": False,
                }
            return {
                "error": (f"Final gate blocked after {GATE_MAX_REVISIONS} "
                          f"revision rounds - report NOT delivered"),
                "report_markdown": "",
                "sidecar": {},
                "final_findings": blockers,
                "revision_history": self._gate_revision_history,
                "blocked": True,
            }

        # Gate passed: render and set up Phase 6
        findings, scan_blocked, rendered = self.orch.validate_final(self.run_id)
        self._phase6_report = rendered.get("report_markdown", "")
        self._phase6_sidecar = rendered.get("sidecar", {})
        self._phase6_findings = [f for f in findings]
        self._phase6_blocked = scan_blocked
        return {
            "report_markdown": rendered.get("report_markdown", ""),
            "sidecar": rendered.get("sidecar", {}),
            "final_findings": [f for f in findings],
            "blocked": scan_blocked,
            "phase6_ready": not scan_blocked,
        }

    def prepare_phase6(self) -> Tuple[str, str]:
        """Phase 6: Adversarial review. Returns (role, prompt).

        The adversarial reviewer reads the full rendered report + original
        FACTs and tries to find errors. Findings block delivery.
        """
        template = load_role_template("adversarial_reviewer")
        run = self.orch.runs[self.run_id]
        facts = run.fact_store.to_dict()

        prompt = f"{template}\n\n## 報告全文\n```\n"
        # Truncate report if too long (keep first PHASE6_MAX_REPORT_CHARS chars)
        report = self._phase6_report
        if len(report) > PHASE6_MAX_REPORT_CHARS:
            report = report[:PHASE6_MAX_REPORT_CHARS] + "\n...（截斷）"
        prompt += report
        prompt += "\n```\n\n## 原始 FACT（英文）\n```json\n"
        prompt += json.dumps(facts, ensure_ascii=False, indent=2, default=str)
        prompt += "\n```\n"
        # 第二輪起：附上上一輪的完整上下文（初審收到的完整 prompt），
        # 讓複審知道初審看到了什麼、有什麼資訊。第一輪保持乾淨 context。
        if self._phase6_revision_history:
            prev = self._phase6_revision_history[-1]
            # 初審完整 prompt（含模板、報告、FACTs）
            if prev.get("reviewer_prompt"):
                prompt += ("\n## 上一輪審查員收到的完整上下文（第 "
                           f"{prev['round']} 輪初審 prompt 全文）\n```\n")
                prompt += prev["reviewer_prompt"]
                prompt += "\n```\n"
            prompt += (f"\n## 上一輪審查發現（第 {prev['round']} 輪）\n"
                       "```json\n")
            prompt += json.dumps(prev["findings"], ensure_ascii=False,
                                 indent=2, default=str)
            prompt += "\n```\n"
            if prev.get("revisions"):
                prompt += "\n## 上一輪針對上述 findings 的修正紀錄\n```json\n"
                prompt += json.dumps(prev["revisions"], ensure_ascii=False,
                                     indent=2, default=str)
                prompt += "\n```\n"
            prompt += ("\n請驗證：上一輪的問題在新版報告中是否已確實修正"
                       "（特別注意修正是否有漏網）；同時照常做完整獨立審查，也要抓新問題。\n"
                       "**你可以獨立推翻上一輪的 finding**：如果你認為某條 finding "
                       "是誤報，直接說明理由並標註推翻，不用沿用上一輪的結論。\n")
        prompt += "\n請按上面的輸出格式，直接回傳 JSON array，不要 markdown。"
        # 存下本輪的完整 prompt，供下一輪複審參考
        if not self._phase6_revision_history:
            # 第一輪：存 prompt，下一輪會用到
            self._phase6_last_reviewer_prompt = prompt
        return ("adversarial_reviewer", prompt)

    def collect_phase6(self, findings: List[Dict]) -> Dict[str, Any]:
        """Feed Phase 6 results.

        findings go into the revision loop (up to PHASE6_MAX_REVISIONS
        rounds): the coordinator spawns one reviser per author_role via
        prepare_phase6_revision() (each role fixes its own claims),
        feeds the results to collect_phase6_revision(), then re-runs the
        reviewer.
        If findings remain after max revisions, block and report to the user.
        """
        # 不分級：所有 findings 都要修
        p0 = findings
        p1 = []
        p2 = []
        needs_fix = p0 + p1

        if needs_fix:
            self._phase6_revision_history.append({
                "round": self._phase6_revision_round,
                "findings": findings,
                # 初審完整 prompt，供下一輪複審參考完整上下文
                "reviewer_prompt": getattr(self, "_phase6_last_reviewer_prompt",
                                           ""),
            })
            if self._phase6_revision_round < PHASE6_MAX_REVISIONS:
                return {
                    "needs_revision": True,
                    "revision_round": self._phase6_revision_round,
                    "findings": needs_fix,
                    "blocked": False,
                    "report_markdown": "",
                }
            return {
                "error": (f"Phase 6 blocked after {PHASE6_MAX_REVISIONS} revision "
                          f"rounds: {len(p0)} remaining"),
                "report_markdown": "",
                "sidecar": self._phase6_sidecar,
                "adversarial_findings": findings,
                "revision_history": self._phase6_revision_history,
                "blocked": True,
            }

        # appendix, final report
        report = self._phase6_report
        if p2:
            report += "\n\n---\n\n## 附錄：對抗式覆核意見\n\n"
            report += "以下為獨立覆核員的發現，供參考：\n\n"
            for f in p2:
                sev = f.get("severity", "?")
                loc = f.get("location", "?")
                issue = f.get("issue", "?")
                fix = f.get("suggested_fix", "")
                report += f"### [{sev}] {loc}\n\n"
                report += f"- 問題：{issue}\n"
                if fix:
                    report += f"- 建議修正：{fix}\n"
                report += "\n"

        return {
            "report_markdown": report,
            "sidecar": self._phase6_sidecar,
            "final_findings": self._phase6_findings,
            "adversarial_findings": findings,
            "revision_history": self._phase6_revision_history,
            "blocked": False,
        }

    def prepare_phase6_revision(self, findings: List[Dict]) -> List[Tuple[str, str]]:
        """Build reviser prompts for findings.

        修錯的人是原作者：按每條 finding 對應 claim 的 author_role 分組，
        用該角色自己的模板來修，不再用通用的 phase6_reviser。

        Returns [(role, prompt)]. Coordinator spawns one reviser subagent
        per role with its prompt, then feeds all JSON outputs to
        collect_phase6_revision().
        """
        run = self.orch.runs[self.run_id]
        claims_by_id = {c.get("claim_id"): c for c in run.claims}

        # 數字正規化搜尋：findings 點名的 claim_ids 可能不全，從問題描述抓
        # 關鍵數字/概念，用去逗號比對找出所有出現的 claims（例：「1,620億」
        # 和「1620億」視為同一個）
        flagged_terms = set()
        for f in findings:
            issue_text = f.get("issue", "") + " " + f.get("suggested_fix", "")
            # 抓數字（含千分位逗號）：1,620億、46.2倍、13.6%
            # 正規化：去逗號＋去所有空白（「1,620 億」和「1620億」視為同一個）
            for m in re.finditer(r'[\d,]+\.?\d*\s*[億萬倍%％]', issue_text):
                term = re.sub(r'\s+', '', m.group(0)).replace(",", "")
                flagged_terms.add(term)
            # 抓引號內的關鍵概念：「在手訂單」
            for m in re.finditer(r'「([^」]{2,20})」', issue_text):
                flagged_terms.add(m.group(1))

        # 按 author_role 分組 findings
        by_role: Dict[str, List[Dict]] = {}
        by_role_claims: Dict[str, List[Dict]] = {}
        for f in findings:
            cid = f.get("claim_id")
            claim = claims_by_id.get(cid) if cid else None
            # 如果 finding 沒標 claim_id，用正規化搜尋找相關 claims
            related = [claim] if claim else []
            if not related:
                for c in run.claims:
                    norm_nospace = re.sub(r'\s+', '',
                                          c.get("text", "")).replace(",", "")
                    norm_text = c.get("text", "").replace(",", "")
                    for term in flagged_terms:
                        if not term:
                            continue
                        matched = False
                        if re.fullmatch(r'[\d.]+[億萬倍%％]?', term):
                            matched = term in norm_nospace
                        else:
                            matched = term in norm_text
                        if matched:
                            related.append(c)
                            break
            for c in related:
                role = c.get("author_role", "unknown")
                by_role.setdefault(role, []).append(f)
                if c not in by_role_claims.get(role, []):
                    by_role_claims.setdefault(role, []).append(c)

        # 每個角色一組 prompt，用該角色自己的模板
        result = []
        for role, role_findings in by_role.items():
            try:
                template = load_role_template(role)
            except Exception:
                # 找不到模板時 fallback 到通用 reviser
                template = load_role_template("phase6_reviser")
            prompt = f"{template}\n\n## 覆核員對你的 claims 找到的問題\n```json\n"
            prompt += json.dumps(role_findings, ensure_ascii=False,
                                 indent=2, default=str)
            prompt += "\n```\n\n## 你的原始 claims（被點名的）\n```json\n"
            prompt += json.dumps(by_role_claims.get(role, []),
                                 ensure_ascii=False, indent=2, default=str)
            prompt += "\n```\n\n## 原始 FACT（英文，修正時只能用這裡的數字）\n```json\n"
            prompt += json.dumps(run.fact_store.to_dict(),
                                 ensure_ascii=False, indent=2, default=str)
            prompt += ("\n```\n\n你是這些 claims 的原作者。請修正上面列出的問題，"
                       "保持你的論述方向和風格，只修正事實錯誤。\n"
                       "如果某條 finding 是誤報，標註 \"dispute\": true 並說明理由。\n"
                       "直接回傳 JSON array 的修正後 claims，不要 markdown。")
            result.append((role, prompt))
        return result

    def prepare_phase6_verify(self) -> Tuple[str, str]:
        """Phase 6 外部事實驗證（兩段式第一段：先驗真）。

        用 Gemini 3.8 Flash（獨立模型家族）抽取並驗證報告中的
        負重事實（估值分母、指引歸屬、YoY 基期、事件日期、口徑斷言）。
        Gemini 自己從報告抽取 15–30 條斷言並獨立驗證。

        Returns (verifier_prompt, "") — prompt 直接餵給 gemini_client。
        """
        template = load_role_template("fact_verifier")
        # 不截斷：全文驗證需要完整報告
        report = self._phase6_report

        prompt = f"{template}\n\n## 待驗證報告全文\n```\n"
        prompt += report
        prompt += "\n```\n\n請按上面的輸出格式，直接回傳 JSON array，不要 markdown。"
        return (prompt, "")

    def merge_verifier_findings(self, verifier_output: List[Dict]) -> List[Dict]:
        """verifier mismatch → V-finding（小函數）。

        把 Gemini 回傳的 mismatch 轉成管線 finding 格式，
        category 標為 V-fact_verification。
        """
        findings = []
        for m in verifier_output:
            findings.append({
                
                "category": "V-fact_verification",
                "location": m.get("location", "?"),
                "issue": (
                    f"[外部驗真] {m.get('issue', '?')} "
                    f"(報告斷言：{m.get('assertion', '?')[:80]}；"
                    f"驗證員認為正確值：{m.get('correct_value', '?')[:80]}；"
                    f"信心度：{m.get('confidence', '?')})"
                ),
                "evidence": m.get("correct_value", ""),
                "suggested_fix": f"核對後修正為：{m.get('correct_value', '?')}",
                "source": "fact_verifier (gemini-3.8-flash)",
            })
        return findings

    def collect_phase6_revision(self, revised_claims: List[Dict]) -> Dict[str, Any]:
        """Apply revised claims, re-render, re-validate.

        HARD BOUNDARY: this revises report CONTENT (claim text/numbers/refs)
        only. It never touches pipeline code, role templates, FACT definitions,
        or any file. If a finding cannot be fixed by changing claims, the
        loop blocks and reports to the user — it never escalates to code
        changes. Code changes always need the user's explicit approval
        (see AGENTS.md).

        Returns {"phase6_prompt": (role, prompt)} for the next reviewer round,
        so the coordinator can re-run prepare_phase6() and the reviewer.
        """
        run = self.orch.runs[self.run_id]
        # BUG-1 修復（2026-10-17）：重複 claim_id 不再靜默 last-wins。
        # 同一 claim_id 出現多次時：優先採用帶實質修訂內容的條目；
        # 若兩條都有實質內容且文本不同，明確報錯（不靜默二選一）；
        # 其餘情況保留第一條並記警告。警告隨回傳值交給 coordinator。
        def _is_stub(c: Dict) -> bool:
            return bool(c.get("dispute")) and not (c.get("text") or "").strip()

        revised_by_id: Dict[str, Dict] = {}
        dedup_warnings: List[str] = []
        for c in revised_claims:
            if not isinstance(c, dict):
                continue
            cid = c.get("claim_id")
            if not cid:
                continue
            if cid not in revised_by_id:
                revised_by_id[cid] = c
                continue
            prev = revised_by_id[cid]
            prev_stub, cur_stub = _is_stub(prev), _is_stub(c)
            prev_text = (prev.get("text") or "").strip()
            cur_text = (c.get("text") or "").strip()
            if not prev_stub and not cur_stub and prev_text != cur_text:
                raise ValueError(
                    "collect_phase6_revision: claim_id %r 收到兩條實質內容"
                    "不同的修訂，無法合併（前: %.60r，後: %.60r）。"
                    "請檢查 reviser 輸出是否一稿多投。" % (cid, prev_text, cur_text)
                )
            if cur_stub and not prev_stub:
                # 後到的是路由駁回 stub，保留前面的實質修訂
                dedup_warnings.append(
                    "claim_id %r 重複：丟棄後到的 dispute stub，保留實質修訂" % cid)
            elif prev_stub and not cur_stub:
                # 前面的是 stub，後到的是實質修訂，採用後者
                dedup_warnings.append(
                    "claim_id %r 重複：前面的 dispute stub 被實質修訂取代" % cid)
                revised_by_id[cid] = c
            else:
                dedup_warnings.append(
                    "claim_id %r 重複（皆為 stub 或內容相同）：保留第一條" % cid)
        applied = 0
        for i, c in enumerate(run.claims):
            cid = c.get("claim_id")
            if cid in revised_by_id:
                # 欄位合併：原 claim 為底，reviser 回傳的欄位疊加上去。
                # 這讓 reviser 可以修正 subsection（子標題錯位）而不丟失
                # author_role、purpose 等欄位（BUG-002 修復）。
                new_c = dict(c)
                new_c.update(revised_by_id[cid])
                new_c["claim_id"] = cid  # claim_id 不可被改寫
                # Keep a revision trail on the claim itself
                new_c["_revised_in_phase6_round"] = self._phase6_revision_round
                run.claims[i] = new_c
                applied += 1

        self._phase6_revision_round += 1

        # 記錄本輪修了什麼：每條 finding 對應的修正，供下一輪審查員驗證
        # （用去重後的清單，避免被丟棄的 stub 污染歷史）
        if self._phase6_revision_history:
            self._phase6_revision_history[-1]["revisions"] = [
                {"claim_id": c.get("claim_id"),
                 "text": c.get("text", ""),
                 "dispute": c.get("dispute", False),
                 "dispute_reason": c.get("dispute_reason", "")}
                for c in revised_by_id.values() if isinstance(c, dict)
            ]
            if dedup_warnings:
                self._phase6_revision_history[-1]["dedup_warnings"] = dedup_warnings

        # Re-render the report from updated claims
        from . import reporting
        snap = self.orch.snapshot(self.run_id)
        rendered = reporting.render_report(snap)
        self._phase6_report = rendered.get("report_markdown", "")
        self._phase6_sidecar = rendered.get("sidecar", {})

        # Re-run final validation on the revised report
        findings, scan_blocked, _ = self.orch.validate_final(self.run_id)
        self._phase6_findings = [f for f in findings]
        self._phase6_blocked = scan_blocked

        role, prompt = self.prepare_phase6()
        return {
            "applied_revisions": applied,
            "revision_round": self._phase6_revision_round,
            "phase6_prompt": (role, prompt),
            "scan_blocked": scan_blocked,
            "dedup_warnings": dedup_warnings,
        }


def run_pipeline(ticker: str, manifest: Dict[str, Any]):
    """Entry point. Returns a PipelineRunner ready for Phase 2.

    Usage (by assistant):
        runner = run_pipeline("AAPL", manifest)
        prompts = runner.prepare(...)  # Phase 2 prompts
        # ... spawn subagents, collect claims ...
        prompts = runner.collect_phase2(claims_by_role)
        # ... etc
    """
    runner = PipelineRunner()
    return runner
