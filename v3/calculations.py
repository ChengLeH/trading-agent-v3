#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 calculations.py — deterministic calculation kernel.
Pure functions only: no I/O, no LLM, no network. Spec §11: keep it clean,
low-dependency, unit-testable. Ported from V2 fin_kernel.py (stripped of
comment bloat) + V3 additions: CAGR, adjusted close-to-close returns."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple


class FinancialDefenseError(ValueError):
    """Raised when book equity is passed where market cap is required."""


class FinancialUnitMismatchError(ValueError):
    """Raised on incompatible unit/scale combinations."""


class BasisMismatchError(ValueError):
    """Raised when mixing accounting bases (pre/post-tax, GAAP/non-GAAP...)."""


# --------------------------------------------------------------------------
# Enterprise value (from V2 fin_kernel)
# --------------------------------------------------------------------------
_FORBIDDEN_EV_KEYS = {
    "book_equity", "stockholders_equity", "shareholders_equity",
    "book_value", "equity", "equity_base", "total_equity",
}


def calc_enterprise_value(market_cap: float, total_debt: float,
                          cash_and_equivalents: float,
                          short_term_investments: float = 0.0,
                          **kw: Any) -> float:
    for key in _FORBIDDEN_EV_KEYS:
        if key in kw:
            raise FinancialDefenseError(
                f"book equity '{key}' must not stand in for market cap in EV")
    if market_cap is None or not isinstance(market_cap, (int, float)):
        raise TypeError("market_cap must be numeric")
    if market_cap <= 0:
        raise FinancialDefenseError(f"market_cap must be positive: {market_cap}")
    if total_debt < 0:
        raise FinancialDefenseError(f"total_debt must be >= 0: {total_debt}")
    net_debt = float(total_debt) - (float(cash_and_equivalents)
                                    + float(short_term_investments))
    return round(float(market_cap) + net_debt, 4)


# --------------------------------------------------------------------------
# CAGR — first-class calculation type (user decision 2026-10-15)
# --------------------------------------------------------------------------
def calc_cagr(start_value: float, end_value: float,
              years_elapsed: float) -> float:
    """(end/start)^(1/n) - 1. n = actual years elapsed, NOT observation count.
    Caller must also verify period/basis/unit/metric identity separately."""
    if start_value is None or end_value is None or years_elapsed is None:
        raise TypeError("cagr inputs must not be None")
    if start_value <= 0 or end_value <= 0:
        raise ValueError(f"cagr needs positive values: {start_value} -> {end_value}")
    if years_elapsed <= 0:
        raise ValueError(f"years_elapsed must be positive: {years_elapsed}")
    return round((end_value / start_value) ** (1.0 / years_elapsed) - 1.0, 6)


# --------------------------------------------------------------------------
# Returns — user decision 2026-10-15: weekly/monthly use ADJUSTED
# close-to-close. open-to-close is only ever `intraday_return`.
# --------------------------------------------------------------------------
def close_to_close_return(prev_close: float, close: float) -> float:
    if prev_close is None or close is None:
        raise TypeError("closes must not be None")
    if prev_close == 0:
        raise ValueError("prev_close must not be zero")
    return round((close - prev_close) / prev_close, 6)


def intraday_return(open_: float, close: float) -> float:
    """Explicitly NOT 'return'. Never mix with close-to-close series."""
    if open_ is None or close is None:
        raise TypeError("open/close must not be None")
    if open_ == 0:
        raise ValueError("open must not be zero")
    return round((close - open_) / open_, 6)


def aggregate_bars_close_to_close(bars: List[Dict[str, Any]],
                                  group_key: str) -> List[Dict[str, Any]]:
    """Aggregate daily bars to weekly/monthly. change_pct is ALWAYS
    adjusted close-to-close vs the previous period's close."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for b in bars:
        groups.setdefault(b[group_key], []).append(b)
    keys = sorted(groups)
    out = []
    prev_close: Optional[float] = None
    for k in keys:
        gb = groups[k]
        o, c = gb[0]["open"], gb[-1]["close"]
        h = max(b["high"] for b in gb)
        l = min(b["low"] for b in gb)
        v = sum(b["volume"] for b in gb)
        chg = close_to_close_return(prev_close, c) if prev_close else 0.0
        out.append({"period": k, "open": o, "high": h, "low": l, "close": c,
                    "volume": v, "change_pct": round(chg * 100, 2)})
        prev_close = c
    return out


# --------------------------------------------------------------------------
# Market technicals (from V2 summarize_ohlcv)
# --------------------------------------------------------------------------
def sma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period or period <= 0:
        return None
    return round(sum(closes[-period:]) / period, 2)


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-i] - closes[-i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = (sum(gains) / period) / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def atr(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, period + 1):
        h, l, pc = bars[-i]["high"], bars[-i]["low"], bars[-i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs) / period, 2)


# --------------------------------------------------------------------------
# Per-share, margins, weighted cost, R:R (from V2 fin_kernel)
# --------------------------------------------------------------------------
def calc_per_share(total: float, shares: float,
                   total_unit: str, shares_unit: str) -> float:
    tu, su = total_unit.upper(), shares_unit.upper()
    if tu == "USD" and su == "SHARES":
        ps = total / shares
    elif tu in ("USD_MILLIONS", "USD_BILLIONS") and su == "SHARES_MILLIONS":
        scale = 1.0 if tu == "USD_MILLIONS" else 1000.0
        ps = (total * scale) / shares
    else:
        raise FinancialUnitMismatchError(
            f"unsupported unit combo: {total_unit}/{shares_unit}")
    return round(ps, 2)


def calc_margin(numerator: float, denominator: float,
                num_basis: str = "unknown", den_basis: str = "unknown") -> float:
    """Margins require matching basis; mixing pre/post-tax raises."""
    if num_basis != "unknown" and den_basis != "unknown" \
            and num_basis != den_basis:
        raise BasisMismatchError(
            f"margin basis mismatch: {num_basis} vs {den_basis}")
    if denominator == 0:
        raise ValueError("margin denominator must not be zero")
    return round(numerator / denominator, 6)


def parse_price_value(spec: Any) -> float:
    if isinstance(spec, (int, float)):
        return float(spec)
    if isinstance(spec, (list, tuple)):
        if len(spec) != 2:
            raise ValueError(f"price range needs 2 values: {spec}")
        return round((float(spec[0]) + float(spec[1])) / 2.0, 4)
    if isinstance(spec, dict):
        if "mid" in spec:
            return float(spec["mid"])
        lo = next((spec[k] for k in ("min", "low", "start") if k in spec), None)
        hi = next((spec[k] for k in ("max", "high", "end") if k in spec), None)
        if lo is not None and hi is not None:
            return round((float(lo) + float(hi)) / 2.0, 4)
        raise ValueError(f"price dict needs low/high keys: {spec}")
    if isinstance(spec, str):
        s = spec.replace("$", "").replace(",", "").strip()
        m = re.match(r"^([\d.]+)\s*[-~–—]\s*([\d.]+)$", s)
        if m:
            return round((float(m.group(1)) + float(m.group(2))) / 2.0, 4)
        return float(s)
    raise TypeError(f"unparseable price spec: {type(spec)}")


def calc_weighted_entry_cost(tranches: List[Dict[str, Any]]) -> float:
    if not tranches:
        raise ValueError("tranches must be non-empty")
    tw, ws = 0.0, 0.0
    for i, t in enumerate(tranches):
        price = parse_price_value(t.get("price", t.get("range", t.get("entry"))))
        w = float(t.get("weight", t.get("pct", t.get("portion", t.get("shares")))))
        if w <= 0:
            raise ValueError(f"tranche {i}: weight must be > 0")
        tw += w
        ws += price * w
    return round(ws / tw, 2)


def calc_risk_reward_ratio(entry_cost: float, stop_loss: float,
                           target_price: float,
                           direction: str = "BUY") -> float:
    d = direction.strip().upper()
    # CONDITIONAL_* 只是觸發方式不同，方向本身仍是多/空，按對應方向計算
    if d == "CONDITIONAL_LONG":
        d = "LONG"
    elif d == "CONDITIONAL_SHORT":
        d = "SHORT"
    if d in ("BUY", "LONG"):
        risk = float(entry_cost) - float(stop_loss)
        reward = float(target_price) - float(entry_cost)
        if risk <= 0:
            raise ValueError(f"invalid risk (entry must be > stop): {risk}")
        if reward <= 0:
            raise ValueError(f"invalid reward (target must be > entry): {reward}")
    elif d in ("SELL", "SHORT"):
        risk = float(stop_loss) - float(entry_cost)
        reward = float(entry_cost) - float(target_price)
        if risk <= 0:
            raise ValueError(f"invalid risk (stop must be > entry): {risk}")
        if reward <= 0:
            raise ValueError(f"invalid reward (entry must be > target): {reward}")
    else:
        raise NotImplementedError(f"unknown direction: {direction}")
    return round(reward / risk, 4)


def calc_position_risk_pct(entry: float, stop: float,
                           position_pct: float,
                           direction: str = "BUY") -> float:
    """Account loss if stopped, in percent of account.
    position_pct is in percent units (10.0 = 10% position)."""
    if entry <= 0:
        raise ValueError("entry must be positive")
    d = direction.strip().upper()
    # CONDITIONAL_* 只是觸發方式不同，方向本身仍是多/空，按對應方向計算
    if d == "CONDITIONAL_LONG":
        d = "LONG"
    elif d == "CONDITIONAL_SHORT":
        d = "SHORT"
    if d in ("BUY", "LONG"):
        loss_per_share = entry - stop
        if loss_per_share <= 0:
            raise ValueError("stop must be below entry for LONG")
    elif d in ("SELL", "SHORT"):
        loss_per_share = stop - entry
        if loss_per_share <= 0:
            raise ValueError("stop must be above entry for SHORT")
    else:
        raise NotImplementedError(f"unknown direction: {direction}")
    return round(loss_per_share / entry * position_pct, 4)


def avg_volume(volumes: List[float], period: int) -> Optional[float]:
    """Simple average of the last `period` volume bars."""
    if len(volumes) < period or period <= 0:
        return None
    return round(sum(volumes[-period:]) / period, 2)


def calc_volume_ratio(volume: float, avg_volume_value: float) -> float:
    """Latest volume vs its moving average, as a ratio (1.0 = average)."""
    if avg_volume_value <= 0:
        raise ValueError("avg volume must be positive")
    return round(float(volume) / float(avg_volume_value), 4)


def calc_fcf(operating_cash_flow: float, capex: float) -> float:
    """Free cash flow = OCF + capex (capex is negative cash outflow).

    Bug 6 fix (2026-10-15): normalize CapEx sign. Data sources are
    inconsistent — some report CapEx as negative (outflow), others as
    positive (absolute value). We treat positive input as absolute value
    and negate it, so FCF = OCF - |CapEx| always holds.
    """
    capex = float(capex)
    if capex > 0:
        capex = -capex  # normalize: CapEx is cash outflow
    return round(float(operating_cash_flow) + capex, 2)


def calc_growth(current: float, prior: float) -> float:
    """Simple period-over-period growth rate, as a ratio."""
    if prior == 0:
        raise ValueError("prior must be non-zero for growth")
    return round((float(current) - float(prior)) / abs(float(prior)), 6)


def calc_distance_pct(value: float, reference: float) -> float:
    """Distance of value below reference, in percent (e.g. dist from 52w high)."""
    if reference == 0:
        raise ValueError("reference must be non-zero")
    return round((float(reference) - float(value)) / abs(float(reference)) * 100, 2)
