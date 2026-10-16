"""v3/builders.py — production FACT/CALC builders.

These turn SourceRecords into FactStore/CalcStore entries automatically.
No hand-written fact_store.add() in the production path: the orchestrator
calls build_market_facts() / build_fundamental_facts() / build_evidence_packets()
during ingest, and register_trade_plan() after the trader submits.

Missing data is recorded in `limitations`, never invented.
"""
from __future__ import annotations
from typing import Any, Dict, List, Tuple

from .facts import Fact
from . import calculations as K


def build_market_facts(bars: List[Dict[str, Any]],
                       source_id: str, source_hash: str,
                       as_of: str = None,
                       ) -> Tuple[List[Fact], List[Dict], List[str]]:
    """Auto-build market FACTs + CALCs from OHLCV bars.

    Returns (facts, calc_specs, limitations). calc_specs are dicts ready for
    CalcStore.add(cid, value, calc_type, input_refs, unit, meta).

    Partial bar protection (2026-10-15): if the last bar's date == as_of
    (today) and US market hasn't closed yet, exclude it — it's a partial
    intraday bar, not a settled close. Use the previous bar instead.
    """
    facts: List[Fact] = []
    calcs: List[Dict] = []
    limitations: List[str] = []
    if not bars:
        return facts, calcs, ["no bars in market source"]

    # --- Partial bar detection ---
    # If as_of is provided and the last bar is dated as_of, check if
    # market is still open (before 16:00 ET on the as_of date itself).
    # BOTH conditions required: last bar date == as_of AND today (ET) == as_of.
    # This prevents deleting yesterday's completed bar when re-running
    # yesterday's report the next morning.
    # Uses zoneinfo for correct DST handling (EDT vs EST).
    if as_of and len(bars) > 1:
        from datetime import datetime, time
        from zoneinfo import ZoneInfo
        try:
            last_date = str(bars[-1].get("date", ""))[:10]
            now_et = datetime.now(ZoneInfo("America/New_York"))
            today_et = now_et.date().isoformat()
            if (last_date == as_of
                    and today_et == as_of
                    and now_et.time() < time(16, 0)):
                dropped = bars.pop()
                limitations.append(
                    f"partial bar removed: {dropped.get('date')} "
                    f"(intraday, market not closed)"
                )
        except Exception:
            pass  # Don't break on date parsing errors

    closes = [b["close"] for b in bars]
    vols = [b.get("volume") for b in bars]
    last = bars[-1]
    period = str(last.get("date", ""))[:10]

    def F(fid, val, unit, concept, p=None, ptr=""):
        eff = p or period
        facts.append(Fact(fid, val, unit, eff, concept=concept,
                          source_id=source_id, source_pointer=ptr,
                          source_hash=source_hash,
                          effective_date=eff))

    def C(cid, val, ctype, refs, unit, meta=None):
        calcs.append({"cid": cid, "value": val, "calc_type": ctype,
                      "input_refs": refs, "unit": unit, "meta": meta or {}})

    # latest close + volume
    F("fact.Close.latest", round(closes[-1], 2), "USD", "Close",
      ptr="bars[-1].close")
    if last.get("volume") is not None:
        F("fact.Volume.latest", int(last["volume"]), "SHARES", "Volume",
          ptr="bars[-1].volume")
    else:
        limitations.append("volume missing on latest bar")

    # daily close-to-close return (latest day) — stored as ratio, kernel authority
    if len(closes) >= 2:
        r = K.close_to_close_return(closes[-2], closes[-1])
        C("calc.ReturnD.latest", round(r, 6), "return_cc",
          ["fact.Close.prev", "fact.Close.latest"], "RATIO", {})
        F("fact.Close.prev", round(closes[-2], 2), "USD", "Close",
          p=str(bars[-2].get("date", ""))[:10], ptr="bars[-2].close")

    # weekly / monthly OHLCV series + returns (12 periods each, V2 parity)
    # Renderer reads these FACTs directly; no recalculation in reporting.py.
    def _period_end_date(bars_group: List[Dict[str, Any]]) -> str:
        """Last bar date in a group, YYYY-MM-DD."""
        dates = [str(b.get("date", ""))[:10] for b in bars_group if b.get("date")]
        return max(dates) if dates else period

    # Build group -> bars map for effective_date lookup
    _groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {"week": {}, "month": {}}
    for b in bars:
        for gk in ("week", "month"):
            gv = b.get(gk)
            if gv:
                _groups[gk].setdefault(str(gv), []).append(b)

    for a in K.aggregate_bars_close_to_close(bars, "week")[-12:]:
        wp = a["period"]
        wbars = _groups["week"].get(wp, [])
        wend = _period_end_date(wbars)
        # OHLCV FACTs
        for field, concept, unit in (("open", "Open", "USD"),
                                      ("high", "High", "USD"),
                                      ("low", "Low", "USD"),
                                      ("close", "Close", "USD"),
                                      ("volume", "Volume", "SHARES")):
            val = a[field]
            if field == "volume":
                val = int(val)
            elif field in ("open", "high", "low", "close"):
                val = round(val, 2)
            facts.append(Fact(
                f"fact.OHLCV.W.{wp}.{field}", val, unit, wp,
                concept=f"OHLCV_W_{concept}",
                source_id=source_id,
                source_pointer=f"agg weekly {wp}.{field}",
                source_hash=source_hash,
                effective_date=wend))
        # Return FACT (expanded from 4 to 12)
        facts.append(Fact(f"fact.ReturnW.{wp}", a["change_pct"], "PCT",
                          wp, concept="ReturnW",
                          source_id=source_id,
                          source_pointer="adj cc weekly",
                          source_hash=source_hash,
                          effective_date=wend))

    for a in K.aggregate_bars_close_to_close(bars, "month")[-12:]:
        mp = a["period"]
        mbars = _groups["month"].get(mp, [])
        mend = _period_end_date(mbars)
        # OHLCV FACTs
        for field, concept, unit in (("open", "Open", "USD"),
                                      ("high", "High", "USD"),
                                      ("low", "Low", "USD"),
                                      ("close", "Close", "USD"),
                                      ("volume", "Volume", "SHARES")):
            val = a[field]
            if field == "volume":
                val = int(val)
            elif field in ("open", "high", "low", "close"):
                val = round(val, 2)
            facts.append(Fact(
                f"fact.OHLCV.M.{mp}.{field}", val, unit, mp,
                concept=f"OHLCV_M_{concept}",
                source_id=source_id,
                source_pointer=f"agg monthly {mp}.{field}",
                source_hash=source_hash,
                effective_date=mend))
        # Return FACT (expanded from 3 to 12)
        facts.append(Fact(f"fact.ReturnM.{mp}", a["change_pct"], "PCT",
                          mp, concept="ReturnM",
                          source_id=source_id,
                          source_pointer="adj cc monthly",
                          source_hash=source_hash,
                          effective_date=mend))

    # Recent 10 trading days OHLCV
    for b in bars[-10:]:
        d = str(b.get("date", ""))[:10]
        if not d:
            continue
        for field, concept, unit in (("open", "Open", "USD"),
                                      ("high", "High", "USD"),
                                      ("low", "Low", "USD"),
                                      ("close", "Close", "USD"),
                                      ("volume", "Volume", "SHARES")):
            v = b.get(field)
            if v is None:
                continue
            if field == "volume":
                v = int(v)
            else:
                v = round(float(v), 2)
            facts.append(Fact(
                f"fact.OHLCV.D.{d}.{field}", v, unit, d,
                concept=f"OHLCV_D_{concept}",
                source_id=source_id,
                source_pointer=f"bars[{d}].{field}",
                source_hash=source_hash,
                effective_date=d))

    # volume averages + ratio
    if all(v is not None for v in vols):
        for n in (20, 60):
            av = K.avg_volume(vols, n)
            if av:
                F(f"fact.AvgVolume{n}D.latest", av, "SHARES",
                  f"AvgVolume{n}D", ptr=f"avg(volume[-{n}:])")
        av20 = K.avg_volume(vols, 20)
        if av20 and last.get("volume"):
            vr = K.calc_volume_ratio(last["volume"], av20)
            C("calc.VolumeRatio.latest", vr, "volume_ratio",
              ["fact.Volume.latest", "fact.AvgVolume20D.latest"],
              "RATIO", {})
    else:
        limitations.append("volume series incomplete; avg volume skipped")

    # trend / momentum / volatility
    for per in (20, 50, 200):
        v = K.sma(closes, per)
        if v:
            F(f"fact.SMA{per}.latest", v, "USD", f"SMA{per}",
              ptr=f"sma({per})")
        else:
            limitations.append(f"SMA{per}: insufficient bars")
    rsi_v = K.rsi(closes)
    if rsi_v is not None:
        F("fact.RSI14.latest", rsi_v, "INDEX", "RSI14", ptr="rsi(14)")
    atr_v = K.atr(bars)
    if atr_v is not None:
        F("fact.ATR14.latest", atr_v, "USD", "ATR14", ptr="atr(14)")

    # 52-week high / low + distances
    # FIX 2026-10-15 DELL run: 高低點必須用 high/low 欄位，不可用收盤價。
    # 舊寫法 max(close) 得出 588.40（2026-09-17 收盤），正確值為 max(high)=595.51（2026-09-18）。
    win = bars[-252:] if len(bars) >= 252 else bars
    hi, lo = round(max(b["high"] for b in win), 2), round(min(b["low"] for b in win), 2)
    F("fact.High52w.latest", hi, "USD", "High52w", ptr="max(high[-252:])")
    F("fact.Low52w.latest", lo, "USD", "Low52w", ptr="min(low[-252:])")
    C("calc.dist.52whi", K.calc_distance_pct(closes[-1], hi), "derived_pct",
      ["fact.High52w.latest", "fact.Close.latest"], "PCT", {})
    C("calc.dist.52wlo", K.calc_distance_pct(lo, closes[-1]), "derived_pct",
      ["fact.Close.latest", "fact.Low52w.latest"], "PCT", {})
    return facts, calcs, limitations


# fundamentals: statement -> (field, concept, unit)
_INCOME_MAP = [
    ("total_revenue", "Revenue", "USD"),
    ("total_gross_profit", "GrossProfit", "USD"),
    ("total_operating_income", "OperatingIncome", "USD"),
    ("net_income", "NetIncome", "USD"),
    ("basic_eps", "BasicEPS", "USD_PER_SHARE"),
    ("diluted_eps", "DilutedEPS", "USD_PER_SHARE"),
]
_BALANCE_MAP = [
    ("cash_and_equivalents", "Cash", "USD"),
    ("total_assets", "Assets", "USD"),
    ("total_liabilities", "Liabilities", "USD"),
    ("total_equity", "Equity", "USD"),
    ("net_inventory", "Inventory", "USD"),
    ("long_term_debt", "LongTermDebt", "USD"),
    ("short_term_debt", "ShortTermDebt", "USD"),
]
_CASHFLOW_MAP = [
    ("net_cash_from_operating_activities", "OperatingCashFlow", "USD"),
    ("purchase_of_plant_property_and_equipment", "Capex", "USD"),
]

_MISSING_CONCEPTS = ["EBITDA", "TotalDebt(reported)", "SharesOutstanding",
                     "SegmentRevenue", "SegmentProfit"]


def build_fundamental_facts(income: List[Dict], balance: List[Dict],
                            cash_flow: List[Dict],
                            source_id: str, source_hash: str
                            ) -> Tuple[List[Fact], List[Dict], List[str]]:
    """Auto-build fundamental FACTs + CALCs from SEC/OpenBB statements."""
    facts: List[Fact] = []
    calcs: List[Dict] = []
    limitations: List[str] = []

    def add(stmt_rows, field, concept, unit):
        for i, row in enumerate(stmt_rows):
            v = row.get(field)
            if v is None:
                continue
            fy = row.get("fiscal_year")
            facts.append(Fact(
                f"fact.{concept}.FY{fy}", float(v), unit, f"FY{fy}",
                fiscal_year=fy, fiscal_period=row.get("fiscal_period", "FY"),
                concept=concept, basis="gaap", scope="consolidated",
                source_id=source_id,
                source_pointer=f"{field}[{i}]",
                source_hash=source_hash))

    for field, concept, unit in _INCOME_MAP:
        add(income, field, concept, unit)
    for field, concept, unit in _BALANCE_MAP:
        add(balance, field, concept, unit)
    for field, concept, unit in _CASHFLOW_MAP:
        add(cash_flow, field, concept, unit)

    # derived: total debt = short + long (reported total_debt absent)
    fys = sorted({r.get("fiscal_year") for r in balance if r.get("fiscal_year")})
    by_fy = {r.get("fiscal_year"): r for r in balance}
    for fy in fys:
        row = by_fy[fy]
        std = row.get("short_term_debt") or 0
        cpltd = row.get("current_portion_of_long_term_debt") or 0
        ltd = row.get("long_term_debt") or 0
        if std or ltd:
            facts.append(Fact(
                f"fact.TotalDebt.FY{fy}", float(std + cpltd + ltd), "USD",
                f"FY{fy}", fiscal_year=fy, fiscal_period="FY",
                concept="TotalDebt", basis="gaap", scope="consolidated",
                source_id=source_id, source_pointer="derived: short+cpltd+long",
                source_hash=source_hash))
    # derived: FCF = OCF + capex ; margins ; revenue growth ; CAGR
    ocf = {r.get("fiscal_year"): r.get("net_cash_from_operating_activities")
           for r in cash_flow}
    capex = {r.get("fiscal_year"): r.get("purchase_of_plant_property_and_equipment")
             for r in cash_flow}
    for fy in fys:
        if ocf.get(fy) is not None and capex.get(fy) is not None:
            fcf = K.calc_fcf(ocf[fy], capex[fy])
            facts.append(Fact(
                f"fact.FreeCashFlow.FY{fy}", fcf, "USD", f"FY{fy}",
                fiscal_year=fy, fiscal_period="FY", concept="FreeCashFlow",
                basis="gaap", scope="consolidated", source_id=source_id,
                source_pointer="derived: OCF+capex", source_hash=source_hash))
            calcs.append({"cid": f"calc.fcf.FY{fy}", "value": fcf,
                          "calc_type": "fcf",
                          "input_refs": [f"fact.OperatingCashFlow.FY{fy}",
                                         f"fact.Capex.FY{fy}"],
                          "unit": "USD", "meta": {}})

    rev = {r.get("fiscal_year"): r.get("total_revenue") for r in income}
    gp = {r.get("fiscal_year"): r.get("total_gross_profit") for r in income}
    for fy in fys:
        if gp.get(fy) is not None and rev.get(fy):
            gm = K.calc_margin(gp[fy], rev[fy], "gaap", "gaap")
            calcs.append({"cid": f"calc.grossmargin.FY{fy}", "value": gm,
                          "calc_type": "margin",
                          "input_refs": [f"fact.GrossProfit.FY{fy}",
                                         f"fact.Revenue.FY{fy}"],
                          "unit": "RATIO",
                          "meta": {"num_basis": "gaap", "den_basis": "gaap"}})
    for a, b in zip(fys, fys[1:]):
        if rev.get(a) and rev.get(b):
            g = K.calc_growth(rev[b], rev[a])
            calcs.append({"cid": f"calc.rev_growth.FY{b}", "value": g,
                          "calc_type": "growth",
                          "input_refs": [f"fact.Revenue.FY{b}",
                                         f"fact.Revenue.FY{a}"],
                          "unit": "RATIO", "meta": {}})
    if len(fys) >= 2 and rev.get(fys[0]) and rev.get(fys[-1]):
        years = fys[-1] - fys[0]
        cagr = K.calc_cagr(rev[fys[0]], rev[fys[-1]], years)
        calcs.append({"cid": "calc.cagr.rev", "value": cagr,
                      "calc_type": "cagr",
                      "input_refs": [f"fact.Revenue.FY{fys[0]}",
                                     f"fact.Revenue.FY{fys[-1]}"],
                      "unit": "RATIO", "meta": {"years": float(years)}})

    for concept in _MISSING_CONCEPTS:
        limitations.append(f"{concept}: not in source; no fact created")
    return facts, calcs, limitations


def register_trade_plan(trade: Dict[str, Any], fact_store, calc_store,
                        source_id: str = "trader") -> List[str]:
    """Production path: trader structured plan -> kernel -> CalcStore.

    The trader submits entry/stop/target/position_pct (levels are the trader's
    discretion parameters). This function derives risk_reward / position_risk
    via the kernel and registers them as CALCs, plus trade FACTs.
    Returns the list of created calc ids.
    """
    # WAIT = 不建倉，沒有交易可註冊，直接返回空清單
    direction = str(trade.get("direction", "LONG")).strip().upper()
    if direction == "WAIT":
        return []
    created: List[str] = []
    entry = float(trade["entry"])
    stop = float(trade["stop"])
    target = float(trade["target"])
    pos = float(trade["position_pct"])
    # Direction-aware: SHORT trades have stop > entry (inverse of LONG).
    # trigger_price is optional; if present, register it for transparency.
    # R/R is always computed from entry (actual fill), never trigger.
    trigger = trade.get("trigger_price")
    if trigger is not None:
        trigger = float(trigger)

    for fid, val, concept in (("fact.Trade.Entry", entry, "TradeEntry"),
                              ("fact.Trade.Stop", stop, "TradeStop"),
                              ("fact.Trade.Target", target, "TradeTarget"),
                              ("fact.Trade.PositionPct", pos, "TradePositionPct")):
        fact_store.add(Fact(fid, val, "USD" if "Pct" not in fid else "PERCENT",
                            "TRADE", concept=concept, source_id=source_id,
                            source_pointer="trade plan",
                            source_hash=fact_store.snapshot_hash()))
    if trigger is not None:
        fact_store.add(Fact("fact.Trade.Trigger", trigger, "USD",
                            "TRADE", concept="TradeTrigger", source_id=source_id,
                            source_pointer="trade plan",
                            source_hash=fact_store.snapshot_hash()))

    rr = K.calc_risk_reward_ratio(entry, stop, target, direction=direction)
    calc_store.add("calc.trade.risk_reward", rr, "risk_reward",
                   ["fact.Trade.Entry", "fact.Trade.Stop", "fact.Trade.Target"],
                   "RATIO", {"direction": direction})
    created.append("calc.trade.risk_reward")

    pr = K.calc_position_risk_pct(entry, stop, pos, direction=direction)
    calc_store.add("calc.trade.position_risk", pr, "position_risk",
                   ["fact.Trade.Entry", "fact.Trade.Stop",
                    "fact.Trade.PositionPct"],
                   "PERCENT", {"direction": direction})
    created.append("calc.trade.position_risk")
    return created
