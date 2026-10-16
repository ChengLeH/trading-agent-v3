#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""openbb_data.py — VM data layer (OpenBB 5.x, keyless).

Replaces V2's direct connections:
  - Yahoo chart direct  -> obb.yfinance.equity.price.historical
  - SEC EDGAR XBRL      -> obb.sec.income_statement / balance_sheet / cash_flow
  - Alpha Vantage       -> dropped; news goes through assistant tools

User decision 2026-10-15: weekly/monthly returns use ADJUSTED close-to-close,
so we fetch with adjustment='splits_and_dividends'.

All numbers come from this program. Analysts never hand-fill figures.
"""
import argparse
import json
import sys
from datetime import datetime, timezone


def fetch_ohlcv(symbol: str, start_date=None, end_date=None):
    from openbb import obb
    out = obb.yfinance.equity.price.historical(
        symbol, start_date=start_date, end_date=end_date,
        adjustment="splits_and_dividends",  # adjusted closes (user decision)
    )
    df = out.to_dataframe().reset_index()
    bars = []
    for _, r in df.iterrows():
        d = r["date"]
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        bars.append({
            "date": ds,
            "open": round(float(r["open"]), 2),
            "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2),
            "close": round(float(r["close"]), 2),  # adjusted
            "volume": int(r["volume"]),
        })
    # week / month keys for aggregation
    from datetime import datetime as dt
    for b in bars:
        d = dt.strptime(b["date"], "%Y-%m-%d")
        b["week"] = d.strftime("%Y-W%V")
        b["month"] = d.strftime("%Y-%m")
    return bars


def fetch_fundamentals(symbol: str, period: str = "annual", limit: int = 4):
    from openbb import obb
    data = {}
    calls = {
        "income": lambda: obb.sec.income_statement(
            symbol, period=period, limit=limit).to_dataframe(),
        "balance": lambda: obb.sec.balance_sheet(
            symbol, period=period, limit=limit).to_dataframe(),
        "cash_flow": lambda: obb.sec.cash_flow(
            symbol, period=period, limit=limit).to_dataframe(),
    }
    for key, fn in calls.items():
        try:
            df = fn().reset_index()
            data[key] = json.loads(df.to_json(orient="records", date_format="iso"))
        except Exception as e:  # noqa: BLE001
            data[key + "_error"] = str(e)[:200]
    return data


def main():
    ap = argparse.ArgumentParser(description="OpenBB 5.x keyless data layer")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--periods", type=int, default=4)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-fundamentals", action="store_true")
    args = ap.parse_args()

    payload = {
        "_metadata": {
            "ticker": args.ticker.upper(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "data_layer": "openbb 5.x keyless (yfinance adjusted + sec)",
            "adjustment": "splits_and_dividends",
        },
        "ohlcv": fetch_ohlcv(args.ticker, args.start, args.end),
    }
    if not args.no_fundamentals:
        payload["fundamentals"] = fetch_fundamentals(args.ticker,
                                                     limit=args.periods)
    text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out} ({len(text)} chars)", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
