#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/summarize_ohlcv.py
--------------------------
將 Yahoo Finance 2 年日線資料（~500 天 OHLCV）聚合預處理為週線與月線摘要，
並提取關鍵技術指標與 52 週高低點，產出結構化 Markdown/JSON 供模型分析。

使用方式:
    python3 scripts/summarize_ohlcv.py --ticker NVDA
    python3 scripts/summarize_ohlcv.py --file raw_chart.json
    python3 scripts/summarize_ohlcv.py --ticker AAPL --format json
"""

import sys
import os
import json
import math
import argparse
from datetime import datetime
import urllib.request
import urllib.error

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def fetch_yahoo_chart(ticker: str) -> dict:
    """從 Yahoo Finance Chart API 抓取 2 年日線數據 (query1 優先，query2 備援)"""
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y"
    ]
    last_err = None
    for url in urls:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8")
                return json.loads(content)
        except urllib.error.HTTPError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue
            
    sys.stderr.write(f"Error fetching Yahoo Finance for {ticker}: {last_err}\n")
    raise last_err

def parse_ohlcv(data: dict):
    """解析 Yahoo chart JSON，返回乾淨的日線列表 [{date, open, high, low, close, volume}]"""
    result = data.get("chart", {}).get("result", [])
    if not result:
        error = data.get("chart", {}).get("error", "Unknown error")
        raise ValueError(f"Yahoo Finance returned error or empty result: {error}")
    
    meta = result[0].get("meta", {})
    timestamps = result[0].get("timestamp", [])
    indicators = result[0].get("indicators", {})
    quote = indicators.get("quote", [{}])[0]
    
    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])
    
    bars = []
    for i, ts in enumerate(timestamps):
        o = opens[i] if i < len(opens) else None
        h = highs[i] if i < len(highs) else None
        l = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        v = volumes[i] if i < len(volumes) else None
        
        # 濾除缺值
        if None in (o, h, l, c) or math.isnan(c):
            continue
            
        dt = datetime.utcfromtimestamp(ts)
        bars.append({
            "timestamp": ts,
            "date": dt.strftime("%Y-%m-%d"),
            "year": dt.year,
            "month": dt.strftime("%Y-%m"),
            "week": dt.strftime("%Y-W%W"),
            "open": round(float(o), 2),
            "high": round(float(h), 2),
            "low": round(float(l), 2),
            "close": round(float(c), 2),
            "volume": int(v) if v is not None and not math.isnan(v) else 0
        })
    
    return meta, bars

def calculate_sma(closes, period):
    """計算簡單移動平均線 SMA"""
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)

def calculate_rsi(closes, period=14):
    """計算 14 日相對強弱指標 RSI"""
    if len(closes) <= period:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    recent_deltas = deltas[-period:]
    gains = [d for d in recent_deltas if d > 0]
    losses = [-d for d in recent_deltas if d < 0]
    
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)

def calculate_atr(bars, period=14):
    """計算 14 日真實波動幅度均值 ATR"""
    if len(bars) <= period:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]
        l = bars[i]["low"]
        prev_c = bars[i-1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    recent_trs = trs[-period:]
    return round(sum(recent_trs) / period, 2)

def aggregate_bars(bars, group_key):
    """將日線依據週 (week) 或月 (month) 聚合"""
    groups = {}
    for bar in bars:
        key = bar[group_key]
        if key not in groups:
            groups[key] = []
        groups[key].append(bar)
    
    aggregated = []
    for key, group_bars in groups.items():
        o = group_bars[0]["open"]
        c = group_bars[-1]["close"]
        h = max(b["high"] for b in group_bars)
        l = min(b["low"] for b in group_bars)
        v = sum(b["volume"] for b in group_bars)
        change_pct = round(((c - o) / o) * 100, 2) if o else 0.0
        aggregated.append({
            "period": key,
            "start_date": group_bars[0]["date"],
            "end_date": group_bars[-1]["date"],
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
            "change_pct": change_pct
        })
    return aggregated

def build_summary(meta: dict, bars: list) -> dict:
    """生成聚合統計資料與技術面數據"""
    if not bars:
        return {"error": "No valid bars available"}
        
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]
    
    latest = bars[-1]
    
    # 52 週（約 252 個交易日）高低點
    past_year_bars = bars[-252:] if len(bars) >= 252 else bars
    high_52w = max(b["high"] for b in past_year_bars)
    low_52w = min(b["low"] for b in past_year_bars)
    
    # 移動平均線
    sma_20 = calculate_sma(closes, 20)
    sma_50 = calculate_sma(closes, 50)
    sma_200 = calculate_sma(closes, 200)
    rsi_14 = calculate_rsi(closes, 14)
    atr_14 = calculate_atr(bars, 14)
    avg_vol_20 = int(sum(volumes[-20:]) / 20) if len(volumes) >= 20 else int(sum(volumes) / len(volumes))
    
    # 聚合週線與月線
    weekly_bars = aggregate_bars(bars, "week")
    monthly_bars = aggregate_bars(bars, "month")
    
    # 取最近 12 週與最近 12 個月
    recent_weekly = weekly_bars[-12:]
    recent_monthly = monthly_bars[-12:]
    recent_daily = bars[-10:]
    
    return {
        "ticker": meta.get("symbol", "UNKNOWN"),
        "currency": meta.get("currency", "USD"),
        "regular_market_price": meta.get("regularMarketPrice", latest["close"]),
        "latest_date": latest["date"],
        "total_days_analyzed": len(bars),
        "key_metrics": {
            "latest_close": latest["close"],
            "52_week_high": high_52w,
            "52_week_low": low_52w,
            "distance_from_52w_high_pct": round(((latest["close"] - high_52w) / high_52w) * 100, 2),
            "distance_from_52w_low_pct": round(((latest["close"] - low_52w) / low_52w) * 100, 2),
            "sma_20": sma_20,
            "sma_50": sma_50,
            "sma_200": sma_200,
            "rsi_14": rsi_14,
            "atr_14": atr_14,
            "avg_volume_20d": avg_vol_20
        },
        "recent_daily": recent_daily,
        "recent_weekly": recent_weekly,
        "recent_monthly": recent_monthly
    }

def format_markdown(summary: dict) -> str:
    """將摘要轉為模型友好的 Markdown 格式"""
    t = summary["ticker"]
    m = summary["key_metrics"]
    
    md = []
    md.append(f"# OHLCV 預處理摘要報告: {t}")
    md.append(f"**資料來源**: Yahoo Finance 2y Chart API | **分析涵蓋天數**: {summary['total_days_analyzed']} 天 | **最新日期**: {summary['latest_date']}")
    md.append("")
    md.append("## 核心技術面概覽")
    md.append(f"- **現價/最新收盤**: ${m['latest_close']}")
    md.append(f"- **52 週最高/最低**: ${m['52_week_high']} / ${m['52_week_low']} (距最高點 {m['distance_from_52w_high_pct']}%, 距最低點 +{m['distance_from_52w_low_pct']}%)")
    md.append(f"- **移動平均線**: SMA20 = ${m['sma_20']} | SMA50 = ${m['sma_50']} | SMA200 = ${m['sma_200']}")
    md.append(f"- **動能與波動**: RSI(14) = {m['rsi_14']} | ATR(14) = ${m['atr_14']} | 20日均量 = {m['avg_volume_20d']:,}")
    md.append("")
    
    md.append("## 最近 12 個月線趨勢 (Monthly Summary)")
    md.append("| 月份 | 起迄日 | 開盤 | 最高 | 最低 | 收盤 | 成交量 | 漲跌幅 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in summary["recent_monthly"]:
        md.append(f"| {r['period']} | {r['start_date']}~{r['end_date']} | ${r['open']} | ${r['high']} | ${r['low']} | ${r['close']} | {r['volume']:,} | {r['change_pct']:+}% |")
    md.append("")
    
    md.append("## 最近 12 週線趨勢 (Weekly Summary)")
    md.append("| 週別 | 起迄日 | 開盤 | 最高 | 最低 | 收盤 | 成交量 | 漲跌幅 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in summary["recent_weekly"]:
        md.append(f"| {r['period']} | {r['start_date']}~{r['end_date']} | ${r['open']} | ${r['high']} | ${r['low']} | ${r['close']} | {r['volume']:,} | {r['change_pct']:+}% |")
    md.append("")
    
    md.append("## 最近 10 個交易日走勢 (Daily Snapshot)")
    md.append("| 日期 | 開盤 | 最高 | 最低 | 收盤 | 成交量 |")
    md.append("|---|---|---|---|---|---|")
    for r in summary["recent_daily"]:
        md.append(f"| {r['date']} | ${r['open']} | ${r['high']} | ${r['low']} | ${r['close']} | {r['volume']:,} |")
    md.append("")
    
    return "\n".join(md)

def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance 500-day daily to weekly/monthly summarizer")
    parser.add_argument("--ticker", "-t", type=str, help="Stock ticker symbol (e.g. NVDA, AAPL)")
    parser.add_argument("--file", "-f", type=str, help="Path to raw Yahoo Finance JSON file")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format (markdown or json)")
    parser.add_argument("--output", "-o", type=str, help="Path to save output (default: stdout)")
    
    args = parser.parse_args()
    
    if not args.ticker and not args.file:
        parser.print_help()
        sys.exit(1)
        
    try:
        if args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        else:
            raw_data = fetch_yahoo_chart(args.ticker.strip().upper())
            
        meta, bars = parse_ohlcv(raw_data)
        summary = build_summary(meta, bars)
        
        if args.format == "json":
            out_str = json.dumps(summary, indent=2, ensure_ascii=False)
        else:
            out_str = format_markdown(summary)
            
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(out_str)
            print(f"Summary successfully written to {args.output}")
        else:
            print(out_str)
            
        sys.exit(0)
    except Exception as e:
        sys.stderr.write(f"Error in summarize_ohlcv: {str(e)}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
