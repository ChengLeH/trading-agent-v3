#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 pipeline.py — stage state machine + Analyst Packet builder + orchestrator.
Prototype A scope: ingest -> facts/calcs -> evidence -> packets ->
2 analyses (Market, Fundamentals) -> claims -> validation.

Agent execution (user decision 2026-10-15): a Python orchestrator drives
subagents. Each role only ever sees its Analyst Packet, never raw sources."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .sources import load_manifest, default_registry, SourceError
from .facts import Fact, FactStore, CalcStore
from . import calculations as K
from . import builders as B
from .evidence import EvidenceStore
from .validation import ValidationService, Finding, CHECKERS

# Packet size guard (spec §7): roles must not expand to raw sources.
MAX_PACKET_CHARS = 12000


@dataclass
class Claim:
    claim_id: str
    text: str
    claim_type: str   # source_fact|derived_fact|semantic_fact|interpretation|forecast|trade_assumption
    refs: List[str]
    author_role: str
    confidence: float = 0.0
    # --- presentation metadata (2026-10-15): report subsection grouping ONLY ---
    # Does NOT affect FACT/CALC/EVIDENCE correctness, grounding, or validation.
    # Vocabulary per role (renderer maps to Chinese headers):
    #   market_analyst: "trend" | "momentum_volatility" | "support_resistance"
    #   fundamentals_analyst: "profitability_growth" | "balance_sheet" |
    #                         "cash_flow_capex" | "valuation"
    #   news_analyst: "timeline" | "catalysts" | "risks"
    #   social_analyst: "sentiment_overview" | "reflexivity_risk"
    #   bull/bear: "scenario" | "counter_response"
    #   research_manager: "adjudication"
    #   risk_manager: "aggressive" | "conservative" | "neutral" | "final_tradeoff"
    #   trader: "blueprint"
    # None = uncategorized; renderer puts these under "其他論述".
    subsection: Optional[str] = None
    # --- presentation metadata (2026-10-15): report role for _thesis_block ONLY ---
    # claim_type = validation semantics (source_fact|derived_fact|semantic_fact).
    # purpose    = report role (what this claim does in the 投行核心精要).
    # subsection = second-level section grouping.
    # Vocabulary (renderer maps to Chinese headers; unknown -> "其他論述", no crash):
    #   "thesis" | "evidence" | "contradiction" | "reversal" |
    #   "interpretation" | "forward" | "valuation" | "scenario" |
    #   "verdict" | "adjudication" | "plan" | "blueprint"
    purpose: Optional[str] = None

    def to_dict(self):
        return asdict(self)


@dataclass
class RunState:
    run_id: str
    ticker: str
    stage: str = "created"
    manifest: Dict[str, Any] = field(default_factory=dict)
    records: List[Any] = field(default_factory=list)
    trade: Dict[str, Any] = field(default_factory=dict)
    fact_store: FactStore = field(default_factory=FactStore)
    calc_store: CalcStore = field(default_factory=CalcStore)
    evidence_store: EvidenceStore = field(default_factory=EvidenceStore)
    packets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    analyses: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    claims: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)


def _clip(text: str, limit: int = MAX_PACKET_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[clipped {len(text)-limit} chars by packet guard]"


def build_packet(run: RunState, role: str) -> Dict[str, Any]:
    """Assemble a small, role-scoped Analyst Packet (spec §7)."""
    facts = run.fact_store.to_dict()
    if role == "market_analyst":
        packet = {
            "role": role,
            "market_facts": {k: v for k, v in facts.items()
                             if v["concept"] in (
                                 "Close", "SMA20", "SMA50", "SMA200", "RSI14",
                                 "ATR14", "High52w", "Low52w", "Volume",
                                 "ReturnW", "ReturnM")},
            "calcs": {k: v for k, v in run.calc_store.to_dict().items()
                      if v["calc_type"] in ("sma", "rsi", "return_cc")},
            "recent_evidence": [
                e.to_dict() for e in
                run.evidence_store.find(topic="price_action")[:5]],
            "note": "No raw SEC filings in this packet.",
        }
    elif role == "fundamentals_analyst":
        # only key concepts, last 2 FYs; strip verbose provenance for size
        _keep = ("Revenue", "GrossProfit", "OperatingIncome", "NetIncome",
                 "BasicEPS", "DilutedEPS", "OperatingCashFlow", "Capex",
                 "FreeCashFlow", "Cash", "TotalDebt", "Assets", "Liabilities",
                 "Equity", "Inventory")
        _facts = {k: v for k, v in facts.items()
                  if v["concept"] in _keep}
        _by_concept: Dict[str, List[str]] = {}
        for k in _facts:
            _by_concept.setdefault(_facts[k]["concept"], []).append(k)
        _sel = {}
        for concept, keys in _by_concept.items():
            for k in sorted(keys)[-2:]:
                f = dict(_facts[k])
                # strip long provenance; keep ids for grounding
                f.pop("source_hash", None)
                f.pop("source_pointer", None)
                _sel[k] = f
        packet = {
            "role": role,
            "financial_facts": _sel,
            "calcs": {k: v for k, v in run.calc_store.to_dict().items()
                      if v["calc_type"] in (
                          "cagr", "margin", "ev", "per_share", "fcf",
                          "growth")},
            "evidence": [e.to_dict() for e in
                         run.evidence_store.find()[:10]],
            "note": "Full filing available only on evidence miss; ask orchestrator.",
        }
    elif role == "news_analyst":
        packet = {
            "role": role,
            "news_items": [
                e.to_dict() for e in
                run.evidence_store.find(topic="news")[:10]],
            "note": "Each item carries published date; check temporal grounding.",
        }
    elif role == "social_analyst":
        packet = {
            "role": role,
            "social_items": [
                e.to_dict() for e in
                run.evidence_store.find(topic="social")[:10]],
            "note": "Sentiment is discussion signal, not a price fact.",
        }
    elif role in ("bull", "bear"):
        # P1 fix (2026-10-15): slim bull/bear packet at creation so the 12K
        # hard limit actually holds. Previously passed the entire run.analyses
        # dict + full claim dicts (~78K+ chars); the truncation fallback only
        # knew about evidence/news/social lists and never touched these.
        packet = {
            "role": role,
            "phase1_analyses": {
                r: {
                    "text": (a.get("text", "")[:2000]
                             if isinstance(a, dict) else str(a)[:2000]),
                    "claim_ids": (a.get("claim_ids", [])
                                  if isinstance(a, dict) else []),
                }
                for r, a in run.analyses.items()
                if r in ("market_analyst", "fundamentals_analyst",
                         "news_analyst", "social_analyst")
            },
            "key_claims": [
                {k: c.get(k) for k in (
                    "claim_id", "purpose", "subsection", "text",
                    "confidence", "refs")}
                for c in run.claims
                if c.get("purpose") in ("thesis", "evidence", "valuation")
            ][:30],  # Max 30 key claims, slimmed dicts
            "stance": ("Build the strongest BULL case from Phase 1 output only."
                       if role == "bull" else
                       "Build the strongest BEAR case from Phase 1 output only."),
        }
    elif role == "research_manager":
        packet = {
            "role": role,
            "bull": run.analyses.get("bull", {}),
            "bear": run.analyses.get("bear", {}),
            "directive": ("Adjudicate bull vs bear. Keep only claims that "
                          "survive validation; flag unresolved conflicts."),
        }
    elif role == "trader":
        packet = {
            "role": role,
            "thesis": run.analyses.get("research_manager", {}),
            "market_facts": {k: v for k, v in run.fact_store.to_dict().items()
                             if v["concept"] in (
                                 "Close", "ATR14", "High52w", "Low52w")},
            "directive": ("Propose entry/stop/target/position. All numbers "
                          "must come from facts or the kernel, never invented."),
        }
    elif role == "risk_manager":
        # Bug 2 fix: Risk packet reads run.trade directly (set by register_trade)
        # + full trader claims from run.analyses["trader"]["claims"]
        trader_analysis = run.analyses.get("trader", {})
        trader_claims = trader_analysis.get("claims", [])
        trade_plan = getattr(run, "trade", {}) or {}
        # Also include claims the trade actually depends on (via refs)
        wanted_refs = set()
        for c in trader_claims:
            wanted_refs.update(c.get("refs", []))
        slim = [
            {"claim_id": c.get("claim_id"), "text": c.get("text", "")[:160],
             "refs": c.get("refs", []), "author_role": c.get("author_role")}
            for c in run.claims
            if c.get("claim_id") in wanted_refs
            or c.get("claim_type") == "trade_assumption"]
        packet = {
            "role": role,
            "trade_plan": {"trade": trade_plan,
                           "analysis_text": trader_analysis.get("text", ""),
                           "trader_claims": trader_claims},
            "dependent_claims": slim,
            "total_claims_in_run": len(run.claims),
            "directive": ("Veto or approve. Check sizing math via kernel; "
                          "surface any ungrounded assumption."),
        }
    else:
        raise ValueError(f"Prototype B supports market/fundamentals/news/social/bull/bear/research_manager/trader/risk_manager: {role}")
    raw = json.dumps(packet, ensure_ascii=False, default=str)
    packet["_packet_chars"] = len(raw)
    if len(raw) > MAX_PACKET_CHARS:
        # Bug 8 fix: true hard limit. First clip evidence text, then if
        # still over, drop oldest evidence items (keep FACTs/CALCs intact).
        for e in packet.get("evidence", []) + packet.get("recent_evidence", []):
            if isinstance(e, dict) and "claim_candidate" in e:
                e["claim_candidate"] = e["claim_candidate"][:500]
        # P1 fix (2026-10-15): also clip key_claims text. A single huge claim
        # would otherwise survive the drop loop (which keeps at least 1).
        for kc in packet.get("key_claims", []):
            if isinstance(kc, dict) and "text" in kc:
                if len(kc["text"]) > 1000:
                    kc["text"] = kc["text"][:1000] + "...[truncated]"
        raw = json.dumps(packet, ensure_ascii=False, default=str)
        if len(raw) > MAX_PACKET_CHARS:
            # Drop oldest evidence first; FACTs are never dropped
            for key in ("evidence", "recent_evidence", "news_items",
                        "social_items", "dependent_claims", "key_claims"):
                items = packet.get(key)
                if isinstance(items, list) and items:
                    # Keep at least 1, drop from the end (oldest last)
                    while len(raw) > MAX_PACKET_CHARS and len(items) > 1:
                        items.pop()
                        raw = json.dumps(packet, ensure_ascii=False,
                                         default=str)
                if len(raw) <= MAX_PACKET_CHARS:
                    break
            # P1 fix (2026-10-15): bull/bear phase1_analyses is a dict, not a
            # list, so the loop above never touches it. Halve analyst texts,
            # then drop whole analyst entries (keep at least 1).
            if len(raw) > MAX_PACKET_CHARS and "phase1_analyses" in packet:
                pa = packet["phase1_analyses"]
                if isinstance(pa, dict):
                    for role_key in list(pa.keys()):
                        if len(raw) <= MAX_PACKET_CHARS:
                            break
                        txt = pa[role_key].get("text", "")
                        if len(txt) > 500:
                            pa[role_key]["text"] = txt[:500] + "...[truncated]"
                            raw = json.dumps(packet, ensure_ascii=False,
                                             default=str)
                    while len(raw) > MAX_PACKET_CHARS and len(pa) > 1:
                        pa.popitem()
                        raw = json.dumps(packet, ensure_ascii=False,
                                         default=str)
            packet["_truncated"] = True
            packet["_packet_chars"] = len(raw)
    return packet


class Orchestrator:
    """Minimal stage machine for Prototype A."""

    def __init__(self):
        self.validation = ValidationService()
        self.runs: Dict[str, RunState] = {}

    # -- API (spec §12, prototype subset) -------------------------------
    def start_run(self, subject: Dict[str, Any],
                  manifest: Dict[str, Any]) -> str:
        m = load_manifest(manifest)
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        self.runs[run_id] = RunState(run_id=run_id,
                                     ticker=m["subject"]["ticker"],
                                     stage="ingest", manifest=m)
        return run_id

    def ingest(self, run_id: str, registry=None) -> List[Any]:
        """Run typed adapters over the manifest. Returns records/errors."""
        run = self.runs[run_id]
        reg = registry or default_registry()
        out = []
        for entry in run.manifest["sources"]:
            rec = reg.adapt(entry, run.manifest["subject"])
            out.append(rec)
        run.stage = "facts"
        run.records = out
        return out

    def build_facts(self, run_id: str) -> Dict[str, Any]:
        """Production path: SourceRecords -> FACT/CALC stores via builders.

        No hand-written fact_store.add() in production. Returns a report of
        what was built and what was marked as limitation.
        """
        run = self.runs[run_id]
        report: Dict[str, Any] = {"facts": 0, "calcs": 0, "limitations": []}
        for rec in getattr(run, "records", []):
            if type(rec).__name__ == "SourceError":
                report["limitations"].append(
                    f"{rec.source_id}: adapter error [{rec.code}], skipped")
                continue
            if rec.type == "openbb_ohlcv":
                facts, calcs, lims = B.build_market_facts(
                    rec.payload["bars"], rec.id, rec.source_hash,
                    as_of=run.manifest.get("as_of"))
            elif rec.type == "openbb_fundamentals":
                fund = rec.payload
                facts, calcs, lims = B.build_fundamental_facts(
                    fund.get("income", []), fund.get("balance", []),
                    fund.get("cash_flow", []), rec.id, rec.source_hash)
            elif rec.type == "news_json":
                # Bug 5 fix: populate evidence_store from news items
                # so news_analyst packet is not empty
                from .evidence import EvidencePacket
                facts, calcs, lims = [], [], []
                for i, item in enumerate(rec.payload.get("items", [])):
                    ev = EvidencePacket(
                        evidence_id=f"{rec.id}.news{i}",
                        claim_candidate=item.get("title", "") + (
                            f" — {item.get('summary', '')}"
                            if item.get("summary") else ""),
                        source_id=rec.id,
                        source_type="news_json",
                        source_span=item.get("url", ""),
                        published_or_filing_time=item.get("published", ""),
                        topic="news",
                        extraction_method="assistant browser.search",
                        source_hash=rec.source_hash,
                    )
                    run.evidence_store.add(ev)
                report["evidence"] = report.get("evidence", 0) + len(
                    rec.payload.get("items", []))
            elif rec.type == "social_json":
                # Bug 5 fix: populate evidence_store from social items
                from .evidence import EvidencePacket
                facts, calcs, lims = [], [], []
                for i, item in enumerate(rec.payload.get("items", [])):
                    ev = EvidencePacket(
                        evidence_id=f"{rec.id}.social{i}",
                        claim_candidate=item.get("text", item.get("title", "")),
                        source_id=rec.id,
                        source_type="social_json",
                        source_span=item.get("url", ""),
                        published_or_filing_time=item.get("published", ""),
                        topic="social",
                        extraction_method="assistant social.search",
                        source_hash=rec.source_hash,
                    )
                    run.evidence_store.add(ev)
                report["evidence"] = report.get("evidence", 0) + len(
                    rec.payload.get("items", []))
            else:
                continue
            for f in facts:
                run.fact_store.add(f)
            for c in calcs:
                run.calc_store.add(c["cid"], c["value"], c["calc_type"],
                                   c["input_refs"], c["unit"], c["meta"])
            report["facts"] += len(facts)
            report["calcs"] += len(calcs)
            report["limitations"].extend(lims)
        run.stage = "evidence"
        return report

    def register_trade(self, run_id: str, trade: Dict[str, Any]) -> List[str]:
        """Production path: trader structured plan -> kernel -> CalcStore."""
        run = self.runs[run_id]
        created = B.register_trade_plan(trade, run.fact_store,
                                        run.calc_store)
        run.trade = trade
        return created

    def get_packet(self, run_id: str, role: str) -> Dict[str, Any]:
        run = self.runs[run_id]
        packet = build_packet(run, role)
        run.packets[role] = packet
        return packet

    def submit_analysis(self, run_id: str, role: str,
                        analysis_text: str,
                        claims: List[Dict[str, Any]]):
        run = self.runs[run_id]
        # Bug 6 fix: store full structured claims, not just IDs
        run.analyses[role] = {"text": analysis_text,
                              "claim_ids": [c["claim_id"] for c in claims],
                              "claims": claims}
        run.claims.extend(claims)
        return len(claims)

    def validate_stage(self, run_id: str, stage: str):
        run = self.runs[run_id]
        ctx = {
            "manifest": run.manifest,
            "records": getattr(run, "records", []),
            "trade": getattr(run, "trade", {}),
            "fact_store": run.fact_store,
            "calc_store": run.calc_store,
            "evidence_store": run.evidence_store,
            "claims": run.claims,
            "report_text": "",
            "known_ids": [],
            "as_of": run.manifest.get("as_of", "9999-12-31"),
            # as_of_consistency tolerance: per-analysis, from manifest context.
            # Never defaulted here — the checker fails closed if missing.
            "freshness_tolerance_days": run.manifest.get(
                "freshness_tolerance_days"),
            "release_events": getattr(run, "release_events", []),
        }
        findings, blocked = self.validation.validate_stage(stage, ctx)
        run.findings = [f.to_dict() for f in findings]
        return findings, blocked

    # -- Phase gates (Prototype B: full 4-phase pipeline) ----------------
    # Each gate validates the stage; blocking findings stop the run.
    # as_of comes from the manifest and bounds temporal grounding.
    def gate(self, run_id: str, stage: str) -> bool:
        """Run a stage gate. Returns True if the stage is blocked."""
        _, blocked = self.validate_stage(run_id, stage)
        run = self.runs[run_id]
        if blocked:
            run.stage = f"blocked:{stage}"
        return blocked

    def advance(self, run_id: str, next_stage: str):
        run = self.runs[run_id]
        if run.stage.startswith("blocked:"):
            raise RuntimeError(f"run {run_id} is blocked at {run.stage}; "
                               f"resolve findings before advancing")
        run.stage = next_stage
        return next_stage

    def snapshot(self, run_id: str) -> Dict[str, Any]:
        """Read-only snapshot for the Independent Reviewer (spec §13)."""
        run = self.runs[run_id]
        return {
            "run_id": run_id, "ticker": run.ticker, "stage": run.stage,
            # Bug 3 fix: snapshot now carries as_of and trade
            "as_of": run.manifest.get("as_of"),
            "trade": getattr(run, "trade", None),
            "fact_snapshot_hash": run.fact_store.snapshot_hash(),
            "facts": run.fact_store.to_dict(),
            "calcs": run.calc_store.to_dict(),
            "evidence": run.evidence_store.to_dict(),
            "claims": run.claims,
            "findings": run.findings,
        }

    def validate_final(self, run_id: str) -> tuple:
        """Render the final report for Phase 6 review.

        (2026-10-16: numeric_surface_scan was deleted with the keyword-checker
        purge; its direct call here caused a KeyError crash. Report-text
        scanning is now covered semantically by the Phase 6 reviewer.)"""
        from . import reporting
        run = self.runs[run_id]
        snap = self.snapshot(run_id)
        rendered = reporting.render_report(snap)
        return [], False, rendered
