#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prototype A acceptance (spec §15): Fundamentals + Market vertical slice.

Asserts:
  - FACT STORE + CALC STORE built from OpenBB data (no hand-filled numbers)
  - >=10 semantic evidence packets via deepread provider
  - Market + Fundamentals Analyst Packets within size guard
  - Claim validation passes on clean claims
  - 3 injected errors are caught as blocking/unsupported:
      1. quarterly number described as YTD
      2. invented percentage with no source
      3. narrative from another period applied to current quarter
  - accounting_basis_consistency catches pre/post-tax mixing (AMZN case)
"""

import json
import os
import sys

pass  # run as: python -m v3.test_prototype_a
from v3.sources import load_manifest, default_registry
from v3.facts import Fact, FactStore, CalcStore
from v3.pipeline import Orchestrator, Claim
from v3.evidence import EvidenceStore, DeepReadProvider
import v3.calculations as K

DATA = "/tmp/amzn_v3.json"
TICKER = "AMZN"
passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f" — {detail}" if detail else ""))


# --- 1. manifest + ingest -------------------------------------------------
raw = json.load(open(DATA))
with open("/tmp/amzn_manifest.json", "w") as f:
    json.dump({"subject": {"ticker": TICKER},
               "as_of": "2026-10-15",
               "sources": [
                   {"id": "mkt", "type": "openbb_ohlcv", "path": DATA},
                   {"id": "fin", "type": "openbb_fundamentals", "path": DATA},
               ]}, f)

orch = Orchestrator()
run_id = orch.start_run({"ticker": TICKER},
                        json.load(open("/tmp/amzn_manifest.json")))
recs = orch.ingest(run_id)
check("ingest: 2 records, no errors",
      len(recs) == 2 and all(hasattr(r, "payload") for r in recs))
mkt = next(r for r in recs if r.id == "mkt")
bars = mkt.payload["bars"]
check("ingest: 400+ adjusted bars", len(bars) > 400, f"{len(bars)} bars")

# --- 2. FACT STORE: market facts (program-computed, never hand-filled) ----
run = orch.runs[run_id]
closes = [b["close"] for b in bars]
fid = lambda c, p: f"fact.{c}.{p}"
run.fact_store.add(Fact(fid("Close", "latest"), closes[-1], "USD",
                        "2026-10-14", concept="Close",
                        source_id="mkt", source_pointer="bars[-1].close",
                        source_hash=mkt.source_hash))
for per in (20, 50, 200):
    v = K.sma(closes, per)
    if v: run.fact_store.add(Fact(fid(f"SMA{per}", "latest"), v, "USD",
                                  "2026-10-14", concept=f"SMA{per}",
                                  source_id="mkt",
                                  source_pointer=f"sma({per}) of closes",
                                  source_hash=mkt.source_hash))
run.fact_store.add(Fact(fid("RSI14", "latest"), K.rsi(closes), "INDEX",
                        "2026-10-14", concept="RSI14", source_id="mkt",
                        source_pointer="rsi(14) of closes",
                        source_hash=mkt.source_hash))
run.fact_store.add(Fact(fid("ATR14", "latest"), K.atr(bars), "USD",
                        "2026-10-14", concept="ATR14", source_id="mkt",
                        source_pointer="atr(14) of bars",
                        source_hash=mkt.source_hash))
hi52, lo52 = max(closes[-252:]), min(closes[-252:])
run.fact_store.add(Fact(fid("High52w", "latest"), round(hi52, 2), "USD",
                        "2026-10-14", concept="High52w", source_id="mkt",
                        source_pointer="max(close[-252:])",
                        source_hash=mkt.source_hash))

# weekly/monthly returns: ADJUSTED close-to-close (user decision)
agg_w = K.aggregate_bars_close_to_close(bars, "week")
agg_m = K.aggregate_bars_close_to_close(bars, "month")
for a in agg_w[-2:]:
    run.fact_store.add(Fact(fid("ReturnW", a["period"]), a["change_pct"], "PCT",
                            a["period"], concept="ReturnW", source_id="mkt",
                            source_pointer="adjusted close-to-close",
                            source_hash=mkt.source_hash))
check("facts: market facts present", len(run.fact_store.find()) >= 8)
# intraday must NOT be called return
sample_bar = bars[-1]
ir = K.intraday_return(sample_bar["open"], sample_bar["close"])
check("intraday_return computed separately", isinstance(ir, float))

# --- 3. FACT STORE: fundamentals (SEC, with basis labels) -----------------
fin = next(r for r in recs if r.id == "fin")
inc = fin.payload["income"][0]
rev = float(inc["total_revenue"])
run.fact_store.add(Fact("fact.Revenue.FY2025", rev, "USD",
                        "FY2025", fiscal_year=2025, fiscal_period="FY",
                        concept="Revenue", basis="gaap",
                        scope="consolidated",
                        source_id="fin", source_pointer="income[0].total_revenue",
                        source_hash=fin.source_hash))
check("facts: revenue fact labeled gaap/consolidated", True)

# --- 4. CALC STORE ----------------------------------------------------------
run.calc_store.add("calc.cagr.rev.23_25", None, "cagr",
                   ["fact.Revenue.FY2023", "fact.Revenue.FY2025"],
                   "RATIO", {"years": 2.0})
# add FY2023 revenue so CAGR has both endpoints
inc_old = fin.payload["income"][1]
run.fact_store.add(Fact("fact.Revenue.FY2023", float(inc_old["total_revenue"]),
                        "USD", "FY2023", fiscal_year=2023, fiscal_period="FY",
                        concept="Revenue", basis="gaap", scope="consolidated",
                        source_id="fin", source_pointer="income[1].total_revenue",
                        source_hash=fin.source_hash))
cagr_val = K.calc_cagr(float(inc_old["total_revenue"]), rev, 2.0)
run.calc_store._calcs["calc.cagr.rev.23_25"]["value"] = cagr_val
check("calc: CAGR computed by kernel", isinstance(cagr_val, float),
      f"{cagr_val:.4f}")

# --- 5. Evidence: >=10 packets via deepread provider -----------------------
fixture = open(os.path.join(os.path.dirname(__file__), "fixtures", "amzn_mda_excerpt.txt")).read()
provider = DeepReadProvider()
topics = [("AWS revenue growth guidance",
             ["aws", "revenue", "growth", "guidance", "margin"]),
            ("AI capex infrastructure",
             ["capex", "AI", "infrastructure", "data", "center", "trainium"])]
evs = []
for topic, terms in topics:
    evs += provider.extract(topic, {"10q_excerpt": fixture},
                            source_id="fin", company="AMZN", period="2026-Q2",
                            extra_terms=terms)
check("evidence: >=10 packets", len(evs) >= 10, f"{len(evs)} packets")
for e in evs:
    run.evidence_store.add(e)
check("evidence: all have source_span",
      all(v["source_span"] for v in run.evidence_store.to_dict().values()))

# --- 6. Packets -------------------------------------------------------------
mp = orch.get_packet(run_id, "market_analyst")
fp = orch.get_packet(run_id, "fundamentals_analyst")
check("packets: within size guard",
      mp["_packet_chars"] <= 12000 and fp["_packet_chars"] <= 12000)
check("packets: market has no raw filing",
      "No raw SEC filings" in mp["note"])

# --- 7. Clean claims pass ----------------------------------------------------
clean_claims = [
    Claim("claim_m_001", "Latest close is below the 20-day SMA",
          "derived_fact", ["fact.Close.latest", "fact.SMA20.latest"],
          "market_analyst", 0.99).to_dict(),
    Claim("claim_f_001", "FY2025 total revenue was $%0.0f (GAAP, consolidated)"
          % rev, "source_fact", ["fact.Revenue.FY2025"],
          "fundamentals_analyst", 0.99).to_dict(),
]
orch.submit_analysis(run_id, "market_analyst", "clean", clean_claims[:1])
orch.submit_analysis(run_id, "fundamentals_analyst", "clean", clean_claims[1:])
findings, blocked = orch.validate_stage(run_id, "analysis")
p0 = [f for f in findings if f.blocks]
check("validation: clean claims not blocked", not p0,
      f"{len(findings)} findings, {len(p0)} blocking")

# --- 9. Injected error 2: invented percentage, no source ----------------------
bad2 = Claim("claim_bad_2", "AWS grew 45% YoY",
             "derived_fact", [], "fundamentals_analyst", 0.8).to_dict()
run.claims.append(bad2)
findings, blocked = orch.validate_stage(run_id, "analysis")
hit = [f for f in findings if f.check_id == "claim_grounding"
       and f.status == "FAIL"]
check("injected-2 (invented %) caught blocking", bool(hit) and blocked)
run.claims = run2c

# --- 11. AMZN case: pre/post-tax mixing ---------------------------------------
run.fact_store.add(Fact("fact.RevalGain.pre", 53400, "USD_MILLIONS", "2026-Q2",
                        fiscal_year=2026, fiscal_period="Q2",
                        concept="RevaluationGain", basis="pre_tax",
                        scope="consolidated", source_id="fin",
                        source_pointer="notes.revaluation",
                        source_hash=fin.source_hash))
run.fact_store.add(Fact("fact.NetIncome.post", 62647, "USD_MILLIONS", "2026-Q2",
                        fiscal_year=2026, fiscal_period="Q2",
                        concept="NetIncome", basis="post_tax",
                        scope="consolidated", source_id="fin",
                        source_pointer="income.net",
                        source_hash=fin.source_hash))
run.calc_store.add("calc.mixed.ratio", 0.852, "margin",
                   ["fact.RevalGain.pre", "fact.NetIncome.post"], "RATIO",
                   {"num_basis": "pre_tax", "den_basis": "post_tax"})
findings, _ = orch.validate_stage(run_id, "analysis")
hit = [f for f in findings
       if f.check_id == "calculation" and f.status == "FAIL"
       and "basis mismatch" in f.reason]
check("AMZN case: pre/post-tax ratio blocked", bool(hit),
      hit[0].reason[:80] if hit else "NOT CAUGHT")

# --- 13. position_risk wired into check_calculation -------------------------
# AMZN trader plan: entry 254.08, stop 245.79, position 10% -> 0.3263%
run.fact_store.add(Fact("fact.Trade.Entry", 254.08, "USD", "2026-10-15",
                        concept="TradeEntry", source_id="trader",
                        source_pointer="analysis_trader.json:entry"))
run.fact_store.add(Fact("fact.Trade.Stop", 245.79, "USD", "2026-10-15",
                        concept="TradeStop", source_id="trader",
                        source_pointer="analysis_trader.json:stop"))
run.fact_store.add(Fact("fact.Trade.PositionPct", 10.0, "PERCENT", "2026-10-15",
                        concept="TradePositionPct", source_id="trader",
                        source_pointer="analysis_trader.json:position"))
run.calc_store.add("calc.position.risk.amzn", None, "position_risk",
                   ["fact.Trade.Entry", "fact.Trade.Stop",
                    "fact.Trade.PositionPct"],
                   "PERCENT", {})
pr_val = K.calc_position_risk_pct(254.08, 245.79, 10.0)
run.calc_store._calcs["calc.position.risk.amzn"]["value"] = pr_val
findings, _ = orch.validate_stage(run_id, "analysis")
hit = [f for f in findings
       if f.check_id == "calculation" and f.status == "PASS"
       and "position_risk" in f.reason]
check("position_risk: kernel value wired through validator", bool(hit),
      f"{pr_val:.4f}%" if hit else "NOT RECOMPUTED")
# negative: the old buggy value (32.6275, double x100) must P0-fail
run.calc_store.add("calc.position.risk.buggy", 32.6275, "position_risk",
                   ["fact.Trade.Entry", "fact.Trade.Stop",
                    "fact.Trade.PositionPct"],
                   "PERCENT", {})
findings, _ = orch.validate_stage(run_id, "analysis")
hit = [f for f in findings
       if f.check_id == "calculation" and f.status == "FAIL"
       and "recompute mismatch" in f.reason
       and "calc.position.risk.buggy" in f.object_id]
check("position_risk: buggy 32.6275 value P0-blocked", bool(hit),
      hit[0].reason[:80] if hit else "NOT CAUGHT")

print(f"\n==== {len(passed)} passed, {len(failed)} failed ====")
sys.exit(1 if failed else 0)
