#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the 4 generic invariants (2026-10-15, abstracted from CBRS errors).
Run: PYTHONPATH=. .venv/bin/python -m v3.test_invariants
"""
from v3.facts import Fact, FactStore, CalcStore
from v3.evidence import EvidencePacket, EvidenceStore
from v3.schemas import ReleaseEvent
from v3 import validation as V

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail else ""))


def _fact(fid, value, concept, period="2026-10-14", **kw):
    d = dict(fact_id=fid, value=value, unit=kw.pop("unit", "USD"),
             period=period, concept=concept, source_id="t",
             source_pointer="p", source_hash="h")
    d.update(kw)
    return Fact(**d)


# ============ 1. as_of_consistency ============
fs = FactStore()
fs.add(_fact("fact.Close.latest", 150.0, "Close", effective_date="2026-10-14"))
fs.add(_fact("fact.SMA20.latest", 148.0, "SMA20", effective_date="2026-10-14"))
fs.add(_fact("fact.RSI14.latest", 55.0, "RSI14", unit="INDEX",
             effective_date="2026-10-14"))
f = V.check_as_of_consistency(fs, None, "2026-10-15", 4)
check("asof: fresh latest FACTs pass",
      all(x.status == "PASS" for x in f), f[0].reason[:50])

fs2 = FactStore()
fs2.add(_fact("fact.Close.latest", 150.0, "Close", effective_date="2026-10-01"))
f = V.check_as_of_consistency(fs2, None, "2026-10-15", 4)
check("asof: stale FACT blocks",
      any(x.blocks for x in f), [x.reason[:60] for x in f if x.blocks])

fs3 = FactStore()
fs3.add(_fact("fact.Close.latest", 150.0, "Close"))  # no effective_date
f = V.check_as_of_consistency(fs3, None, "2026-10-15", 4)
check("asof: missing effective_date blocks",
      any(x.blocks and "effective_date" in x.reason for x in f))

fs4 = FactStore()
fs4.add(_fact("fact.Close.latest", 150.0, "Close", effective_date="2026-10-14"))
fs4.add(_fact("fact.SMA20.latest", 148.0, "SMA20", effective_date="2026-10-13"))
f = V.check_as_of_consistency(fs4, None, "2026-10-15", 4)
check("asof: market group date mismatch blocks",
      any(x.blocks and "observation date" in x.reason for x in f))

# CALC consuming stale input
fs5 = FactStore()
fs5.add(_fact("fact.Shares.latest", 228.0, "SharesOutstanding", unit="SHARES",
              effective_date="2026-06-30"))
cs5 = CalcStore()
cs5.add("calc.mktcap", 34000, "per_share",
        ["fact.Close.latest", "fact.Shares.latest"], "USD", {})
fs5.add(_fact("fact.Close.latest", 150.0, "Close", effective_date="2026-10-14"))
f = V.check_as_of_consistency(fs5, cs5, "2026-10-15", 4)
check("asof: CALC with stale share count blocks",
      any(x.blocks and "stale input" in x.reason for x in f),
      "CBRS stale-shares pattern")

# ============ 2. accounting_scope_consistency ============
fs6 = FactStore()
fs6.add(_fact("fact.Cash.total", 8.61e9, "Cash", period="2026-H1",
              basis="gaap", scope="consolidated"))
cs6 = CalcStore()
cs6.add("calc.available_liquidity", 8.61e9, "derived",
        ["fact.Cash.total"], "USD",
        {"cash_scope": "total"})  # WRONG: total presented as available
f = V.check_accounting_basis_consistency(fs6, cs6)
check("scope: available_* with total cash blocks",
      any(x.blocks and "available_*" in x.reason for x in f),
      "CBRS $8.61B liquidity pattern")

cs7 = CalcStore()
cs7.add("calc.cash_runway", 7.2, "derived", ["fact.Cash.total"], "YEARS", {})
f = V.check_accounting_basis_consistency(fs6, cs7)
check("scope: cash CALC without cash_scope blocks",
      any(x.blocks and "cash_scope" in x.reason for x in f))

cs8 = CalcStore()
cs8.add("calc.available_liquidity", 7.92e9, "derived",
        ["fact.Cash.total"], "USD", {"cash_scope": "unrestricted"})
f = V.check_accounting_basis_consistency(fs6, cs8)
check("scope: declared unrestricted passes",
      not any(x.blocks for x in f))

# revenue_scope
fs9 = FactStore()
fs9.add(_fact("fact.Revenue.2025", 18.7e9, "Revenue", period="FY2025",
              basis="gaap", scope="consolidated"))
fs9.add(_fact("fact.Revenue.core.2025", 20.0e9, "Revenue", period="FY2025",
              basis="non_gaap", scope="consolidated"))
cs9 = CalcStore()
cs9.add("calc.ps_ratio", 40.0, "per_share",
        ["fact.MktCap.latest", "fact.Revenue.core.2025"], "RATIO", {})
fs9.add(_fact("fact.MktCap.latest", 800e9, "MarketCap",
              effective_date="2026-10-14"))
f = V.check_accounting_basis_consistency(fs9, cs9)
check("scope: non-GAAP revenue CALC needs revenue_scope",
      any(x.status == "FAIL" and "revenue_scope" in x.reason for x in f))

# ============ 4. event_timeline_consistency ============
events_ok = [
    ReleaseEvent("2026-08-01", "180d post-IPO", 27_700_000, False, "S-1").to_dict(),
    ReleaseEvent("2026-09-15", "Q2 earnings", 36_400_000, False, "S-1").to_dict(),
    ReleaseEvent("2026-10-01", "early release", 14_600_000, False, "8-K").to_dict(),
    {"date": "2027-01-01", "condition": "lockup end", "shares": 171_100_000,
     "is_aggregate": True, "source_ref": "S-1 p.112"},
]
f = V.check_event_timeline_consistency(events_ok, [])
check("timeline: individuals under cap pass",
      not any(x.blocks for x in f), f[0].reason[:60])

events_over = events_ok + [
    ReleaseEvent("2026-11-01", "extra", 200_000_000, False, "8-K").to_dict()]
f = V.check_event_timeline_consistency(events_over, [])
check("timeline: individuals exceeding cap block",
      any(x.blocks and "exceeding" in x.reason for x in f))

bad_claim = [{
    "claim_id": "c4", "claim_type": "derived_fact",
    "text": "A final batch of 171.1M shares unlocks on 2026-11-10.",
    "refs": []}]
f = V.check_event_timeline_consistency(events_ok, bad_claim)
check("timeline: aggregate-as-batch blocks (CBRS 171.1M pattern)",
      any(x.blocks and "aggregate cap" in x.reason for x in f))

good_claim = [{
    "claim_id": "c5", "claim_type": "derived_fact",
    "text": "Up to 171.1M shares may be released cumulatively during lockup.",
    "refs": []}]
f = V.check_event_timeline_consistency(events_ok, good_claim)
check("timeline: correctly-framed aggregate passes",
      not any(x.blocks for x in f))

print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
    raise SystemExit(1)
