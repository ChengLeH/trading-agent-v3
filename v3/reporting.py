"""v3/reporting.py — deterministic report renderer (V2 human-readable format).

Renders in V2 investment-bank style:
  - Chinese titles, Table of Contents
  - OHLCV tables (monthly / weekly / daily)
  - Each analyst section: 一句話立場 + 三個最強證據 + 兩個反轉條件 + 估值邏輯
  - Indicator summary tables, scenario tables

Claim IDs, confidence scores, fact/calc refs live in the sidecar ONLY,
not in the report body. The body reads like a human analyst wrote it.
Numbers are still deterministic: every value comes from FACT/CALC,
and the sidecar maps each section to its source objects.
"""
from __future__ import annotations
from typing import Any, Dict, List
import re


def _num(facts: Dict[str, Dict], fid: str) -> str:
    """Format a fact value for display (no ID tag)."""
    f = facts.get(fid)
    if not f:
        return "—"
    v = f["value"]
    unit = f.get("unit", "")
    if unit == "USD" and abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if unit == "USD" and abs(v) >= 1e6:
        return f"${v/1e6:.1f}M"
    if unit in ("PCT", "PERCENT"):
        return f"{v}%"
    if unit == "RATIO":
        return f"{v:.2%}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def _clean(text: str) -> str:
    """Strip [fact.xxx] / [calc.xxx] / [ev_xxx] inline markers for readability."""
    text = re.sub(r"\[(fact|calc)\.[\w.]+\]", "", text)
    text = re.sub(r"\[ev_[\w]+\]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _claims_by_type(claims: List[Dict], *types: str) -> List[Dict]:
    return [c for c in claims if c.get("claim_type") in types]


# Valid purpose values (2026-10-15): report role for _thesis_block.
# claim_type = validation semantics; purpose = presentation role.
# Unknown purpose -> renderer puts under "其他論述", never crashes.
PURPOSES = frozenset({
    "thesis", "evidence", "contradiction", "reversal",
    "interpretation", "forward", "valuation", "scenario",
    "verdict", "adjudication", "plan", "blueprint",
})


def _claims_by_purpose(claims: List[Dict], *purposes: str) -> List[Dict]:
    return [c for c in claims if c.get("purpose") in purposes]


# Subsection -> Chinese header mapping (presentation only).
# Agents set Claim.subsection from the documented vocabulary; renderer
# groups by it. Claims without subsection go under "其他論述".
_SUBSECTION_HEADERS: Dict[str, str] = {
    # market_analyst
    "trend": "趨勢結構",
    "momentum_volatility": "動能與波動",
    "support_resistance": "支撐壓力",
    # fundamentals_analyst
    "profitability_growth": "獲利與成長",
    "balance_sheet": "資產負債",
    "cash_flow_capex": "現金流與資本支出",
    "valuation": "估值",
    # news_analyst
    "timeline": "事件時間軸",
    "catalysts": "催化劑",
    "risks": "風險",
    # social_analyst
    "sentiment_overview": "情緒總覽",
    "reflexivity_risk": "反身性風險",
    # bull / bear
    "scenario": "情境分析",
    "counter_response": "對反方論點的回應",
    # research_manager
    "adjudication": "裁決",
    # risk_manager
    "aggressive": "激進觀點",
    "conservative": "保守觀點",
    "neutral": "中性觀點",
    "final_tradeoff": "最終取捨",
    # trader
    "blueprint": "交易藍圖",
}

# Minimal keyword fallback for old claims without subsection.
# Production path should use explicit subsection; this is backward-compat only.
_FALLBACK_KEYWORDS: List[tuple] = [
    ("trend", ["趨勢", "均線", "多頭排列", "空頭排列"]),
    ("momentum_volatility", ["RSI", "MACD", "動能", "波動", "ATR"]),
    ("support_resistance", ["支撐", "壓力", "阻力"]),
    ("profitability_growth", ["毛利", "營收成長", "獲利"]),
    ("balance_sheet", ["資產負債", "負債", "現金"]),
    ("cash_flow_capex", ["現金流", "資本支出", "CapEx", "FCF"]),
]


def _group_by_subsection(role_claims: List[Dict]) -> Dict[str, List[Dict]]:
    """Group claims by subsection field. Fallback to keywords only for
    claims without subsection (logged). No deduplication."""
    grouped: Dict[str, List[Dict]] = {}
    for c in role_claims:
        # Fix 2026-10-16: purpose="forward" 的 claim 是未來含義，不該被
        # subsection 的主題標題綁架（如「### 支撐壓力」下放未來含義）。
        # 這類 claim 統一歸到「未來含義」組。
        if c.get("purpose") == "forward":
            grouped.setdefault("__forward__", []).append(c)
            continue
        ss = c.get("subsection")
        if not ss:
            # Minimal keyword fallback (backward compat only).
            # Fix 2026-10-16: 最長關鍵字優先，避免「現金」先咬走
            # 「營運現金流」、害 cash_flow_capex 沒機會的碰撞。
            text = c.get("text", "")
            best_key, best_len = None, -1
            for key, kws in _FALLBACK_KEYWORDS:
                for kw in kws:
                    if kw in text and len(kw) > best_len:
                        best_key, best_len = key, len(kw)
            ss = best_key
            if ss:
                # Log fallback usage (visible in sidecar)
                c = dict(c)
                c["_fallback_subsection"] = True
        ss = ss or "其他論述"
        grouped.setdefault(ss, []).append(c)
    return grouped


def _render_subsections(role_claims: List[Dict]) -> List[str]:
    """Render claims grouped by subsection. No deduplication — same FACT
    may be interpreted differently by different roles (semantic redundancy
    is intentional and preserved)."""
    out: List[str] = []
    grouped = _group_by_subsection(role_claims)
    # Render in a stable order: known subsections first (in mapping order),
    # then "其他論述" last.
    ordered_keys = [k for k in _SUBSECTION_HEADERS if k in grouped]
    if "其他論述" in grouped:
        ordered_keys.append("其他論述")
    # Any remaining keys not in mapping (shouldn't happen, but be safe)
    for k in grouped:
        if k not in ordered_keys:
            ordered_keys.append(k)
    for ss in ordered_keys:
        header = _SUBSECTION_HEADERS.get(ss, ss)
        # Fix 2026-10-16: forward-purpose claims 的專用標題
        if ss == "__forward__":
            header = "未來含義"
        out.append(f"### {header}")
        out.append("")
        for c in grouped[ss]:
            out.append(_clean(c.get("text", "")))
            out.append("")
    return out


def _thesis_block(role_claims: List[Dict]) -> List[str]:
    """Render 投行核心精要: 一句話立場 + 三證據 + 反證 + 兩反轉 + 深度解讀 + 未來含義 + 估值邏輯.

    Uses `purpose` (presentation role), NOT claim_type (validation semantics).
    Unknown purpose -> "其他論述", never crashes.

    This is the Executive Summary. It does NOT replace the full analysis
    below (subsection-grouped claims). The 3 evidences / 2 reversals are
    highlights; other important claims still appear in 詳細論述.
    """
    out = ["### 一句話立場（核心論點）"]
    thesis = _claims_by_purpose(role_claims, "thesis", "verdict")
    thesis_ids = {c.get("claim_id") for c in thesis}
    if thesis:
        out.append(_clean(thesis[0].get("text", "")))
    else:
        out.append("（本節尚無明確立場）")
    out.append("")
    out.append("### 三個最強證據")
    evids = _claims_by_purpose(role_claims, "evidence")
    for i, c in enumerate(evids[:3], 1):
        out.append(f"{i}. {_clean(c.get('text', ''))}")
    if not evids:
        out.append("（待補）")
    out.append("")
    out.append("### 主要反證")
    contras = _claims_by_purpose(role_claims, "contradiction")
    if contras:
        for c in contras[:2]:
            out.append(f"- {_clean(c.get('text', ''))}")
    else:
        out.append("（待補）")
    out.append("")
    out.append("### 兩個反轉條件（何時改變看法）")
    revs = _claims_by_purpose(role_claims, "reversal")
    for i, c in enumerate(revs[:2], 1):
        out.append(f"{i}. {_clean(c.get('text', ''))}")
    if not revs:
        out.append("（待補）")
    out.append("")
    out.append("### 深度解讀")
    interps = [c for c in _claims_by_purpose(role_claims, "interpretation")
               if c.get("claim_id") not in thesis_ids]
    if interps:
        for c in interps[:3]:
            out.append(_clean(c.get("text", "")))
            out.append("")
    else:
        out.append("（待補）")
        out.append("")
    out.append("### 未來含義")
    forwards = _claims_by_purpose(role_claims, "forward")
    if forwards:
        for c in forwards[:2]:
            out.append(_clean(c.get("text", "")))
            out.append("")
    else:
        out.append("（待補）")
        out.append("")
    out.append("### 估值邏輯")
    vals = _claims_by_purpose(role_claims, "valuation", "scenario", "plan")
    if vals:
        out.append(_clean(vals[0].get("text", "")))
    else:
        out.append("（待補）")
    out.append("")
    return out


def render_report(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Render V2-style report + sidecar. Returns {report_markdown, sidecar, untraced}."""
    facts = snapshot.get("facts", {})
    calcs = snapshot.get("calcs", {})
    claims = snapshot.get("claims", [])
    trade = snapshot.get("trade", {})
    ticker = snapshot.get("ticker", "?")
    as_of = snapshot.get("as_of", "?")
    evidence = snapshot.get("evidence", {})

    by_role: Dict[str, List[Dict]] = {}
    for c in claims:
        by_role.setdefault(c.get("author_role", "?"), []).append(c)

    lines: List[str] = []
    sidecar: Dict[str, Any] = {"sections": {}}
    untraced: List[str] = []

    def section(key: str, title: str, role: str,
                fact_ids: List[str], calc_ids: List[str],
                evidence_ids: List[str], body: List[str]):
        claim_ids = [c["claim_id"] for c in by_role.get(role, [])]
        sidecar["sections"][key] = {
            "title": title, "claim_ids": claim_ids,
            "fact_ids": fact_ids, "calc_ids": calc_ids,
            "evidence_ids": evidence_ids,
            "confidences": {c["claim_id"]: c.get("confidence", 0)
                            for c in by_role.get(role, [])},
        }
        lines.append(f"## {title}\n")
        lines.extend(body)
        lines.append("")

    # ---- header ----
    lines.append(f"# {ticker} 完整投資分析報告 ({as_of})")
    lines.append("")
    lines.append(f"- **標的代碼**: {ticker}")
    lines.append(f"- **分析基準日**: {as_of}")
    lines.append(f"- **收錄章節**: 12")
    lines.append(f"- **事實數**: {len(facts)} | **計算數**: {len(calcs)} | "
                 f"**論述數**: {len(claims)}")
    lines.append("")

    # ---- TOC ----
    toc = [
        ("一、股價與技術指標摘要", "ohlcv"),
        ("二、市場與技術分析報告", "market"),
        ("三、基本面財報分析報告", "fundamentals"),
        ("四、新聞與重大事件分析", "news"),
        ("五、社群與市場情緒分析", "social"),
        ("六、多頭研究員辯論論點", "bull"),
        ("七、空頭研究員辯論論點", "bear"),
        ("八、研究主管投資計畫", "research"),
        ("九、交易員執行提案", "trader"),
        ("十、風險評估", "risk"),
        ("十一、交易決策綜合摘要", "summary"),
        ("十二、最終決策", "decision"),
    ]
    lines.append("## 目錄")
    lines.append("")
    for i, (title, _) in enumerate(toc, 1):
        lines.append(f"{i}. {title}")
    lines.append("")

    # ---- 一、OHLCV ----
    ohlcv_body = ["### 核心技術面概覽"]
    ohlcv_body.append(f"- **現價**: {_num(facts, 'fact.Close.latest')}")
    ohlcv_body.append(f"- **52 週高/低**: {_num(facts, 'fact.High52w.latest')} / "
                       f"{_num(facts, 'fact.Low52w.latest')}")
    ohlcv_body.append(f"- **均線**: SMA20 {_num(facts, 'fact.SMA20.latest')} | "
                       f"SMA50 {_num(facts, 'fact.SMA50.latest')} | "
                       f"SMA200 {_num(facts, 'fact.SMA200.latest')}")
    ohlcv_body.append(f"- **動能**: RSI(14) {_num(facts, 'fact.RSI14.latest')} | "
                       f"ATR(14) {_num(facts, 'fact.ATR14.latest')}")
    ohlcv_body.append("")

    # Monthly OHLCV table (12 months) — renderer reads FACTs only, no recalc
    m_periods = sorted({fid.split(".")[3] for fid in facts
                        if fid.startswith("fact.OHLCV.M.")})
    if m_periods:
        ohlcv_body.append("### 月線趨勢（12 個月 OHLCV）")
        ohlcv_body.append("| 月份 | 開盤 | 最高 | 最低 | 收盤 | 成交量 | 漲跌幅 |")
        ohlcv_body.append("|---|---|---|---|---|---|---|")
        for mp in m_periods[-12:]:
            o = _num(facts, f"fact.OHLCV.M.{mp}.open")
            h = _num(facts, f"fact.OHLCV.M.{mp}.high")
            l = _num(facts, f"fact.OHLCV.M.{mp}.low")
            c = _num(facts, f"fact.OHLCV.M.{mp}.close")
            v = _num(facts, f"fact.OHLCV.M.{mp}.volume")
            r = _num(facts, f"fact.ReturnM.{mp}")
            ohlcv_body.append(f"| {mp} | {o} | {h} | {l} | {c} | {v} | {r} |")
        ohlcv_body.append("")
    else:
        ohlcv_body.append("### 月線趨勢")
        ohlcv_body.append("（資料不足）")
        ohlcv_body.append("")

    # Weekly OHLCV table (12 weeks)
    w_periods = sorted({fid.split(".")[3] for fid in facts
                        if fid.startswith("fact.OHLCV.W.")})
    if w_periods:
        ohlcv_body.append("### 週線趨勢（12 週 OHLCV）")
        ohlcv_body.append("| 週別 | 開盤 | 最高 | 最低 | 收盤 | 成交量 | 漲跌幅 |")
        ohlcv_body.append("|---|---|---|---|---|---|---|")
        for wp in w_periods[-12:]:
            o = _num(facts, f"fact.OHLCV.W.{wp}.open")
            h = _num(facts, f"fact.OHLCV.W.{wp}.high")
            l = _num(facts, f"fact.OHLCV.W.{wp}.low")
            c = _num(facts, f"fact.OHLCV.W.{wp}.close")
            v = _num(facts, f"fact.OHLCV.W.{wp}.volume")
            r = _num(facts, f"fact.ReturnW.{wp}")
            ohlcv_body.append(f"| {wp} | {o} | {h} | {l} | {c} | {v} | {r} |")
        ohlcv_body.append("")
    else:
        ohlcv_body.append("### 週線趨勢")
        ohlcv_body.append("（資料不足）")
        ohlcv_body.append("")

    # Daily OHLCV table (recent 10 trading days)
    d_periods = sorted({fid.split(".")[3] for fid in facts
                        if fid.startswith("fact.OHLCV.D.")})
    if d_periods:
        ohlcv_body.append("### 日線趨勢（最近 10 個交易日）")
        ohlcv_body.append("| 日期 | 開盤 | 最高 | 最低 | 收盤 | 成交量 |")
        ohlcv_body.append("|---|---|---|---|---|---|")
        for dp in d_periods[-10:]:
            o = _num(facts, f"fact.OHLCV.D.{dp}.open")
            h = _num(facts, f"fact.OHLCV.D.{dp}.high")
            l = _num(facts, f"fact.OHLCV.D.{dp}.low")
            c = _num(facts, f"fact.OHLCV.D.{dp}.close")
            v = _num(facts, f"fact.OHLCV.D.{dp}.volume")
            ohlcv_body.append(f"| {dp} | {o} | {h} | {l} | {c} | {v} |")
        ohlcv_body.append("")
    section("ohlcv", "一、股價與技術指標摘要", "",
            ["fact.Close.latest", "fact.SMA20.latest", "fact.SMA50.latest",
             "fact.SMA200.latest", "fact.RSI14.latest", "fact.ATR14.latest",
             "fact.High52w.latest", "fact.Low52w.latest"],
            [], [], ohlcv_body)

    # ---- 二、市場分析 ----
    mkt = by_role.get("market_analyst", [])
    mkt_body = ["## 投行核心精要", ""]
    mkt_body += _thesis_block(mkt)
    mkt_body.append("### 技術指標彙整表")
    mkt_body.append("")
    mkt_body.append("| 指標 | 數值 | 解讀 |")
    mkt_body.append("|---|---|---|")
    close_v = facts.get("fact.Close.latest", {}).get("value")

    def _interp(fid: str, label: str) -> str:
        f = facts.get(fid, {})
        v = f.get("value")
        if v is None:
            return "—"
        if label in ("SMA20", "SMA50") and close_v:
            return "壓力（現價在其下）" if v > close_v else "支撐（現價在其上）"
        if label == "RSI(14)":
            if v < 30:
                return "超賣"
            if v > 70:
                return "超買"
            if v >= 50:
                return "偏強"
            return "偏弱"
        if label == "ATR(14)" and close_v:
            pct = v / close_v * 100
            if pct > 5:
                return f"高波動（{pct:.1f}%）"
            if pct > 2:
                return f"正常（{pct:.1f}%）"
            return f"低波動（{pct:.1f}%）"
        return "—"

    for fid, label in [("fact.Close.latest", "現價"),
                        ("fact.SMA20.latest", "SMA20"),
                        ("fact.SMA50.latest", "SMA50"),
                        ("fact.RSI14.latest", "RSI(14)"),
                        ("fact.ATR14.latest", "ATR(14)")]:
        if fid in facts:
            mkt_body.append(f"| {label} | {_num(facts, fid)} | {_interp(fid, label)} |")
    mkt_body.append("")
    mkt_body.append("### 詳細論述")
    mkt_body.append("")
    mkt_body.extend(_render_subsections(mkt))
    section("market", "二、市場與技術分析報告", "market_analyst",
            ["fact.Close.latest", "fact.SMA20.latest", "fact.SMA50.latest",
             "fact.RSI14.latest", "fact.ATR14.latest"],
            ["calc.dist.52whi", "calc.dist.52wlo"], [], mkt_body)

    # ---- 三、基本面 ----
    fun = by_role.get("fundamentals_analyst", [])
    fun_body = ["## 投行核心精要", ""]
    fun_body += _thesis_block(fun)
    fun_body.append("### 詳細論述")
    fun_body.append("")
    fun_body.extend(_render_subsections(fun))
    # source priority note
    fun_body.append("> **資料來源優先級**: 最新 SEC / 公司 IR > 舊 SEC > "
                    "Reuters/主流媒體 > 其他來源。標示為 reported 的數字為媒體報導，非審計財報.")
    fun_body.append("")
    section("fundamentals", "三、基本面財報分析報告", "fundamentals_analyst",
            [fid for fid in facts if "Revenue" in fid or "NetLoss" in fid or "EPS" in fid],
            [cid for cid in calcs if "margin" in cid or "ev" in cid], [],
            fun_body)

    # ---- 四、新聞 ----
    news = by_role.get("news_analyst", [])
    news_body = ["## 事件分析", ""]
    news_body.extend(_render_subsections(news))
    section("news", "四、新聞與重大事件分析", "news_analyst",
            [], [], list(evidence.keys()), news_body)

    # ---- 五、社群 ----
    soc = by_role.get("social_analyst", [])
    soc_body = ["## 情緒分析", ""]
    soc_body.extend(_render_subsections(soc))
    section("social", "五、社群與市場情緒分析", "social_analyst",
            [], [], [], soc_body)

    # ---- 六、 Bull ----
    bull = by_role.get("bull", [])
    bull_body = ["## 投行核心精要", ""]
    bull_body += _thesis_block(bull)
    bull_body.append("### 詳細論述")
    bull_body.append("")
    bull_body.extend(_render_subsections(bull))
    section("bull", "六、多頭研究員辯論論點", "bull", [], [], [], bull_body)

    # ---- 七、 Bear ----
    bear = by_role.get("bear", [])
    bear_body = ["## 投行核心精要", ""]
    bear_body += _thesis_block(bear)
    bear_body.append("### 詳細論述")
    bear_body.append("")
    bear_body.extend(_render_subsections(bear))
    section("bear", "七、空頭研究員辯論論點", "bear", [], [], [], bear_body)

    # ---- 八、研究主管 ----
    rm = by_role.get("research_manager", [])
    rm_body = ["## 投資計畫與裁決", ""]
    rm_body.extend(_render_subsections(rm))
    conflicts = snapshot.get("unresolved_conflicts", [])
    if conflicts:
        rm_body.append("### 未解衝突")
        rm_body.append("")
        for cf in conflicts:
            rm_body.append(f"- {cf}")
        rm_body.append("")
    section("research", "八、研究主管投資計畫", "research_manager",
            [], [], [], rm_body)

    # ---- 九、交易員 ----
    trd = by_role.get("trader", [])
    trd_body = []
    if trade:
        trd_body.append("### 執行提案")
        trd_body.append("")
        trd_body.append(f"- **方向**: {trade.get('direction', '?')}")
        trd_body.append(f"- **進場**: {_num(facts, 'fact.Trade.Entry')}")
        trd_body.append(f"- **停損**: {_num(facts, 'fact.Trade.Stop')}")
        trd_body.append(f"- **目標**: {_num(facts, 'fact.Trade.Target')}")
        trd_body.append("")
    trd_body.append("### 交易藍圖")
    trd_body.append("")
    trd_body.extend(_render_subsections(trd))
    section("trader", "九、交易員執行提案", "trader",
            ["fact.Trade.Entry", "fact.Trade.Stop", "fact.Trade.Target"],
            ["calc.trade.risk_reward"], [], trd_body)

    # ---- 十、風險 ----
    rsk = by_role.get("risk_manager", [])
    rsk_body = ["## 風險辯論", ""]
    rsk_body.extend(_render_subsections(rsk))
    section("risk", "十、風險評估", "risk_manager", [], [], [], rsk_body)

    # ---- 十一、綜合摘要 ----
    # Fix 2026-10-16: 不再全文重印第八章的 RM claims（逐字重複）。
    # 只放裁決摘要＋參照，詳細內容見第八章。
    sum_body = ["### 決策摘要", ""]
    sum_body.append("## 研究主管完整裁決（摘要）")
    sum_body.append("")
    rm_verdicts = [c for c in rm if c.get("purpose") == "verdict"]
    if rm_verdicts:
        for c in rm_verdicts:
            sum_body.append(_clean(c.get("text", "")))
            sum_body.append("")
    else:
        # Fallback: 取第一條 adjudication
        rm_adj = [c for c in rm if c.get("purpose") == "adjudication"]
        if rm_adj:
            sum_body.append(_clean(rm_adj[0].get("text", "")))
            sum_body.append("")
        else:
            sum_body.append("（待補）")
            sum_body.append("")
    sum_body.append("（詳細裁決與投資計畫見第八章「研究主管投資計畫」，此處不重複）")
    sum_body.append("")
    section("summary", "十一、交易決策綜合摘要", "research_manager",
            [], [], [], sum_body)

    # ---- 十二、最終決策 ----
    # Full rendering: no truncation. Complete trade blueprint from RM + Risk.
    dec_body = ["### 最終決策", ""]
    if trade:
        dec_body.append(f"**方向: {trade.get('direction', 'WAIT')}**")
        dec_body.append("")
    dec_body.append("## 風險主管完整評估（摘要）")
    dec_body.append("")
    # Fix 2026-10-16: 不再全文重印第十章的 risk claims（逐字重複）。
    # 只放最終取捨，詳細辯論見第十章。
    rsk_verdicts = [c for c in rsk if c.get("purpose") == "verdict"]
    if rsk_verdicts:
        for c in rsk_verdicts:
            dec_body.append(_clean(c.get("text", "")))
            dec_body.append("")
    else:
        dec_body.append("（待補）")
        dec_body.append("")
    dec_body.append("（詳細風險辯論見第十章「風險評估」，此處不重複）")
    dec_body.append("")
    # Also include RM adjudication for completeness
    # Fix 2026-10-16: don't re-render full text (was duplicating Chapter 11).
    # Just reference it to avoid the "Russian doll" problem.
    if rm:
        dec_body.append("## 研究主管裁決")
        dec_body.append("")
        dec_body.append("（詳見第十一章「研究主管完整裁決」，此處不重複）")
        dec_body.append("")
    section("decision", "十二、最終決策", "risk_manager", [], [], [], dec_body)

    # untraced: scan for leftover markers (should be none after _clean)
    text = "\n".join(lines)
    for m in re.findall(r"\[(fact\.[\w.]+|calc\.[\w.]+)\]", text):
        if m.startswith("fact.") and m not in facts:
            untraced.append(m)
        if m.startswith("calc.") and m not in calcs:
            untraced.append(m)

    sidecar["untraced"] = sorted(set(untraced))
    sidecar["as_of"] = as_of
    sidecar["ticker"] = ticker
    sidecar["total_claims"] = len(claims)
    sidecar["format"] = "v2-human-readable"
    return {"report_markdown": "\n".join(lines), "sidecar": sidecar,
            "untraced": sorted(set(untraced))}


# ---- Point 2: Market freshness hard gate ----
def check_market_freshness(facts: Dict[str, Dict], as_of: str) -> Dict[str, Any]:
    """Hard gate: fact.Close.latest must be the latest complete trading day
    before as_of. Returns {ok, blocking, detail}."""
    from datetime import datetime, timedelta
    close_fact = facts.get("fact.Close.latest", {})
    fact_date = close_fact.get("end") or close_fact.get("start", "")
    if not fact_date:
        return {"ok": False, "blocking": True,
                "detail": "fact.Close.latest has no date"}
    try:
        fd = datetime.strptime(fact_date[:10], "%Y-%m-%d").date()
        ao = datetime.strptime(as_of[:10], "%Y-%m-%d").date()
    except ValueError:
        return {"ok": False, "blocking": True,
                "detail": f"unparseable dates: {fact_date} / {as_of}"}
    # Latest complete trading day: as_of minus 1 day (or Friday if Monday).
    # Allow up to 4 calendar days for weekends/holidays.
    delta = (ao - fd).days
    if delta < 0:
        return {"ok": False, "blocking": True,
                "detail": f"fact date {fact_date} is AFTER as_of {as_of}"}
    if delta > 4:
        return {"ok": False, "blocking": True,
                "detail": f"stale: fact date {fact_date}, as_of {as_of} "
                          f"({delta} days behind)"}
    return {"ok": True, "blocking": False,
            "detail": f"fresh: {fact_date} vs as_of {as_of} ({delta}d)"}


# ---- Point 4: Listing status check (pre-ingest) ----
def check_listing_status(ticker: str) -> Dict[str, Any]:
    """Confirm ticker is currently listed, return exchange + status.
    Must be called before ingest. Raises on delisted/unknown."""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        info = t.info
        # If yfinance returns no meaningful info, treat as unlisted/unknown
        if not info or not info.get("symbol"):
            # Fallback: try a 5d history probe
            hist = t.history(period="5d")
            if hist.empty:
                raise ValueError(f"ticker {ticker}: no market data (unlisted?)")
        exchange = info.get("exchange", "?") if info else "?"
        return {"ticker": ticker, "exchange": exchange,
                "listed": True,
                "longName": (info or {}).get("longName", ticker)}
    except Exception as e:
        raise ValueError(f"listing check failed for {ticker}: {e}")
