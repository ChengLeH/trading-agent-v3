#!/usr/bin/env python3 # 指定腳本使用 Python 3 直譯器執行
# -*- coding: utf-8 -*- # 指定原始碼檔案編碼為 UTF-8
# ============================================================================== # 分隔裝飾線
# 腳本名稱：fin_kernel.py # 標示腳本名稱
# 功能描述：確定性金融計算核心 (Deterministic Financial Kernel) 與 SSOT 產生器 # 標示主要功能說明
# 職責範圍：提供無狀態純數學運算、會計公式防禦、量綱單位鎖定與兩層插槽防呆 # 標示職責規範
# 依據標準：相容 Python 3.9+、全標準庫、嚴格會計型別邊界 # 標示技術規範
# ============================================================================== # 分隔裝飾線
import os # 導入作業系統模組以處理檔案存在性與路徑 # 系統模組
import sys # 導入系統模組以處理程序結束代碼與標準輸入輸出 # 系統模組
import json # 導入 JSON 模組以序列化與反序列化資料 # 資料模組
import re # 導入正則表達式模組以進行文本模式比對與掃描 # 文本模組
import argparse # 導入參數解析模組以支援命令列與子行程呼叫 # CLI 模組
from datetime import datetime, timezone # 導入日期時間模組以生成標準 ISO 時間戳記 # 時間模組
from typing import Union, List, Dict, Tuple, Optional, Any # 導入型別提示標記以相容 Python 3.9 # 型別模組
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 自訂金融例外類別定義 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
class FinancialDefenseError(ValueError): # 定義違反金融安全與會計原則之特定例外類別
    """當輸入資料違反金融原則或企圖以帳面股東權益充當市值時拋出此異常。""" # 類別文件註解
    pass # 繼承 ValueError 之預設行為 # 佔位敘述
class FinancialUnitMismatchError(ValueError): # 定義量綱與單位衝突之特定例外類別
    """當財務數據單位或規模數量級不一致時拋出此異常。""" # 類別文件註解
    pass # 繼承 ValueError 之預設行為 # 佔位敘述
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 核心會計與交易數學無狀態計算函式 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def calc_enterprise_value( # 定義企業價值 (EV) 嚴格計算函式
    market_cap: float, # 必要參數：市場交易總市值 (Market Capitalization)
    total_debt: float, # 必要參數：總有息負債 (Total Debt)
    cash_and_equivalents: float, # 必要參數：現金及約當現金 (Cash & Cash Equivalents)
    short_term_investments: float = 0.0, # 選填參數：短期投資與流動有價證券，預設為 0.0
    **forbidden_kwargs: Any # 防呆捕捉：捕捉任何誤傳的非預期參數
) -> float: # 回傳型別為浮點數之企業價值
    """計算企業價值 EV = 市值 + 總負債 - 現金及投資，嚴格禁止傳入帳面權益。""" # 函式說明文件
    forbidden_keys = [ # 定義嚴格禁止傳入之會計帳面股東權益關鍵字清單
        "book_equity", "stockholders_equity", "shareholders_equity", # 禁止帳面權益英文欄位
        "book_value", "equity", "equity_base", "total_equity" # 禁止帳面價值與權益相關別名
    ] # 結束禁止關鍵字清單定義
    for key in forbidden_keys: # 遍歷所有禁止之會計關鍵字
        if key in forbidden_kwargs: # 檢查是否於額外參數中誤傳了帳面權益欄位
            raise FinancialDefenseError( # 發現帳面權益即強制拋出嚴重金融防禦例外
                f"【嚴重會計錯誤】禁止以帳面權益 '{key}' 充當市值計算企業價值 (EV)！" # 詳細錯誤訊息
                f"企業價值定義必須基於市場流通市值 (Market Cap)。" # 指引正確財務定義
            ) # 結束拋出例外
    if market_cap is None or not isinstance(market_cap, (int, float)): # 驗證市值參數型別是否合法
        raise TypeError("市值 (market_cap) 必須為數值型別 (int 或 float)，不得為空或無效型別。") # 拋出型別錯誤
    if market_cap <= 0.0: # 驗證市值是否為正數
        raise FinancialDefenseError(f"市場交易市值必須為正數，目前傳入值為: {market_cap}") # 拋出數值錯誤
    if total_debt is None or not isinstance(total_debt, (int, float)): # 驗證總負債參數型別是否合法
        raise TypeError("總負債 (total_debt) 必須為數值型別 (int 或 float)。") # 拋出型別錯誤
    if total_debt < 0.0: # 驗證總負債是否非負
        raise FinancialDefenseError(f"總負債不得為負數，目前傳入值為: {total_debt}") # 拋出數值錯誤
    total_cash_liquid = float(cash_and_equivalents) + float(short_term_investments) # 加總流動現金與短期投資
    net_debt = float(total_debt) - total_cash_liquid # 計算淨負債 (Net Debt)
    ev = float(market_cap) + net_debt # 計算企業價值 EV = 市值 + 淨負債
    return round(ev, 4) # 四捨五入至小數點後四位回傳
def parse_price_value(price_spec: Any) -> float: # 定義點位解析函式，將數值或區間轉為中點浮點數
    """解析交易點位：支援單一數值、數值字串或 (低點, 高點) 區間並回傳中點數值。""" # 函式說明文件
    if isinstance(price_spec, (int, float)): # 若為原生數值型別
        return float(price_spec) # 直接轉為浮點數回傳
    if isinstance(price_spec, (list, tuple)): # 若為清單或元組區間表示法
        if len(price_spec) != 2: # 檢查區間元素數量是否為兩個
            raise ValueError(f"點位區間元組必須正好包含 2 個數值，目前為: {price_spec}") # 拋出格式錯誤
        p_low = float(price_spec[0]) # 取得區間低點數值
        p_high = float(price_spec[1]) # 取得區間高點數值
        return round((p_low + p_high) / 2.0, 4) # 計算區間中點平均值回傳
    if isinstance(price_spec, dict): # 若為字典表示法
        if "mid" in price_spec: # 若字典中已明確提供中點
            return float(price_spec["mid"]) # 直接取中點數值
        low_key = next((k for k in ("min", "low", "start") if k in price_spec), None) # 搜尋低點鍵名
        high_key = next((k for k in ("max", "high", "end") if k in price_spec), None) # 搜尋高點鍵名
        if low_key and high_key: # 若成功找到低點與高點鍵名
            return round((float(price_spec[low_key]) + float(price_spec[high_key])) / 2.0, 4) # 計算中點回傳
        raise ValueError(f"點位字典缺少 low/high 鍵名: {price_spec}") # 字典結構不符合預期拋出例外
    if isinstance(price_spec, str): # 若點位以字串形式傳入
        clean_str = price_spec.replace("$", "").replace(",", "").strip() # 清理貨幣符號與千分位逗號
        range_match = re.match(r"^([\d.]+)\s*[-~–—]\s*([\d.]+)$", clean_str) # 正則匹配範圍區間字串
        if range_match: # 若符合區間格式
            p1 = float(range_match.group(1)) # 取得第一段數值
            p2 = float(range_match.group(2)) # 取得第二段數值
            return round((p1 + p2) / 2.0, 4) # 回傳區間中點
        return float(clean_str) # 單一數值字串直接轉浮點數
    raise TypeError(f"無法解析之點位規格型別: {type(price_spec)} -> {price_spec}") # 未知型別拋錯
def calc_weighted_entry_cost(tranches: List[Dict[str, Any]]) -> float: # 定義分批加權進場成本計算函式
    """計算多批次建倉的加權平均成本，自動將區間化為中點並校驗權重合規性。""" # 函式說明文件
    if not tranches or not isinstance(tranches, list): # 驗證批次資料是否為非空串列
        raise ValueError("批次資料 (tranches) 必須為非空的字典清單。") # 拋出參數錯誤
    total_weight = 0.0 # 初始化權重累加器
    weighted_sum = 0.0 # 初始化加權金額累加器
    for idx, item in enumerate(tranches): # 遍歷每一批建倉設定
        if not isinstance(item, dict): # 驗證個別批次是否為字典格式
            raise TypeError(f"批次索引 {idx} 必須為字典型別。") # 拋出型別錯誤
        raw_price = item.get("price", item.get("range", item.get("entry"))) # 取得點位欄位值
        if raw_price is None: # 檢查是否具備點位資訊
            raise ValueError(f"批次索引 {idx} 缺少 price/range/entry 點位欄位。") # 拋出欄位缺失錯誤
        price = parse_price_value(raw_price) # 解析點位為浮點數中點
        raw_weight = item.get("weight", item.get("pct", item.get("portion", item.get("shares")))) # 取得權重欄位值
        if raw_weight is None: # 檢查是否具備權重資訊
            raise ValueError(f"批次索引 {idx} 缺少 weight/pct/portion 權重欄位。") # 拋出欄位缺失錯誤
        weight = float(raw_weight) # 轉為浮點數權重
        if weight <= 0.0: # 驗證權重是否大於 0
            raise ValueError(f"批次索引 {idx} 權重必須大於 0，目前為: {weight}") # 拋出數值錯誤
        total_weight += weight # 累計總權重
        weighted_sum += price * weight # 累計加權金額
    if total_weight <= 0.0: # 檢查總權重是否有效
        raise ValueError("批次總權重必須大於 0。") # 拋出權重錯誤
    calculated_cost = weighted_sum / total_weight # 計算加權平均成本
    return round(calculated_cost, 2) # 四捨五入至小數點後兩位回傳
def calc_risk_reward_ratio( # 定義風報比 (Risk-to-Reward Ratio) 計算函式
    entry_cost: float, # 必要參數：加權進場成本
    stop_loss: float, # 必要參數：硬性停損價格
    target_price: float, # 必要參數：目標獲利價格
    direction: str = "BUY" # 交易方向，預設為 BUY 做多
) -> float: # 回傳浮點數風報比
    """計算交易之風險報酬比，嚴格防禦無效停損與方向倒置。""" # 函式說明文件
    dir_upper = direction.strip().upper() # 轉大寫清理方向字串
    if dir_upper not in ("BUY", "LONG"): # 目前本核心僅嚴格支援做多交易風報比驗證
        raise NotImplementedError(f"目前核心風報比僅支援做多 (BUY/LONG)，目前為: {direction}") # 拋出未支援例外
    cost_f = float(entry_cost) # 轉換成本為浮點數
    sl_f = float(stop_loss) # 轉換停損為浮點數
    tp_f = float(target_price) # 轉換目標價為浮點數
    if sl_f >= cost_f: # 檢查做多時停損是否低於進場成本
        raise FinancialDefenseError( # 停損不合法強制拋錯
            f"【風控邏輯錯誤】做多交易停損價 ({sl_f}) 必須低於進場成本 ({cost_f})！" # 錯誤訊息說明
        ) # 結束拋出
    if tp_f <= cost_f: # 檢查做多時目標價是否高於進場成本
        raise FinancialDefenseError( # 目標價不合法強制拋錯
            f"【風控邏輯錯誤】做多交易目標獲利價 ({tp_f}) 必須高於進場成本 ({cost_f})！" # 錯誤訊息說明
        ) # 結束拋出
    risk_distance = cost_f - sl_f # 計算每股承受風險點數
    reward_distance = tp_f - cost_f # 計算每股預期報酬點數
    if risk_distance <= 1e-6: # 防範除以零浮點數極小值
        raise FinancialDefenseError("每股風險距離過小或為零，無法計算風報比。") # 拋出數學錯誤
    rr_ratio = reward_distance / risk_distance # 計算風報比倍數
    return round(rr_ratio, 2) # 四捨五入至小數點後兩位回傳
def calc_fcf_per_share( # 定義每股自由現金流計算函式
    fcf_total: float, # 必要參數：總自由現金流金額
    diluted_shares: float, # 必要參數：稀釋加權平均股數
    fcf_unit: str = "USD_MILLIONS", # 總現金流單位，預設為百萬美元
    shares_unit: str = "SHARES_MILLIONS" # 股數單位，預設為百萬股
) -> float: # 回傳每股現金流浮點數
    """計算每股自由現金流，強制統一量綱單位消除 10 倍錯誤。""" # 函式說明文件
    if diluted_shares is None or float(diluted_shares) <= 0.0: # 驗證稀釋股數是否為有效正數
        raise FinancialDefenseError(f"稀釋股數必須為正數，目前傳入值: {diluted_shares}") # 拋出參數錯誤
    fcf_val = float(fcf_total) # 轉換現金流為浮點數
    shares_val = float(diluted_shares) # 轉換股數為浮點數
    if fcf_unit == "USD_MILLIONS" and shares_unit == "SHARES_MILLIONS": # 若兩者皆為百萬規模
        per_share = fcf_val / shares_val # 百萬與百萬相除直接為每股美元金額
    elif fcf_unit == "USD" and shares_unit == "SHARES": # 若兩者皆為基礎單位
        per_share = fcf_val / shares_val # 直接相除得每股美元金額
    elif fcf_unit == "USD_BILLIONS" and shares_unit == "SHARES_MILLIONS": # 若現金流為十億且股數為百萬
        per_share = (fcf_val * 1000.0) / shares_val # 將十億乘以一千轉換為百萬後相除
    else: # 遭遇非標準量綱組合
        raise FinancialUnitMismatchError( # 拋出量綱不相容例外
            f"不支援的單位組合: fcf_unit='{fcf_unit}', shares_unit='{shares_unit}'" # 說明不支援組合
        ) # 結束拋出
    return round(per_share, 2) # 四捨五入至小數點後兩位回傳
# ------------------------------------------------------------------------------ # 分隔裝飾線
# SSOT 資料處理與標準規範輸出 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def create_metric_entry(value: Any, period: str, unit: str, source: str) -> Dict[str, Any]: # 定義建立標準度量項目的輔助函式
    """建立符合 SSOT 規範的度量項目，必備包含 value、period、unit、source 四欄位。""" # 函式說明文件
    return { # 回傳標準字典結構
        "value": value, # 數值本身
        "period": str(period).strip(), # 財務或交易期間元數據
        "unit": str(unit).strip(), # 量綱與單位元數據
        "source": str(source).strip() # 資料來源出處元數據
    } # 結束字典構建
def process_financial_data(raw_data: Dict[str, Any]) -> Dict[str, Any]: # 定義核心金融資料處理與 SSOT 生成函式
    """接收原始 JSON 輸入資料，執行確定性金融計算並產出 canonical_metrics 結構。""" # 函式說明文件
    if not isinstance(raw_data, dict): # 驗證輸入資料是否為字典格式
        raise TypeError("輸入資料 raw_data 必須為字典 (dict) 結構。") # 拋出型別錯誤
    ticker = raw_data.get("ticker", "UNKNOWN").upper() # 取得股票代碼
    period = raw_data.get("period") or "UNKNOWN" # 取得報表期間
    fundamentals_input = raw_data.get("fundamentals", {}) # 取得基本面輸入區塊
    market_input = raw_data.get("market", {}) # 取得市場行情輸入區塊
    trading_input = raw_data.get("trading", {}) # 取得交易策略點位輸入區塊
    metrics_output: Dict[str, Any] = {} # 初始化度量項目字典
    book_equity_keys = ["stockholders_equity", "shareholders_equity", "book_equity", "book_value"] # 帳面權益常見鍵名
    has_book_equity = any(k in fundamentals_input for k in book_equity_keys) # 檢查基本面中是否存在帳面權益
    book_equity_val = None # 初始化帳面權益數值變數
    for k in book_equity_keys: # 尋找帳面權益值
        if k in fundamentals_input: # 若該鍵名存在
            book_equity_val = float(fundamentals_input[k]) # 取得其浮點數值
            break # 取得後中斷搜尋
    market_cap_val = market_input.get("market_cap") # 取得市場行情中的市值
    if market_cap_val is None and "market_cap" in fundamentals_input: # 若基本面中直接提供了市值
        market_cap_val = fundamentals_input["market_cap"] # 採用該市值
    if market_cap_val is None and fundamentals_input.get("use_book_equity_for_ev"): # 若請求以帳面權益計算 EV
        raise FinancialDefenseError( # 強制攔截並報錯
            "【型別介面防禦失敗】嚴禁要求以帳面權益 (Book Equity) 充當市值計算企業價值 (EV)！" # 詳細報錯
        ) # 結束拋出
    if book_equity_val is not None: # 若有提供帳面權益
        metrics_output["stockholders_equity"] = create_metric_entry( # 記錄帳面權益指標
            value=book_equity_val, # 數值
            period=period, # 期間
            unit=fundamentals_input.get("equity_unit", "USD_MILLIONS"), # 單位
            source=fundamentals_input.get("source", "SEC EDGAR 10-K Filings") # 來源出處
        ) # 結束指標記錄
    if market_cap_val is not None: # 若有提供正規市值
        market_cap_f = float(market_cap_val) # 轉為浮點數
        metrics_output["market_cap"] = create_metric_entry( # 記錄市值指標
            value=market_cap_f, # 數值
            period=period, # 期間
            unit=market_input.get("market_cap_unit", "USD_MILLIONS"), # 單位
            source=market_input.get("source", "Market Trading Data") # 來源出處
        ) # 結束指標記錄
        # 缺少負債或現金時保留市值，但不產生虛構的企業價值。
        if all(fundamentals_input.get(key) is not None for key in ("total_debt", "cash_and_equivalents")):
            debt_f = float(fundamentals_input["total_debt"]) # 取得總負債
            cash_f = float(fundamentals_input["cash_and_equivalents"]) # 取得現金
            st_inv_f = float(fundamentals_input.get("short_term_investments", 0.0)) # 取得短期投資
            ev_val = calc_enterprise_value( # 透過嚴格介面計算企業價值
                market_cap=market_cap_f, # 傳入正規市值
                total_debt=debt_f, # 傳入總負債
                cash_and_equivalents=cash_f, # 傳入現金
                short_term_investments=st_inv_f # 傳入短期投資
            ) # 結束 EV 計算
            metrics_output["enterprise_value"] = create_metric_entry( # 記錄企業價值指標
                value=ev_val, # 數值
                period=period, # 期間
                unit=market_input.get("market_cap_unit", "USD_MILLIONS"), # 單位
                source="Kernel Derived: Market Cap + Total Debt - Cash & Investments" # 來源出處
            ) # 結束指標記錄
            ebitda_val = fundamentals_input.get("ebitda") # 取得 EBITDA
            if ebitda_val is not None and float(ebitda_val) > 0.0: # 若 EBITDA 有效
                ev_ebitda = round(ev_val / float(ebitda_val), 2) # 計算 EV/EBITDA 倍數
                metrics_output["ev_to_ebitda"] = create_metric_entry( # 記錄 EV/EBITDA 指標
                    value=ev_ebitda, # 數值
                    period=period, # 期間
                    unit="RATIO", # 單位為倍數
                    source="Kernel Derived: Enterprise Value / EBITDA" # 來源出處
                ) # 結束指標記錄
    fcf_val = fundamentals_input.get("free_cash_flow") # 取得總自由現金流
    diluted_shares = fundamentals_input.get("diluted_shares") # 取得稀釋股數
    if fcf_val is not None: # 若有提供自由現金流
        fcf_unit = fundamentals_input.get("fcf_unit", "USD_MILLIONS") # 取得現金流單位
        metrics_output["free_cash_flow"] = create_metric_entry( # 記錄自由現金流指標
            value=float(fcf_val), # 數值
            period=period, # 期間
            unit=fcf_unit, # 單位
            source=fundamentals_input.get("source", "SEC EDGAR Cash Flow Statement") # 來源出處
        ) # 結束指標記錄
        if diluted_shares is not None: # 若亦有提供稀釋股數
            shares_unit = fundamentals_input.get("shares_unit", "SHARES_MILLIONS") # 取得股數單位
            fcf_ps = calc_fcf_per_share( # 計算每股自由現金流
                fcf_total=float(fcf_val), # 總現金流
                diluted_shares=float(diluted_shares), # 稀釋股數
                fcf_unit=fcf_unit, # 現金流單位
                shares_unit=shares_unit # 股數單位
            ) # 結束計算
            metrics_output["free_cash_flow_per_share"] = create_metric_entry( # 記錄每股自由現金流指標
                value=fcf_ps, # 數值
                period=period, # 期間
                unit="USD_PER_SHARE", # 單位為每股美元
                source=f"{fundamentals_input.get('source', 'SEC')}: FCF / Diluted Shares ({diluted_shares} {shares_unit})" # 來源出處
            ) # 結束指標記錄
    if "revenue" in fundamentals_input: # 若有提供營收
        metrics_output["revenue"] = create_metric_entry( # 記錄營收指標
            value=float(fundamentals_input["revenue"]), # 數值
            period=period, # 期間
            unit=fundamentals_input.get("revenue_unit", "USD_MILLIONS"), # 單位
            source=fundamentals_input.get("source", "SEC EDGAR Income Statement") # 來源出處
        ) # 結束指標記錄
    if "operating_income" in fundamentals_input: # 若有提供營業利益
        metrics_output["operating_income"] = create_metric_entry( # 記錄營業利益指標
            value=float(fundamentals_input["operating_income"]), # 數值
            period=period, # 期間
            unit=fundamentals_input.get("revenue_unit", "USD_MILLIONS"), # 單位
            source=fundamentals_input.get("source", "SEC EDGAR Income Statement") # 來源出處
        ) # 結束指標記錄
    if "net_income" in fundamentals_input: # 若有提供淨利潤
        metrics_output["net_income"] = create_metric_entry( # 記錄淨利潤指標
            value=float(fundamentals_input["net_income"]), # 數值
            period=period, # 期間
            unit=fundamentals_input.get("revenue_unit", "USD_MILLIONS"), # 單位
            source=fundamentals_input.get("source", "SEC EDGAR Income Statement") # 來源出處
        ) # 結束指標記錄
    tranches_data = trading_input.get("tranches") # 取得分批建倉資料
    trade_date = trading_input.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")) # 取得交易設定日期
    if tranches_data: # 若有提供建倉批次
        weighted_cost = calc_weighted_entry_cost(tranches_data) # 計算核定加權成本
        claimed_cost = trading_input.get("claimed_cost") # 取得請求或歷史宣稱之成本
        if claimed_cost is not None: # 若外部傳入舊有宣稱成本
            claimed_float = float(claimed_cost) # 轉為浮點數
            if abs(claimed_float - weighted_cost) > 0.05: # 若與核定計算成本產生脫節
                pass # 核心防禦：由系統核定計算值覆蓋，並記錄來源說明
        metrics_output["weighted_entry_cost"] = create_metric_entry( # 記錄核定加權進場成本
            value=weighted_cost, # 數值
            period=trade_date, # 期間為交易日期
            unit="USD", # 單位為美元
            source=f"Kernel Derived: Weighted Tranches Entry Cost ({len(tranches_data)} Tranches)" # 來源出處
        ) # 結束指標記錄
        stop_loss_val = trading_input.get("stop_loss") # 取得停損設定值
        if stop_loss_val is not None: # 若有提供停損
            sl_f = float(stop_loss_val) # 轉為浮點數
            metrics_output["stop_loss"] = create_metric_entry( # 記錄停損指標
                value=sl_f, # 數值
                period=trade_date, # 期間
                unit="USD", # 單位
                source="Trading Plan Approved Stop Loss" # 來源出處
            ) # 結束指標記錄
            tp1_val = trading_input.get("tp1", trading_input.get("take_profit_1")) # 取得第一目標價
            if tp1_val is not None: # 若有第一目標價
                tp1_f = float(tp1_val) # 轉為浮點數
                rr1 = calc_risk_reward_ratio(weighted_cost, sl_f, tp1_f) # 計算 TP1 風報比
                metrics_output["target_price_1"] = create_metric_entry( # 記錄第一目標價
                    value=tp1_f, period=trade_date, unit="USD", source="Trading Plan Take Profit Target 1" # 屬性
                ) # 結束記錄
                metrics_output["risk_reward_tp1"] = create_metric_entry( # 記錄 TP1 風報比
                    value=rr1, period=trade_date, unit="RATIO", source=f"Kernel Derived: (TP1 {tp1_f} - Cost {weighted_cost}) / (Cost {weighted_cost} - SL {sl_f})" # 屬性
                ) # 結束記錄
            tp2_val = trading_input.get("tp2", trading_input.get("take_profit_2")) # 取得第二目標價
            if tp2_val is not None: # 若有第二目標價
                tp2_f = float(tp2_val) # 轉為浮點數
                rr2 = calc_risk_reward_ratio(weighted_cost, sl_f, tp2_f) # 計算 TP2 風報比
                metrics_output["target_price_2"] = create_metric_entry( # 記錄第二目標價
                    value=tp2_f, period=trade_date, unit="USD", source="Trading Plan Take Profit Target 2" # 屬性
                ) # 結束記錄
                metrics_output["risk_reward_tp2"] = create_metric_entry( # 記錄 TP2 風報比
                    value=rr2, period=trade_date, unit="RATIO", source=f"Kernel Derived: (TP2 {tp2_f} - Cost {weighted_cost}) / (Cost {weighted_cost} - SL {sl_f})" # 屬性
                ) # 結束記錄
    constants_block = generate_system_verified_constants_block(metrics_output, ticker=ticker) # 生成系統常數防呆注入區塊
    canonical_document = { # 組裝標準 canonical_metrics.json 格式文件
        "_metadata": { # 頂層元數據區塊
            "ticker": ticker, # 標的代碼
            "period": period, # 報告期間
            "generated_at": datetime.now(timezone.utc).isoformat(), # 生成時間戳記
            "schema_version": "2.0-ssot", # 規範版本
            "status": "VERIFIED_CANONICAL" # 驗證狀態
        }, # 結束頂層元數據
        "metrics": metrics_output, # 度量數據字典
        "system_verified_constants_block": constants_block # 預先渲染之常數文字區塊
    } # 結束文檔構建
    return canonical_document # 回傳標準字典
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 插槽防呆第一層：常數文字區塊渲染器 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def generate_system_verified_constants_block(metrics: Dict[str, Any], ticker: str = "ASSET") -> str: # 定義常數區塊生成函式
    """將已核准之度量指標轉換為 [SYSTEM VERIFIED CONSTANTS] 注入區塊文字。""" # 函式說明文件
    lines = [ # 初始化輸出字串行串列
        "[SYSTEM VERIFIED CONSTANTS - DO NOT RECALCULATE]", # 系統常數區塊起始標籤
        f"<!-- 標的代碼: {ticker} | 系統核定唯一事實來源 (SSOT) | LLM 禁止自行計算衍生數值，所有數值必須 100% 複製下方核定值 -->" # 系統指示
    ] # 結束初始串列定義
    if "weighted_entry_cost" in metrics: # 若包含加權進場成本
        cost_val = metrics["weighted_entry_cost"]["value"] # 取得成本數值
        lines.append(f"- 核定加權進場成本: ${cost_val:.2f} (禁止沿用舊提案過期點位)") # 格式化成本文字
    if "stop_loss" in metrics: # 若包含停損
        sl_val = metrics["stop_loss"]["value"] # 取得停損數值
        lines.append(f"- 核定硬性停損價: ${sl_val:.2f}") # 格式化停損文字
    if "target_price_1" in metrics and "risk_reward_tp1" in metrics: # 若包含第一目標價與風報比
        tp1_val = metrics["target_price_1"]["value"] # 取得 TP1 數值
        rr1_val = metrics["risk_reward_tp1"]["value"] # 取得 TP1 風報比
        note = " (注意：低於 1:2 門檻，必須在報告中說明折衷理由)" if rr1_val < 2.0 else "" # 補充門檻提示
        lines.append(f"- 核定第一目標價 (TP1): ${tp1_val:.2f} | 核定第一目標風報比: 1:{rr1_val:.2f}{note}") # 格式化文字
    if "target_price_2" in metrics and "risk_reward_tp2" in metrics: # 若包含第二目標價與風報比
        tp2_val = metrics["target_price_2"]["value"] # 取得 TP2 數值
        rr2_val = metrics["risk_reward_tp2"]["value"] # 取得 TP2 風報比
        lines.append(f"- 核定第二目標價 (TP2): ${tp2_val:.2f} | 核定第二目標風報比: 1:{rr2_val:.2f}") # 格式化文字
    if "enterprise_value" in metrics: # 若包含企業價值
        ev_val = metrics["enterprise_value"]["value"] # 取得 EV 數值
        unit_str = metrics["enterprise_value"]["unit"] # 取得單位
        lines.append(f"- 核定企業價值 (EV): ${ev_val:,.2f} {unit_str} (基於市場交易流通市值，嚴禁使用帳面股東權益)") # 格式化 EV 文字
    if "ev_to_ebitda" in metrics: # 若包含 EV/EBITDA
        ev_ebitda = metrics["ev_to_ebitda"]["value"] # 取得倍數
        lines.append(f"- 核定 EV/EBITDA 估值倍數: {ev_ebitda:.2f}x") # 格式化倍數文字
    if "free_cash_flow" in metrics: # 若包含自由現金流
        fcf_val = metrics["free_cash_flow"]["value"] # 取得現金流數值
        unit_str = metrics["free_cash_flow"]["unit"] # 取得單位
        fcf_line = f"- 核定自由現金流 (FCF): ${fcf_val:,.2f} {unit_str}" # 基礎現金流描述
        if "free_cash_flow_per_share" in metrics: # 若包含每股現金流
            fcf_ps = metrics["free_cash_flow_per_share"]["value"] # 取得每股數值
            fcf_line += f" (每股 FCF: ${fcf_ps:.2f})" # 附加每股說明
        lines.append(fcf_line) # 加入行串列
    lines.append("[END SYSTEM VERIFIED CONSTANTS]") # 系統常數區塊結束標籤
    return "\n".join(lines) # 以換行符號組合回傳完整字串
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 插槽防呆第二層：正則掃描與報告一致性校驗器 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def scan_and_validate_report_text(report_text: str, canonical_data: Dict[str, Any]) -> Tuple[bool, List[str]]: # 定義文本正則校驗函式
    """掃描報告文本中的衍生數字，若與核定常數不符或殘留歷史過期數值，立即打回。""" # 函式說明文件
    errors: List[str] = [] # 初始化錯誤訊息串列
    metrics = canonical_data.get("metrics", {}) # 取得核定度量字典
    if "weighted_entry_cost" in metrics: # 檢查加權進場成本
        expected_cost = float(metrics["weighted_entry_cost"]["value"]) # 取得核定成本數值
        cost_matches = re.findall(r"(?:加權成本|加權進場成本|進場成本|Weighted\s*Cost)[^\d\n\r$]*\$?\s*(\d+(?:\.\d+)?)", report_text, re.IGNORECASE) # 正則抽取文字成本
        for found_str in cost_matches: # 遍歷文本中找到的所有成本數值
            found_val = float(found_str) # 轉為浮點數
            if abs(found_val - expected_cost) > 0.05: # 若與核定值偏差超過容差 (0.05)
                errors.append( # 記錄錯誤說明
                    f"【成本數據脫節】報告中出現加權成本 ${found_val:.2f}，與系統核定成本 ${expected_cost:.2f} 不符！" # 說明不一致
                    f"（請勿殘留舊提案過期點位）" # 提示常見錯誤
                ) # 結束加入錯誤
    ratio_labels = {
        "risk_reward_tp1": r"TP\s*1|第一目標|第一獲利點",
        "risk_reward_tp2": r"TP\s*2|第二目標|第二獲利點",
        "risk_reward_weighted": r"加權目標|加權風報比|整體風報比|weighted(?:\s+target)?",
    }
    available = [key for key in ratio_labels if key in metrics]
    # 每個主張依其局部情境核對，不把 TP2 或加權結果套到 TP1。
    for line in report_text.splitlines():
        for claim in re.finditer(r"1\s*[:：]\s*(\d+(?:\.\d+)?)", line):
            prefix = line[:claim.start()]
            clause = re.split(r"[；;。]", prefix)[-1]
            if re.search(r"(?:門檻|threshold|低於|高於|至少)[^\d]*$", clause, re.IGNORECASE) or re.match(r"\s*(?:門檻|threshold)", line[claim.end():], re.IGNORECASE):
                continue  # 比較門檻不等於目前情境的計算結果。
            labels = [(match.start(), key) for key, pattern in ratio_labels.items()
                      for match in re.finditer(pattern, clause, re.IGNORECASE)]
            if labels:
                key = max(labels)[1]
            elif re.search(r"風險報酬比|風報比|R:R|Risk[/-]Reward", clause, re.IGNORECASE) and len(available) == 1:
                key = available[0]
            else:
                continue  # 未標明情境或門檻的數值不當成特定目標主張。
            if key not in metrics:
                errors.append(f"【風報比缺少依據】{key} 的主張沒有對應核定值")
                continue
            expected = float(metrics[key]["value"])
            actual = float(claim.group(1))
            if abs(actual - expected) > 0.05:
                errors.append(f"【風報比數據脫節】{key}: 報告 1:{actual:.2f}，核定 1:{expected:.2f}")
    equity_ev_pattern = r"(以申報權益基礎推算企業價值|股東權益\s*\$[\d,]+M?\s*加淨現金調整.*EV/EBITDA|權益基礎推算.*企業價值)" # 正則偵測以股東權益算 EV
    if re.search(equity_ev_pattern, report_text): # 若文字中出現以此種荒謬邏輯論述
        errors.append( # 記錄嚴重會計錯誤
            "【重大會計邏輯違規】報告中出現『以申報權益基礎推算企業價值』之錯誤描述！" # 違規說明
            "企業價值 (EV) 嚴禁使用帳面股東權益充當市值推算。" # 指引標準
        ) # 結束加入錯誤
    is_pass = len(errors) == 0 # 判斷是否全數通過
    return is_pass, errors # 回傳通過布林值與錯誤訊息清單
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 命令列與子行程介面實作 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def main() -> None: # 定義 CLI 主程式進入點
    """命令列進入點：支援 --input / --output 檔案轉換與 --verify-report 文本審查。""" # 函式說明文件
    parser = argparse.ArgumentParser(description="Deterministic Financial Kernel (fin_kernel.py)") # 建立參數解析器
    parser.add_argument("--input", "-i", help="原始 raw JSON 資料檔案路徑") # 輸入檔案參數
    parser.add_argument("--output", "-o", help="SSOT canonical_metrics.json 輸出檔案路徑") # 輸出檔案參數
    parser.add_argument("--verify-report", "-v", help="欲校驗之 Markdown 報告檔案路徑") # 報告驗證參數
    parser.add_argument("--constants", "-c", help="已生成之 canonical_metrics.json 常數檔案路徑") # 參考常數檔案參數
    args = parser.parse_args() # 解析命令列傳入之參數
    if args.input: # 若指定了輸入原始資料檔案
        if not os.path.exists(args.input): # 檢查輸入檔案是否存在
            print(f"[ERROR] 輸入檔案不存在: {args.input}", file=sys.stderr) # 印出錯誤訊息至標準錯誤
            sys.exit(1) # 以代碼 1 異常結束
        with open(args.input, "r", encoding="utf-8") as f: # 開啟並讀取輸入檔案
            raw_data = json.load(f) # 解析 JSON 資料
        try: # 嘗試執行金融核心運算
            canonical_result = process_financial_data(raw_data) # 處理並產出 SSOT 資料
        except Exception as exc: # 捕捉任何金融防禦或型別錯誤
            print(f"[BLOCKED] 金融核心運算被攔截: {exc}", file=sys.stderr) # 印出攔截訊息
            sys.exit(1) # 異常退出
        output_str = json.dumps(canonical_result, ensure_ascii=False, indent=2) # 序列化為美化 JSON 字串
        if args.output: # 若有指定輸出檔案路徑
            out_dir = os.path.dirname(args.output) # 取得輸出檔案目錄
            if out_dir: # 若目錄存在指定字串
                os.makedirs(out_dir, exist_ok=True) # 遞迴建立目錄
            with open(args.output, "w", encoding="utf-8") as f: # 開啟並寫入輸出檔案
                f.write(output_str + "\n") # 寫入文字加換行
            print(f"[SUCCESS] 已成功生成 SSOT 檔案: {args.output}") # 印出成功訊息
        else: # 若未指定輸出檔案路徑
            print(output_str) # 直接輸出至標準輸出 (stdout)
        sys.exit(0) # 正常退出
    elif args.verify_report: # 若指定了驗證報告檔案
        if not os.path.exists(args.verify_report): # 檢查報告檔案是否存在
            print(f"[ERROR] 報告檔案不存在: {args.verify_report}", file=sys.stderr) # 印出錯誤訊息
            sys.exit(1) # 異常退出
        if not args.constants or not os.path.exists(args.constants): # 檢查常數檔案是否存在
            print(f"[ERROR] 驗證報告時必須提供有效之 --constants 常數 JSON 檔案！", file=sys.stderr) # 印出錯誤訊息
            sys.exit(1) # 異常退出
        with open(args.verify_report, "r", encoding="utf-8") as f: # 開啟報告檔案
            report_text = f.read() # 讀取全文
        with open(args.constants, "r", encoding="utf-8") as f: # 開啟常數檔案
            canonical_data = json.load(f) # 解析 JSON 常數
        is_pass, errors = scan_and_validate_report_text(report_text, canonical_data) # 執行正則掃描比對
        if is_pass: # 若比對全部通過
            print(f"[PASS] {os.path.basename(args.verify_report)} - 衍生數值與系統核定常數 100% 吻合") # 印出通過訊息
            sys.exit(0) # 正常退出
        else: # 若發現矛盾或舊數值
            print(f"[FAIL] {os.path.basename(args.verify_report)} - 發現 {len(errors)} 項衍生數值矛盾:") # 印出失敗訊息
            for err in errors: # 逐項印出錯誤
                print(f"  • {err}") # 印出具體問題
            sys.exit(1) # 以代碼 1 阻擋退出
    else: # 未提供必要參數時
        parser.print_help() # 顯示說明資訊
        sys.exit(1) # 退出
if __name__ == "__main__": # 當腳本直接被執行時
    main() # 呼叫主函式
