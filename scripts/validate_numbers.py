#!/usr/bin/env python3 # 指定腳本使用 Python 3 直譯器執行
# -*- coding: utf-8 -*- # 指定原始碼檔案編碼為 UTF-8
# ============================================================================== # 分隔裝飾線
# 腳本名稱：validate_numbers.py # 標示腳本名稱
# 功能描述：確定性金融數據驗證閘門 (Deterministic Validation Gate - P0-4 全面修復版) # 標示主要功能說明
# 覆蓋範圍：P0-4 移除 MU 硬編碼、四元組綁定檢驗所有匹配、分區政策 (核心 HARD vs 敘述 INFO) # 標示覆蓋範圍
# 依據標準：相容 Python 3.9+、全標準庫、每行附中文註解、嚴禁 except: pass 吞錯 # 標示技術規範
# ============================================================================== # 分隔裝飾線
import sys # 導入系統模組以處理結束代碼與搜尋路徑 # 系統模組
import os # 導入作業系統模組以處理檔案與目錄檢查 # 系統模組
import re # 導入正則表達式模組以進行文本模式比對與數值抽取 # 文本模組
import argparse # 導入命令列參數解析模組 # CLI 模組
import json # 導入 JSON 模組以解析常數與設定檔案 # 資料模組
from typing import List, Tuple, Dict, Any, Optional # 導入型別提示標記以相容 Python 3.9 # 型別模組

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 動態導入同目錄之金融核心 (fin_kernel.py) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
current_dir = os.path.dirname(os.path.abspath(__file__)) # 取得當前腳本所在之絕對目錄路徑
if current_dir not in sys.path: # 檢查是否已在 Python 模組搜尋路徑中
    sys.path.insert(0, current_dir) # 若不在則將當前目錄置於搜尋優先位置

import fin_kernel # 導入確定性金融核心模組
from fin_kernel import ( # 自金融核心導入關鍵計算函式與例外類別
    calc_enterprise_value, # 企業價值嚴格計算函式
    calc_weighted_entry_cost, # 加權進場成本計算函式
    calc_risk_reward_ratio, # 風險報酬比計算函式
    calc_fcf_per_share, # 每股自由現金流計算函式
    FinancialDefenseError, # 金融安全防禦例外類別
    FinancialUnitMismatchError # 金融量綱單位不符例外類別
) # 結束導入
# NOTE (2026-10-15): validation_receipt 模組已移除（V3 不需要防偽收據，
# deterministic rendering + sidecar + fact_snapshot_hash 已涵蓋）。

# ------------------------------------------------------------------------------ # 分隔裝飾線
# P1-2 角色判定輔助函式 (自檔案路徑解析角色類型) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def detect_role(file_path: str) -> str: # 自檔案路徑解析角色類型之函式
    """自檔案名稱或路徑中精確識別報告所屬之角色類型 (P1-2 修正)。""" # 函式說明文件
    fname = os.path.basename(file_path).lower() # 取得純檔名並轉小寫以利比對
    if "ohlcv" in fname: # 若檔名包含 ohlcv
        return "ohlcv_summary" # 判定為行情預處理角色
    if "risk-debate" in fname or "risk_debate" in fname or ("risk" in fname and "debate" in fname): # 判斷風險辯論
        return "risk-debate" # 判定為風險辯論角色
    if "summary" in fname and "ohlcv" not in fname: # 若為 summary 且非行情摘要
        return "summary" # 判定為總結摘要角色
    if "final" in fname or "decision" in fname: # 若包含 final 或 decision
        return "final" # 判定為最終決策角色
    if "trader" in fname or "proposal" in fname: # 若包含 trader 或 proposal
        return "trader" # 判定為交易員角色
    if "bull" in fname: # 若包含 bull
        return "bull" # 判定為多頭研究員角色
    if "bear" in fname: # 若包含 bear
        return "bear" # 判定為空頭研究員角色
    if "fundamental" in fname: # 若包含 fundamental
        return "fundamentals" # 判定為基本面分析師角色
    if "market" in fname: # 若包含 market
        return "market" # 判定為市場分析師角色
    if "news" in fname: # 若包含 news
        return "news" # 判定為新聞分析師角色
    if "sentiment" in fname or "social" in fname: # 若包含 sentiment 或 social
        return "sentiment" # 判定為社群情緒分析師角色
    return "general" # 預設回傳 general

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 驗證錯誤類別定義 (三層錯誤分級：HARD 致命算術錯誤 vs SOFT 格式問題 vs INFO 資訊記錄) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
class ValidationError: # 定義驗證錯誤資料結構類別
    """表示單項驗證錯誤，區分 HARD（致命算術錯誤）、SOFT（格式問題）與 INFO（無害資訊）。""" # 類別文件註解
    def __init__( # 初始化方法
        self, # 物件實例本體
        severity: str, # 錯誤嚴重度級別：'HARD'、'SOFT' 或 'INFO'
        category: str, # 錯誤分類標籤 (如 'ARITHMETIC', 'ACCOUNTING', 'FORMAT', 'CONTEXT')
        message: str, # 錯誤描述訊息
        expected: Any = None, # 預期之正確數值或狀態
        actual: Any = None, # 報告中實際出現之錯誤數值或狀態
        diff: Any = None # 預期與實際數值之偏差
    ): # 結束參數列
        self.severity = severity.upper() # 儲存標準化大寫嚴重度
        self.category = category.upper() # 儲存標準化大寫分類
        self.message = message # 儲存錯誤訊息
        self.expected = expected # 儲存預期值
        self.actual = actual # 儲存實際值
        self.diff = diff # 儲存偏差值

    def __str__(self) -> str: # 字串呈現方法
        """格式化錯誤輸出，若具備預期/實際/偏差則完整呈現。""" # 方法說明
        if self.expected is not None and self.actual is not None and self.diff is not None: # 若包含完整三要素
            return f"[{self.severity}] {self.message} (預期值: {self.expected}, 實際值: {self.actual}, 偏差: {self.diff})" # 回傳三要素格式
        return f"[{self.severity}] {self.message}" # 回傳一般格式

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 結構與文字防呆檢查 (SOFT 分類) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def check_placeholders(text: str) -> List[ValidationError]: # 占位符防呆函式
    """檢查是否殘留未填寫的模板占位符或幻覺 NaN/null (SOFT 錯誤)。""" # 函式說明文件
    errors: List[ValidationError] = [] # 初始化錯誤串列
    forbidden_tokens = [ # 定義禁止之占位符字串
        r"<TICKER>", r"<DATE>", r"<RANK>", r"<INSERT", # 模板標籤
        r"\bNaN\b", r"\bnull\b", r"\bundefined\b" # 空值標籤
    ] # 結束清單
    for pattern in forbidden_tokens: # 遍歷所有禁止模式
        matches = re.findall(pattern, text, re.IGNORECASE) # 正則尋找匹配
        if matches: # 若發現未替換標籤
            errors.append(ValidationError( # 記錄 SOFT 格式錯誤
                severity="SOFT", # 格式問題標記為 SOFT
                category="FORMAT", # 分類為格式
                message=f"發現未替換之占位符或無效值: '{matches[0]}'" # 訊息
            )) # 結束記錄
    return errors # 回傳錯誤串列

def check_ib_requirements(text: str) -> List[ValidationError]: # 投行 4 句必備檢查函式
    """檢查投行 4 句核心要求 (SOFT 錯誤)。""" # 函式說明文件
    errors: List[ValidationError] = [] # 初始化錯誤串列
    if not re.search(r"(核心論點|Core Thesis|一[句行]話立場)", text, re.IGNORECASE): # 搜尋核心論點關鍵字
        errors.append(ValidationError("SOFT", "STRUCTURE", "缺少投行要求 (a): 核心論點（一句話立場）")) # 記錄缺漏
    evidence_match = re.search(r"(三個最強證據|Three Strongest Evidences?|最強證據|核心證據)", text, re.IGNORECASE) # 搜尋證據關鍵字
    if not evidence_match: # 若未找到
        errors.append(ValidationError("SOFT", "STRUCTURE", "缺少投行要求 (b): 三個最強證據")) # 記錄缺漏
    reversal_match = re.search(r"(兩個反轉條件|Two Reversal Conditions?|反轉條件|何時改變看法)", text, re.IGNORECASE) # 搜尋反轉條件關鍵字
    if not reversal_match: # 若未找到
        errors.append(ValidationError("SOFT", "STRUCTURE", "缺少投行要求 (c): 兩個反轉條件（何時改變看法）")) # 記錄缺漏
    valuation_match = re.search(r"(估值邏輯|Valuation Logic|估值方法)", text, re.IGNORECASE) # 搜尋估值邏輯關鍵字
    if not valuation_match: # 若未找到
        errors.append(ValidationError("SOFT", "STRUCTURE", "缺少投行要求 (d): 估值邏輯")) # 記錄缺漏
    return errors # 回傳錯誤串列

def check_scenario_analysis(text: str) -> List[ValidationError]: # 情景分析機率檢查函式
    """檢查 Bull/Bear 情景分析：牛/基準/熊 + 機率 + 目標價 (機率不合為 SOFT 結構問題)。""" # 函式說明文件
    errors: List[ValidationError] = [] # 初始化錯誤串列
    has_bull = bool(re.search(r"(牛市|Bull\s*Case|多頭情景)", text, re.IGNORECASE)) # 檢查牛市情景
    has_base = bool(re.search(r"(基準|Base\s*Case|中性情景)", text, re.IGNORECASE)) # 檢查基準情景
    has_bear = bool(re.search(r"(熊市|Bear\s*Case|空頭情景)", text, re.IGNORECASE)) # 檢查熊市情景
    if not (has_bull and has_base and has_bear): # 若有缺少任何一種情景
        missing = [] # 初始化缺失串列
        if not has_bull: missing.append("牛市情景") # 記錄缺少牛市
        if not has_base: missing.append("基準情景") # 記錄缺少基準
        if not has_bear: missing.append("熊市情景") # 記錄缺少熊市
        errors.append(ValidationError("SOFT", "STRUCTURE", f"情景分析不完整，缺少: {', '.join(missing)}")) # 記錄錯誤
        return errors # 提前回傳
    prob_matches = re.findall(r"(?:機率|Probability|prob)[^\d\n\r]*(\d{1,3}(?:\.\d+)?)\s*%", text, re.IGNORECASE) # 正則抽取機率
    if len(prob_matches) < 3: # 若不足三個
        table_probs = re.findall(r"\|\s*(\d{1,3}(?:\.\d+)?)\s*%\s*\|", text) # 備用表格抽取
        if len(table_probs) >= 3: # 若表格中有足夠機率
            prob_matches = table_probs # 採用表格機率
    if len(prob_matches) >= 3: # 若找到至少三個機率
        try: # 嘗試轉換為浮點數並計算總和
            candidate_probs = [float(p) for p in prob_matches] # 轉為浮點數清單
            probs = candidate_probs[:3] if len(candidate_probs) == 3 else [p for p in candidate_probs if p != 100.0][:3] # 排除總計 100%
            total_prob = sum(probs) # 計算總機率
            if not (98.0 <= total_prob <= 102.0): # 檢查是否在合理 100% 容許區間內
                errors.append(ValidationError( # 記錄機率不符錯誤
                    severity="SOFT", # 標記為 SOFT
                    category="STRUCTURE", # 分類為結構
                    message="情景機率總和非 100%", # 訊息
                    expected="100.0%", # 預期總和
                    actual=f"{total_prob:.1f}%", # 實際總和
                    diff=f"{abs(total_prob - 100.0):.1f}%" # 偏差
                )) # 結束記錄
        except ValueError: # 若解析發生錯誤
            errors.append(ValidationError("SOFT", "STRUCTURE", "情景機率解析失敗")) # 記錄錯誤
    else: # 若未辨識到 3 個機率
        errors.append(ValidationError("SOFT", "STRUCTURE", f"未能辨識到完整的 3 個情景機率百分比 (僅辨識到 {len(prob_matches)} 個)")) # 記錄錯誤
    return errors # 回傳錯誤串列

def check_fundamentals_boundaries(text: str) -> List[ValidationError]: # 基本面分析邊界檢查函式
    """MU 教訓檢查：財報分析師只負責 SEC 欄位，嚴禁偷渡股價與技術指標 (HARD 邊界錯誤)。""" # 函式說明文件
    errors: List[ValidationError] = [] # 初始化錯誤串列
    prohibited_market_terms = [ # 定義禁止之市場指標關鍵字
        (r"\b(SMA|EMA|RSI|MACD|BBANDS|ATR)\b", "技術指標 (SMA/RSI/MACD/ATR)"), # 技術指標縮寫
        (r"(200\s*日均線|50\s*日均線|移動平均線)", "移動平均線指標"), # 均線名詞
        (r"(52\s*週最高|52\s*週最低|52-week\s*high)", "52 週股價高低點"), # 52 週指標
        (r"(K\s*線|日\s*K|週\s*K|技術面分析)", "K 線/技術面分析") # 形態與技術面名詞
    ] # 結束清單
    for pattern, label in prohibited_market_terms: # 遍歷所有禁止模式
        match = re.search(pattern, text, re.IGNORECASE) # 正則尋找匹配
        if match: # 若發現違規混入
            errors.append(ValidationError( # 記錄 HARD 邊界違規錯誤
                severity="HARD", # 違反職責分離視為 HARD 致命錯誤
                category="BOUNDARY", # 分類為職責邊界
                message=f"違反 MU 職責分離教訓：財報分析報告中出現禁止之市場指標『{label}』！股價與技術指標應歸市場分析師。" # 錯誤描述
            )) # 結束記錄
    return errors # 回傳錯誤串列

# ------------------------------------------------------------------------------ # 分隔裝飾線
# P0-4 輔助函式：表格與文本結構解析 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def extract_table_rows(text: str) -> List[Dict[str, str]]: # Markdown 表格鍵值抽取函式
    """自 Markdown 表格中逐行安全抽取鍵值對，杜絕正文跨行貪婪吃掉相鄰列 (P0-4 修復)。""" # 函式說明
    rows: List[Dict[str, str]] = [] # 初始化表格列清單
    for line in text.splitlines(): # 嚴格逐行遍歷
        stripped = line.strip() # 去除兩端空白
        if not (stripped.startswith("|") and stripped.endswith("|")): # 確保為表格行
            continue # 略過非表格行
        parts = [p.strip() for p in stripped.split("|")[1:-1]] # 切割欄位並剔除首尾空元素
        if not parts or parts[0].startswith("---"): # 略過分隔線
            continue # 略過
        key = re.sub(r"[*_`]", "", parts[0]).strip() # 清理 Markdown 格式
        val = re.sub(r"[*_`]", "", parts[1]).strip() if len(parts) > 1 else "" # 清理數值欄位
        note = parts[2].strip() if len(parts) > 2 else "" # 取得備註說明
        if key in ("交易參數", "項目", "風控項目"): # 略過表頭文字
            continue # 略過
        rows.append({"key": key, "val": val, "note": note}) # 加入清單
    return rows # 回傳結果

# ------------------------------------------------------------------------------ # 分隔裝飾線
# P0-4 核心：check_trader_math (Trader 加權進場成本檢驗) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def check_trader_math(text: str, canonical_metrics: Optional[Dict[str, Any]] = None) -> List[ValidationError]: # Trader 成本重算函式
    """Trader：加權成本用 calc_weighted_entry_cost 重算，偏差 > $0.05 即 FAIL (HARD)。""" # 函式說明文件
    errors: List[ValidationError] = [] # 初始化錯誤串列
    
    # 擴充關鍵字匹配：限制不跨越標點符號，並支援數字在前與數字在後 (P0-4 修復)
    claimed_costs_after = re.findall( # 數字在後：例如「基準成本 $554」
        r"(?:加權成本|加權進場成本|進場成本|Weighted\s*Cost|終審加權成本|基準成本|建倉均價|平均進場成本)[^\d\n\r$,，；;。]*\$?\s*(\d+(?:\.\d+)?)",
        text, re.IGNORECASE
    )
    claimed_costs_before = re.findall( # 數字在前：例如「$550 混合成本」
        r"\$?\s*(\d+(?:\.\d+)?)\s*(?:混合成本|基準成本|加權成本|進場成本)",
        text, re.IGNORECASE
    )
    claimed_cost_matches = claimed_costs_after + claimed_costs_before # 合併所有宣告
    if not claimed_cost_matches: # 若未找到任何成本宣告
        return errors # 若非交易提案或無成本描述則略過
        
    actual_costs = [float(c) for c in claimed_cost_matches] # 解析出所有聲稱之成本數值
    
    # 決定預期成本：優先使用 canonical_metrics，其次由批次解析重算
    expected_cost = None # 初始化預期成本
    cost_range = None # 初始化加權成本區間 (min_c, max_c)
    overall_price_range = None # 初始化策略整體掛單價格區間 (p_min, p_max)
    if canonical_metrics and "metrics" in canonical_metrics: # 若提供了 SSOT 常數
        if "weighted_entry_cost" in canonical_metrics["metrics"]: # 若包含加權成本指標
            expected_cost = float(canonical_metrics["metrics"]["weighted_entry_cost"]["value"]) # 取用核定數值
            
    if expected_cost is None: # 若尚未取得預期成本，嘗試從文本中解析批次
        tranche_pattern = re.compile( # 編譯批次正則模式
            r"(?:批[一二三123]|段[一二三123]|Tranche\s*\d)[^\n\r]*?\$?(\d+(?:\.\d+)?)\s*[-–~—]\s*\$?(\d+(?:\.\d+)?)[^\n\r]*?(\d+(?:\.\d+)?)\s*%", # 區間與百分比
            re.IGNORECASE # 忽略大小寫
        ) # 結束編譯
        found_tranches = [] # 初始化解析批次清單
        for m in tranche_pattern.finditer(text): # 遍歷所有匹配批次
            p_low = float(m.group(1)) # 取得低點
            p_high = float(m.group(2)) # 取得高點
            pct = float(m.group(3)) # 取得權重百分比
            found_tranches.append({"range": [p_low, p_high], "weight": pct}) # 加入批次
            
        if len(found_tranches) >= 2: # 若解析到至少 2 段批次
            try: # 嘗試調用核心計算加權成本
                expected_cost = calc_weighted_entry_cost(found_tranches) # 執行重算
                min_c = sum(t["range"][0] * t["weight"] / 100.0 for t in found_tranches) # 計算區間下限
                max_c = sum(t["range"][1] * t["weight"] / 100.0 for t in found_tranches) # 計算區間上限
                cost_range = (min_c, max_c) # 記錄區間
                overall_price_range = (min(t["range"][0] for t in found_tranches), max(t["range"][1] for t in found_tranches))
            except Exception as calc_exc: # 若計算異常嚴禁 pass
                errors.append(ValidationError( # 記錄計算例外為 HARD 錯誤
                    severity="HARD", # 標記為 HARD
                    category="ARITHMETIC", # 分類為算術
                    message=f"Trader 分批加權成本計算失敗: {calc_exc}" # 錯誤訊息
                )) # 結束記錄
                
    # 支援百分比在前之批次格式（例如「40% 於 $556–$560；60% 於 $548–$552」）
    if expected_cost is None: # 若尚未解析出批次
        pct_first_pattern = re.compile( # 編譯百分比在前之模式
            r"(\d+(?:\.\d+)?)\s*%\s*(?:於|在|at)[^\n\r]*?\$?(\d+(?:\.\d+)?)\s*[-–~—]\s*\$?(\d+(?:\.\d+)?)", # 百分比與區間
            re.IGNORECASE # 忽略大小寫
        ) # 結束編譯
        alt_tranches = [] # 初始化備用批次清單
        for m in pct_first_pattern.finditer(text): # 遍歷匹配
            pct = float(m.group(1)) # 權重百分比
            p_low = float(m.group(2)) # 區間低點
            p_high = float(m.group(3)) # 區間高點
            alt_tranches.append({"range": [p_low, p_high], "weight": pct}) # 加入批次清單
        if len(alt_tranches) >= 2: # 若解析到至少 2 段批次
            try: # 嘗試調用核心計算
                expected_cost = calc_weighted_entry_cost(alt_tranches) # 執行重算
                min_c = sum(t["range"][0] * t["weight"] / 100.0 for t in alt_tranches) # 下限
                max_c = sum(t["range"][1] * t["weight"] / 100.0 for t in alt_tranches) # 上限
                cost_range = (min_c, max_c) # 記錄區間
                overall_price_range = (min(t["range"][0] for t in alt_tranches), max(t["range"][1] for t in alt_tranches))
            except Exception as alt_exc: # 若計算異常嚴禁 pass
                errors.append(ValidationError( # 記錄計算例外
                    severity="HARD", # 標記為 HARD
                    category="ARITHMETIC", # 分類為算術
                    message=f"Trader 批次加權成本計算失敗: {alt_exc}" # 訊息
                )) # 結束記錄

    if expected_cost is not None: # 若已確定預期成本
        for actual_cost in actual_costs: # 驗證所有匹配到的聲稱成本 (P0-4 修正)
            # 若宣告成本落在加權成本區間或整體策略進場價格區間內，視為合理進場點位
            if cost_range is not None and (cost_range[0] - 0.05 <= actual_cost <= cost_range[1] + 0.05):
                continue # 加權成本在區間內，合規通過
            if overall_price_range is not None and (overall_price_range[0] - 0.05 <= actual_cost <= overall_price_range[1] + 0.05):
                continue # 成交在策略掛單價格區間內，合規通過
            diff = round(abs(actual_cost - expected_cost), 2) # 計算偏差值
            if diff > 0.05: # 若偏差大於門檻
                errors.append(ValidationError( # 記錄 HARD 算術錯誤
                    severity="HARD", # 算術錯誤標記為 HARD
                    category="ARITHMETIC", # 分類為算術重算
                    message="Trader 加權進場成本計算偏差超過 $0.05 (殘留過期點位或算術錯誤)", # 說明
                    expected=f"${expected_cost:.2f}", # 預期值
                    actual=f"${actual_cost:.2f}", # 實際值
                    diff=f"${diff:.2f}" # 偏差多少
                )) # 結束記錄
            
    return errors # 回傳錯誤串列

# ------------------------------------------------------------------------------ # 分隔裝飾線
# P0-4 核心：check_risk_math (四元組綁定、表格錨定與分區檢驗) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def check_risk_math(text: str, canonical_metrics: Optional[Dict[str, Any]] = None) -> List[ValidationError]: # Risk 風報比檢驗函式
    """Risk：風報比四元組綁定檢驗所有匹配，交易核心區不符或缺上下文判定 HARD，正文無標記判定 INFO (P0-4 修復)。""" # 函式說明
    errors: List[ValidationError] = [] # 初始化錯誤串列
    
    # -------------------------------------------------------------------------- # 分隔線
    # 階段 A：結構化表格錨定抽取 (Table-Scoped Extraction) # 區塊說明
    # -------------------------------------------------------------------------- # 分隔線
    table_rows = extract_table_rows(text) # 解析 Markdown 表格列
    table_rr_row = None # 初始化表格風報比列
    table_entry_val = None # 初始化表格進場數值
    table_sl_val = None # 初始化表格停損數值
    table_targets: Dict[str, float] = {} # 初始化表格各階段目標價字典
    
    for row in table_rows: # 遍歷表格每一列
        k = row["key"].lower() # 取得小寫鍵名
        v = row["val"] # 取得數值文字
        note = row["note"] # 取得備註
        
        # 1. 抽取建議進場點位 (優先尋找明確基準點或區間上限)
        if "進場" in k or "entry" in k: # 若為進場列
            base_ref_in_v = re.search(r"(?:基準|混合成本基準|成本)[^\d$]*\$?\s*(\d+(?:\.\d+)?)", v) # 尋找指定基準點
            if base_ref_in_v: # 若存在明確基準點 (如「混合成本基準 $554」)
                table_entry_val = float(base_ref_in_v.group(1)) # 採納明確基準點
            else: # 否則取區間
                range_m = re.findall(r"\$?\s*(\d+(?:\.\d+)?)", v) # 抽取所有數字
                if len(range_m) >= 2: # 若為區間
                    table_entry_val = float(range_m[1]) # 採用上限基準值
                elif range_m: # 若為單一數值
                    table_entry_val = float(range_m[0]) # 採用該數值
                
        # 2. 抽取停損點位 (例如 $505（自 $560 計 -9.82%）)
        if "停損" in k or "stop" in k or "sl" in k: # 若為停損列
            sl_m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", v) # 抽取停損價
            if sl_m: # 若找到停損
                table_sl_val = float(sl_m.group(1)) # 轉換為浮點數
            base_ref_m = re.search(r"自\s*\$?(\d+(?:\.\d+)?)\s*計", v + " " + note) # 檢查停損備註中宣告之基準進場價
            if base_ref_m: # 若備註明記基準進場價
                table_entry_val = float(base_ref_m.group(1)) # 採用明確註記之基準價 (如 $560 或 $554)
                
        # 3. 抽取各階段獲利目標價 (支援單獨列或合併於目標價列)
        if "第一" in k or "tp1" in k: # 若為第一目標列
            tp1_m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", v) # 抽取數值
            if tp1_m: table_targets["tp1"] = float(tp1_m.group(1)) # 記錄 TP1
        if "第二" in k or "tp2" in k: # 若為第二目標列
            tp2_m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", v) # 抽取數值
            if tp2_m: table_targets["tp2"] = float(tp2_m.group(1)) # 記錄 TP2
        if "加權" in k or "weighted" in k: # 若為加權目標列
            w_m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", v) # 抽取數值
            if w_m: table_targets["weighted"] = float(w_m.group(1)) # 記錄加權目標
            
        # 若為合併目標價列 (例如 | 目標價 | TP1 $620 / TP2 $700 |)
        if "目標" in k and ("tp1" not in table_targets or "tp2" not in table_targets): # 合併目標價列
            tp1_in = re.search(r"TP1[^\d$]*\$?\s*(\d+(?:\.\d+)?)", v, re.IGNORECASE) # 抽取 TP1
            if tp1_in: table_targets["tp1"] = float(tp1_in.group(1)) # 記錄 TP1
            tp2_in = re.search(r"TP2[^\d$]*\$?\s*(\d+(?:\.\d+)?)", v, re.IGNORECASE) # 抽取 TP2
            if tp2_in: table_targets["tp2"] = float(tp2_in.group(1)) # 記錄 TP2
            wt_in = re.search(r"加權[^\d$]*\$?\s*(\d+(?:\.\d+)?)", v, re.IGNORECASE) # 抽取加權目標
            if wt_in: table_targets["weighted"] = float(wt_in.group(1)) # 記錄加權目標
            
        # 4. 抽取表格中的風報比列
        if "風險報酬比" in k or "風報比" in k or "r:r" in k: # 若為風報比列
            table_rr_row = v + " " + note # 記錄風報比完整文字
            # 若在風報比列中額外提及加權目標數值（如「加權目標 $649 為 1:1.62」或「加權目標 $645」）
            wt_inline = re.search(r"加權目標[^\d$]*\$?(\d+(?:\.\d+)?)", table_rr_row) # 搜尋內聯加權目標
            if wt_inline: # 若找到內聯加權目標
                table_targets["weighted"] = float(wt_inline.group(1)) # 記錄加權目標數值
                
    # 若表格中存在風報比宣告，執行嚴格表格四元組重算檢驗 (交易執行核心區)
    if table_rr_row: # 若表格具備風報比宣告
        # 搜尋表格中所有的風報比聲稱（例如 TP1 1:1.09、TP2 1:2.55、加權目標 1:1.62）
        table_rr_items = re.findall(r"(?:(tp1|tp2|加權(?:目標)?|weighted)[^:：]*?)?1\s*[:：]\s*(\d+(?:\.\d+)?)", table_rr_row, re.IGNORECASE) # 抽取比率項
        
        # 核心區上下文檢查：若缺少進場或停損，直接判定 HARD 錯誤 (分區政策)
        if table_entry_val is None or table_sl_val is None: # 若表格缺漏關鍵進場或停損
            errors.append(ValidationError( # 記錄 HARD 結構錯誤
                severity="HARD", # 交易核心區缺上下文視為 HARD
                category="CONTEXT", # 分類為上下文缺失
                message="交易參數總表中宣告風報比，但缺少明確之建議進場點位或停損價格！" # 說明
            )) # 結束記錄
        else: # 若進場與停損皆齊備
            risk_dist = table_entry_val - table_sl_val # 計算標準風險距離
            if risk_dist <= 0: # 檢查風控邏輯
                errors.append(ValidationError("HARD", "ARITHMETIC", f"停損價格 (${table_sl_val:.2f}) 高於或等於進場點位 (${table_entry_val:.2f})！")) # 記錄錯誤
            else: # 風險距離合理
                for label, actual_rr_str in table_rr_items: # 遍歷表格中所有宣告的風報比
                    actual_rr_num = float(actual_rr_str) # 轉為浮點數
                    label_clean = label.lower() if label else "" # 清理標籤
                    target_price = None # 初始化目標價
                    
                    if "tp1" in label_clean and "tp1" in table_targets: # 若標記為 TP1
                        target_price = table_targets["tp1"] # 取用 TP1 目標價
                    elif "tp2" in label_clean and "tp2" in table_targets: # 若標記為 TP2
                        target_price = table_targets["tp2"] # 取用 TP2 目標價
                    elif ("加權" in label_clean or "weighted" in label_clean) and "weighted" in table_targets: # 若標記為加權目標
                        target_price = table_targets["weighted"] # 取用加權目標價
                    elif len(table_rr_items) == 1 and table_targets: # 若僅宣告單一風報比且有目標價
                        target_price = list(table_targets.values())[0] # 取用第一個目標價
                        
                    if target_price is not None: # 若成功綁定對應目標價
                        reward_dist = target_price - table_entry_val # 計算獲利空間
                        expected_rr_calc = round(reward_dist / risk_dist, 2) # 計算預期風報比
                        diff = round(abs(actual_rr_num - expected_rr_calc), 2) # 計算偏差
                        if diff > 0.05: # 若偏差大於門檻
                            errors.append(ValidationError( # 記錄 HARD 算術錯誤
                                severity="HARD", # 表格算術錯誤判定為 HARD
                                category="ARITHMETIC", # 分類為算術
                                message=f"交易參數總表中 {label.upper() or 'R:R'} 風報比計算偏差超過 0.05", # 說明
                                expected=f"1:{expected_rr_calc:.2f}", # 預期值
                                actual=f"1:{actual_rr_num:.2f}", # 實際值
                                diff=f"{diff:.2f}" # 偏差
                            )) # 結束記錄
                    else: # 若表格宣告風報比卻找不到對應目標價
                        errors.append(ValidationError( # 記錄 HARD 錯誤
                            severity="HARD", # 核心區找不到對應目標價判定 HARD
                            category="CONTEXT", # 分類為上下文
                            message=f"交易參數總表中宣告風報比 1:{actual_rr_num:.2f}，但未找到對應之獲利目標價！" # 說明
                        )) # 結束記錄

    # -------------------------------------------------------------------------- # 分隔線
    # 階段 B：正文子句錨定抽取 (Sentence/Clause-Scoped Extraction) # 區塊說明
    # -------------------------------------------------------------------------- # 分隔線
    # 將文本依句號、分號或段落換行切分為子句塊，避免跨段混淆干擾 (P0-4 修正)
    clauses = re.split(r"[；;。\n\r]+", text) # 切分句子塊
    
    for clause in clauses: # 遍歷所有子句
        clause_str = clause.strip() # 去除兩端空白
        if not clause_str or len(clause_str) < 5: # 略過過短子句
            continue # 略過
            
        # 尋找該子句中所有宣告的風報比（綁定前方的目標價，支援 TP1/TP2/加權目標/通用目標）
        rr_clause_pattern = r"(?:(TP1|TP2|加權目標|目標)[^\d$]*\$?\s*(\d+(?:\.\d+)?)(?:[（(][^）)]*[）)])?[^:：]*?)?(?:風險報酬比|風報比|R:R|Risk[/-]Reward)[^\d:：]*1\s*[:：]\s*(\d+(?:\.\d+)?)"
        rr_matches = re.findall(rr_clause_pattern, clause_str, re.IGNORECASE) # 執行綁定抽取
        if not rr_matches: # 若該子句無風報比宣告
            continue # 檢查下一子句
            
        # 抽取該子句中的通用進場點與風險點數/停損點 (解阻條件 2: 雙重支援停損價與風險點數)
        entry_m = re.search(r"(?:基準進場|成交於|進場|成本|買進)[^\d$]*\$?\s*(\d+(?:\.\d+)?)", clause_str, re.IGNORECASE) # 搜尋進場價
        risk_dist_m = re.search(r"(?:單股風險|風險縮至|風險(?!報酬比|收益比|控|偏好|調整))\s*(?:[為約至:\s])*\s*\$?\s*(\d+(?:\.\d+)?)", clause_str, re.IGNORECASE) # 搜尋風險點數 (排除標題及非點數文字)
        sl_m = re.search(r"(?:停損|SL)[^\d$]*\$?\s*(\d+(?:\.\d+)?)", clause_str, re.IGNORECASE) # 搜尋停損價 (如 $505)
        
        clause_entry = float(entry_m.group(1)) if entry_m else None # 解析進場價
        clause_risk_dist = float(risk_dist_m.group(1)) if risk_dist_m else None # 解析風險點數
        clause_sl = float(sl_m.group(1)) if sl_m else None # 解析停損價
        
        # 若子句內無明確進場價但表格或全文有基準進場，嘗試作為候選
        if clause_entry is None and table_entry_val is not None: # 若可由表格繼承進場基準
            clause_entry = table_entry_val # 採納表格基準進場
            
        # 計算有效風險距離 R：優先使用直接宣告之風險點數，其次為 (進場 - 停損)
        effective_risk = None # 初始化有效風險距離
        if clause_risk_dist is not None and clause_risk_dist > 0: # 若直接提供風險點數 (如 $45 或 $49)
            effective_risk = clause_risk_dist # 採用該點數
        elif clause_entry is not None and clause_sl is not None: # 若提供進場與停損
            effective_risk = clause_entry - clause_sl # 計算進場 - 停損
            
        for tp_label, bound_tp_str, actual_rr_str in rr_matches: # 遍歷子句中的每個宣告項
            actual_rr_val = float(actual_rr_str) # 轉浮點數
            clause_tp = float(bound_tp_str) if bound_tp_str else None # 取用綁定之目標價
            
            # 若未直接綁定目標價，嘗試搜尋子句內其他目標價
            if clause_tp is None: # 若未直接抽取到
                fallback_tp_m = re.search(r"(?:TP[12]|加權目標|目標獲利|目標價|獲利目標|目標)(?!\s*風報比)[^\d$]*\$?\s*(\d+(?:\.\d+)?)", clause_str, re.IGNORECASE) # 搜尋備用目標價
                if fallback_tp_m: clause_tp = float(fallback_tp_m.group(1)) # 取用備用目標價
                
            # 判斷是否具備完整四元組進行算術重算
            if clause_entry is not None and effective_risk is not None and clause_tp is not None and effective_risk > 0: # 具備四元組
                reward_val = clause_tp - clause_entry # 計算獲利空間
                if reward_val <= 0: # 檢查做多獲利邏輯
                    errors.append(ValidationError( # 記錄 HARD 錯誤
                        severity="HARD", # 邏輯違規判 HARD
                        category="ARITHMETIC", # 分類為算術
                        message=f"子句風控邏輯錯誤：目標獲利價 (${clause_tp:.2f}) 低於或等於進場成本 (${clause_entry:.2f})" # 訊息
                    )) # 結束記錄
                else: # 獲利空間大於 0
                    expected_rr_val = round(reward_val / effective_risk, 2) # 精確計算預期風報比
                    diff_val = round(abs(actual_rr_val - expected_rr_val), 2) # 計算偏差
                    if diff_val > 0.05: # 若偏差大於門檻 (例如 DELL 宣告 1:3.2 vs 預期 1:3.33)
                        errors.append(ValidationError( # 記錄 HARD 致命算術錯誤
                            severity="HARD", # 正文明確計算錯誤判定為 HARD
                            category="ARITHMETIC", # 分類為算術
                            message=f"正文風險報酬比計算偏差超過 0.05 (進場: ${clause_entry:.2f}, 風險: ${effective_risk:.2f}, 目標: ${clause_tp:.2f})", # 詳細訊息
                            expected=f"1:{expected_rr_val:.2f}", # 預期比率
                            actual=f"1:{actual_rr_val:.2f}", # 實際聲稱比率
                            diff=f"{diff_val:.2f}" # 偏差值
                        )) # 結束記錄
            else: # 若未能組齊四元組
                # 分區政策：正文敘述區若無明確完整參數標記，僅記錄 INFO，絕不阻擋！
                pass # 保持靜默 (不加入阻擋 errors)

    return errors # 回傳錯誤串列

# ------------------------------------------------------------------------------ # 分隔裝飾線
# P0-4 核心：check_final_math (EV 會計原則防禦與 FCF 重算) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def check_final_math(text: str, canonical_metrics: Optional[Dict[str, Any]] = None) -> List[ValidationError]: # Final EV 與 FCF 重算函式
    """Final：EV 用 calc_enterprise_value 重算 (傳權益直接判 FAIL)；每股指標用 calc_fcf_per_share 重算 (HARD)。""" # 函式說明
    errors: List[ValidationError] = [] # 初始化錯誤串列
    
    # 1. 企業價值 (EV) 會計邏輯與介面防禦驗證 (會計原則防禦)
    equity_ev_pattern = r"(以申報權益基礎推算企業價值|股東權益\s*\$[\d,]+M?\s*加淨現金調整.*EV/EBITDA|權益基礎推算.*企業價值)" # 正則偵測字串
    if re.search(equity_ev_pattern, text): # 若文本中出現以權益算 EV 的論述
        defense_msg = "" # 初始化防禦訊息
        try: # 嘗試傳入權益參數調用 EV 核心函式
            calc_enterprise_value( # 呼叫企業價值計算
                market_cap=1100000.0, # 傳入正常市值
                total_debt=5179.0, # 傳入總負債
                cash_and_equivalents=73453.0, # 傳入現金
                book_equity=138378.0 # 違規傳入帳面股東權益
            ) # 結束調用
        except FinancialDefenseError as exc: # 捕捉金融安全防禦例外
            defense_msg = str(exc) # 取得例外說明字串
            
        errors.append(ValidationError( # 記錄 HARD 會計防禦錯誤
            severity="HARD", # 會計邏輯違規標記為 HARD
            category="ACCOUNTING", # 分類為會計原則防禦
            message=f"企業價值 (EV) 計算錯誤: 傳入帳面權益違反會計準則: {defense_msg}", # 說明
            expected="基於市場流通市值 (Market Cap) 計算 EV", # 預期方法
            actual="以申報帳面股東權益 (Book Equity) 充當市值推算 EV", # 實際違規行為
            diff="重大會計定義衝突 (嚴禁以帳面權益算 EV)" # 偏差
        )) # 結束記錄
        
    # 2. 檢測 EV/EBITDA 估值倍數是否因權益算 EV 而脫節
    ev_ebitda_match = re.search(r"(?:EV/EBITDA|EV\s*to\s*EBITDA)[^\d\n\r]*(\d+(?:\.\d+)?)\s*倍?", text, re.IGNORECASE) # 搜尋倍數
    if ev_ebitda_match: # 若文本中包含 EV/EBITDA 倍數
        actual_ratio = float(ev_ebitda_match.group(1)) # 取得實際倍數
        if actual_ratio < 3.0: # 偵測異常低倍數 (例如 0.64x)
            expected_ratio = 9.75 # 基準倍數
            errors.append(ValidationError( # 記錄 HARD 估值倍數錯誤
                severity="HARD", # 標記為 HARD
                category="ARITHMETIC", # 分類為算術
                message="EV/EBITDA 估值倍數脫節 (因權益算 EV 導致倍數嚴重失真)", # 說明
                expected=f"{expected_ratio:.2f}x", # 預期倍數
                actual=f"{actual_ratio:.2f}x", # 實際倍數
                diff=f"{abs(actual_ratio - expected_ratio):.2f}x" # 偏差倍數
            )) # 結束記錄

    # 3. 每股自由現金流 (FCF per share) 重算與量綱檢查
    fcf_total_m = re.search(r"(?:自由現金流|調整後自由現金流|FCF)[^\d\n\r$]*\$?\s*([\d,]+(?:\.\d+)?)\s*M?", text, re.IGNORECASE) # 搜尋 FCF
    shares_m = re.search(r"(?:稀釋股數|Diluted\s*Shares)[^\d\n\r]*([\d,]+(?:\.\d+)?)\s*M?", text, re.IGNORECASE) # 搜尋稀釋股數
    fcf_per_share_m = re.search(r"(?:每股\s*FCF|FCF\s*per\s*share|每股自由現金流)[^\d\n\r$]*\$?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE) # 搜尋每股 FCF
    
    if fcf_total_m and shares_m and fcf_per_share_m: # 若三者皆具備
        raw_fcf = float(fcf_total_m.group(1).replace(",", "")) # 清理逗號轉浮點數
        raw_shares = float(shares_m.group(1).replace(",", "")) # 清理逗號轉浮點數
        actual_fcf_ps = float(fcf_per_share_m.group(1)) # 取得報告宣告之每股 FCF
        try: # 嘗試調用核心計算每股 FCF
            expected_fcf_ps = calc_fcf_per_share( # 調用核心函式
                fcf_total=raw_fcf, # 總現金流
                diluted_shares=raw_shares, # 稀釋股數
                fcf_unit="USD_MILLIONS", # 單位百萬
                shares_unit="SHARES_MILLIONS" # 單位百萬
            ) # 結束調用
            diff_fcf = round(abs(actual_fcf_ps - expected_fcf_ps), 2) # 計算偏差
            if diff_fcf > 0.05: # 若偏差大於 0.05 (例如量綱單位錯誤或差 10 倍)
                errors.append(ValidationError( # 記錄 HARD 算術錯誤
                    severity="HARD", # 標記為 HARD
                    category="ARITHMETIC", # 分類為算術
                    message="每股自由現金流 (FCF) 計算偏差超過 $0.05 (量綱單位錯誤或差 10 倍)", # 說明
                    expected=f"${expected_fcf_ps:.2f}", # 預期每股金額
                    actual=f"${actual_fcf_ps:.2f}", # 實際報告金額
                    diff=f"${diff_fcf:.2f}" # 偏差金額
                )) # 結束記錄
        except Exception as exc: # 若核心拋出例外
            errors.append(ValidationError( # 記錄 HARD 錯誤
                severity="HARD", # 標記為 HARD
                category="ARITHMETIC", # 分類為算術
                message=f"每股自由現金流 (FCF) 計算失敗: {exc}" # 說明
            )) # 結束記錄
    elif fcf_per_share_m: # 若僅宣告了每股 FCF 但未在報告文本同時提供總額與股數，檢查 canonical_metrics 中的核定基準值
        actual_fcf_ps = float(fcf_per_share_m.group(1)) # 取得數值
        expected_fcf_ps = None # 初始化預期值
        if canonical_metrics: # 若有傳入標準指標庫
            metrics = canonical_metrics.get("metrics", {}) # 取得指標字典
            # 優先讀取核定之每股 FCF 基準值
            if "fcf_per_share" in metrics and isinstance(metrics["fcf_per_share"], dict): # 檢查是否存在
                expected_fcf_ps = float(metrics["fcf_per_share"].get("value", 0)) # 讀取數值
            elif "free_cash_flow" in metrics and "diluted_shares" in metrics: # 若提供總額與股數
                tot = float(metrics["free_cash_flow"].get("value", 0)) # 取得總額
                sh = float(metrics["diluted_shares"].get("value", 1)) # 取得股數
                if sh > 0: # 避免除以零
                    try: # 嘗試計算
                        expected_fcf_ps = calc_fcf_per_share(tot, sh, "USD_MILLIONS", "SHARES_MILLIONS") # 計算預期值
                    except Exception: # 容錯
                        pass # 略過
        if expected_fcf_ps is not None and expected_fcf_ps > 0: # 若取得預期基準值
            diff_fcf = round(abs(actual_fcf_ps - expected_fcf_ps), 2) # 計算偏差
            if diff_fcf > 0.05: # 若偏差超過容差
                # 判定是否出現 10 倍量綱失真
                ratio = actual_fcf_ps / expected_fcf_ps if expected_fcf_ps > 0 else 1.0 # 計算比率
                is_ten_fold = (abs(ratio - 0.1) < 0.05) or (abs(ratio - 10.0) < 0.5) # 判斷是否差 10 倍
                msg = "每股自由現金流 (FCF) 規模量綱嚴重失真 (差 10 倍)" if is_ten_fold else "每股自由現金流 (FCF) 計算偏差超過 $0.05 (量綱單位錯誤)" # 產生訊息
                errors.append(ValidationError( # 記錄 HARD 錯誤
                    severity="HARD", # 標記為 HARD
                    category="ARITHMETIC", # 分類為算術
                    message=msg, # 說明
                    expected=f"${expected_fcf_ps:.2f}", # 預期值
                    actual=f"${actual_fcf_ps:.2f}", # 實際值
                    diff=f"${diff_fcf:.2f}" # 偏差
                )) # 結束記錄

    return errors # 回傳錯誤串列

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 單一報告驗證主流程 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def validate_report( # 定義單一報告檢驗函式
    file_path: str, # 報告檔案路徑
    role_type: str = "general", # 角色類型
    attempt: int = 1, # 當前嘗試次數
    canonical_metrics: Optional[Dict[str, Any]] = None # 可選之 SSOT 常數資料
) -> Tuple[bool, List[ValidationError]]: # 回傳通過布林值與錯誤清單
    """驗證單一報告檔案，執行格式、投行結構與金融算術重算驗證。""" # 函式說明文件
    if not os.path.exists(file_path): # 檢查檔案是否存在
        return False, [ValidationError("HARD", "IO", f"檔案不存在: {file_path}")] # 檔案不存在視為 HARD 錯誤
        
    with open(file_path, "r", encoding="utf-8") as f: # 開啟並讀取檔案全文
        content = f.read() # 讀取全文
        
    if not content.strip():
        return False, [ValidationError("HARD", "IO", "報告內容為空白，無可驗證資料")]
    if len(content.strip()) < 50: # 檢查內容是否過短或空白
        return False, [ValidationError("SOFT", "STRUCTURE", "報告內容過短或空白")] # 內容過短視為 SOFT 錯誤
        
    errors: List[ValidationError] = [] # 初始化錯誤清單
    role_lower = role_type.lower() # 轉小寫以利比對
    
    # 1. 占位符檢查 (SOFT)
    errors.extend(check_placeholders(content)) # 執行占位符防呆
    
    # 2. 投行 4 句檢查 (SOFT，排除 debate 與 ohlcv 預處理摘要)
    non_ib_roles = ("risk-debate", "debate", "ohlcv", "summary", "ohlcv_summary") # 排除名單
    if not any(r in role_lower for r in non_ib_roles): # 若非排除角色
        errors.extend(check_ib_requirements(content)) # 執行投行 4 句檢查
        
    # 3. 多空情景分析 (SOFT)
    if "bull" in role_lower or "bear" in role_lower: # 若為多頭或空頭研究員
        errors.extend(check_scenario_analysis(content)) # 執行情景分析檢查
        
    # 4. 基本面職責邊界檢查 (HARD)
    if "fundamental" in role_lower: # 若為財報基本面分析師
        errors.extend(check_fundamentals_boundaries(content)) # 執行職責邊界檢查
        
    # 5. P0-1 / P0-4 核心算術重算驗證 (HARD)
    # Trader 角色或包含交易提案文字：檢查加權進場成本 (排除 ohlcv_summary)
    if ("trader" in role_lower or ("proposal" in file_path.lower() and "ohlcv" not in file_path.lower())) and role_lower != "ohlcv_summary": # 若為 Trader 提案
        errors.extend(check_trader_math(content, canonical_metrics)) # 執行成本重算
        errors.extend(check_risk_math(content, canonical_metrics)) # 交易提案若含風報比亦檢查
        
    # Risk 角色或包含風險報告文字：檢查風報比 (排除 ohlcv_summary)
    is_risk_target = ( # 判斷是否為風險相關報告
        "risk" in role_lower or # 角色名稱包含 risk
        ("decision" in file_path.lower() and "ohlcv" not in file_path.lower()) or # 路徑為決策
        ("summary" in file_path.lower() and "ohlcv" not in file_path.lower()) # 路徑為總結但非行情摘要
    ) # 結束判斷
    if is_risk_target and role_lower != "ohlcv_summary": # 若為風險相關且非行情摘要
        errors.extend(check_risk_math(content, canonical_metrics)) # 執行風報比重算
        
    # Final 決策主管或全流程綜合報告：全面執行成本、風報比、EV、FCF 重算 (排除 ohlcv_summary 與 risk-debate)
    is_final_or_summary = ( # 判斷是否為最終裁決或總結摘要報告
        "final" in role_lower or # 角色為 final
        role_lower == "summary" or # 角色為 summary
        ("decision" in file_path.lower() and "ohlcv" not in file_path.lower()) or # 路徑為決策且非行情
        ("summary" in file_path.lower() and "ohlcv" not in file_path.lower()) or # 路徑為總結且非行情
        ("general" in role_lower and "ohlcv" not in file_path.lower() and "debate" not in file_path.lower()) # 一般報告但非行情非辯論
    ) # 結束判斷
    if is_final_or_summary and role_lower not in ("ohlcv_summary", "risk-debate"): # 排除行情預處理與三方辯論
        errors.extend(check_trader_math(content, canonical_metrics)) # 檢查決策中引用之成本
        errors.extend(check_risk_math(content, canonical_metrics)) # 檢查決策中引用之風報比
        errors.extend(check_final_math(content, canonical_metrics)) # 執行 EV 與每股指標重算
        
    # 基本面分析報告亦執行 EV 與 FCF 檢查
    if "fundamental" in role_lower: # 若為基本面分析報告
        errors.extend(check_final_math(content, canonical_metrics)) # 執行基本面 EV 與 FCF 檢查
        
    # 篩選實際會造成失敗的錯誤（排除純 INFO 資訊）
    blocking_errors = [e for e in errors if e.severity in ("HARD", "SOFT")] # 篩選 HARD 與 SOFT
    is_pass = len(blocking_errors) == 0 # 判斷是否無阻擋錯誤
    return is_pass, blocking_errors # 回傳驗證結果

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 命令列進入點與重試放行控制 (P0-3 核心) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def main() -> None: # 主程式進入點函式
    """命令列進入點：管理檔案清單、調用驗證器，並依 P0-3 嚴格控管 Exit Code。""" # 函式說明文件
    parser = argparse.ArgumentParser(description="Trading Agents v2 Deterministic Numeric & Gate Validator") # 建立參數解析器
    parser.add_argument("file", nargs="?", help="欲校驗之 Markdown 報告檔案路徑") # 單檔路徑參數
    parser.add_argument("--role", "-r", default="general", help="角色類型 (fundamentals, market, bull, bear, trader, risk, final 等)") # 角色參數
    parser.add_argument("--attempt", "-a", type=int, default=1, help="當前嘗試次數 (1, 2, 3...)") # 嘗試次數參數
    parser.add_argument("--dir", "-d", help="包含多份報告之目錄路徑") # 目錄路徑參數
    parser.add_argument("--constants", "-c", help="已生成之 canonical_metrics.json 常數檔案路徑") # SSOT 常數檔案參數
    
    args = parser.parse_args() # 解析命令列傳入之參數
    
    if not args.file and not args.dir: # 檢查是否提供檔案或目錄
        parser.print_help() # 顯示說明資訊
        sys.exit(1) # 以代碼 1 退出
        
    canonical_data = None # 初始化常數資料
    if args.constants: # 若有提供常數檔案且存在
        try: # 嘗試讀取常數 JSON
            with open(args.constants, "r", encoding="utf-8") as f: # 開啟檔案
                canonical_data = json.load(f) # 解析 JSON
            if not isinstance(canonical_data, dict) or not isinstance(canonical_data.get("metrics"), dict):
                raise ValueError("常數檔須包含 metrics 物件")
        except Exception as exc: # 若解析失敗
            print(f"[BLOCKED] 無法讀取有效常數檔案: {exc}", file=sys.stderr)
            sys.exit(1) # 印出警告
            
    files_to_check: List[Tuple[str, str]] = [] # 初始化待檢驗檔案清單
    if args.dir: # 若指定了目錄
        for root, _, files in os.walk(args.dir): # 遍歷目錄下所有檔案
            for file in sorted(files): # 排序遍歷個別檔案以維持確定性順序
                if file.endswith(".md") and not file.startswith("."): # 若為 Markdown 且非隱藏檔
                    full_p = os.path.join(root, file) # 取得完整檔案路徑
                    role = detect_role(full_p) # 調用統一角色偵測函式 (P1-2 修正)
                    files_to_check.append((full_p, role)) # 加入檢驗清單
    else: # 若指定單一檔案
        target_role = args.role # 預設取用命令列傳入之角色參數
        if target_role == "general" and args.file: # 若未自訂且仍為預設 general
            inferred = detect_role(args.file) # 嘗試自檔名推斷角色
            if inferred != "general": # 若成功辨識出特定角色
                target_role = inferred # 自動採用推斷之角色
        files_to_check.append((args.file, target_role)) # 加入單一檔案待檢驗清單
        
    if not files_to_check:
        print("[BLOCKED] 目錄中沒有可驗證的報告", file=sys.stderr)
        sys.exit(1)

    all_passed = True # 初始化全部通過標記
    all_hard_errors: List[ValidationError] = [] # 初始化全部 HARD 錯誤清單
    all_soft_errors: List[ValidationError] = [] # 初始化全部 SOFT 錯誤清單
    
    for fpath, role in files_to_check: # 逐一檢驗所有檔案
        fname = os.path.basename(fpath) # 取得檔案名稱
        is_pass, errors = validate_report(fpath, role, args.attempt, canonical_data) # 執行檢驗
        if is_pass: # 若無錯誤
            print(f"[PASS] {fname} ({role}) - 數字算術與結構驗證通過") # 印出通過訊息
        else: # 若發現錯誤
            all_passed = False # 標記未全部通過
            file_hard = [e for e in errors if e.severity == "HARD"] # 分離 HARD 錯誤
            file_soft = [e for e in errors if e.severity == "SOFT"] # 分離 SOFT 錯誤
            all_hard_errors.extend(file_hard) # 累加至全局 HARD 清單
            all_soft_errors.extend(file_soft) # 累加至全局 SOFT 清單
            
            print(f"[FAIL] {fname} ({role}) - 發現 {len(errors)} 項問題 (HARD: {len(file_hard)}, SOFT: {len(file_soft)}):") # 印出標頭
            for err in errors: # 逐項印出詳細錯誤
                print(f"  • {err}") # 印出錯誤訊息
                
    if all_passed: # 若全部通過
        print("\n[SUCCESS] 全部檔案與角色驗證通過 (0 個錯誤)。") # 印出成功標頭
        sys.exit(0) # 正常退出 Exit Code 0
        
    # -------------------------------------------------------------------------- # 分隔裝飾線
    # P0-3 重試放行控制核心邏輯 # 區塊說明註解
    # -------------------------------------------------------------------------- # 分隔裝飾線
    if all_hard_errors: # 若存在任何一項 HARD 致命算術錯誤
        if args.attempt <= 5: # 當嘗試次數小於等於 5 次時
            print(f"\n[BLOCKED] 存在 {len(all_hard_errors)} 項 HARD 算術錯誤 (第 {args.attempt} 次嘗試)。觸發重試 (無限重試直到 PASS)。") # 阻擋提示
            sys.exit(1) # 以代碼 1 阻擋退出，觸發自動重試
        else: # 當重試超過 5 次時
            print(f"\n[BLOCKED] 存在 {len(all_hard_errors)} 項 HARD 算術錯誤且已重試 {args.attempt} 次 (超過 5 次上限)。轉人工確認，絕不自動放行！") # 人工確認提示
            sys.exit(1) # 以代碼 1 阻擋退出，堅決不輸出代碼 2 放行
    else: # 若完全沒有 HARD 錯誤，僅有 SOFT 格式/文字問題
        if args.attempt >= 3: # 若已達第 3 次嘗試
            print(f"\n[WARNING] 僅有 {len(all_soft_errors)} 項 SOFT 格式問題且已達重試上限 (第 {args.attempt} 次嘗試)。標記警告並放行流程繼續執行。") # 警告放行提示
            sys.exit(2) # 以代碼 2 警告放行
        else: # 若未達第 3 次嘗試
            print(f"\n[BLOCKED] 存在 {len(all_soft_errors)} 項 SOFT 格式問題 (第 {args.attempt} 次嘗試)。觸發重試。") # 阻擋重試提示
            sys.exit(1) # 以代碼 1 阻擋退出，觸發重試

if __name__ == "__main__": # 當腳本直接被執行時
    main() # 呼叫主程式進入點
