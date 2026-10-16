#!/usr/bin/env python3 # 指定腳本使用 Python 3 直譯器執行
# -*- coding: utf-8 -*- # 指定原始碼檔案編碼為 UTF-8
# ============================================================================== # 分隔裝飾線
# 腳本名稱：cross_file_consistency.py # 標示腳本名稱
# 功能描述：Phase 2/3/4 跨檔案數字一致性檢驗關卡 (P0-5 核心) # 標示功能
# 檢驗重點：加權目標價、進場成本、停損點位、TP1/TP2/TP3、風報比、倉位上限之跨階段 SSOT 比對 # 標示重點
# 阻擋政策：交易執行核心參數矛盾判 HARD (Exit 1，銷毀收據)；情景預測差異判 INFO (不阻擋) # 標示阻擋政策
# 依據標準：相容 Python 3.9+、全標準庫、每行附中文註解、絕對值微小容差 ($0.05) # 標示技術規範
# ============================================================================== # 分隔裝飾線
import os # 導入作業系統模組以處理檔案路徑與狀態檢查 # 系統模組
import sys # 導入系統模組以管理模組搜尋路徑與 Exit Code # 系統模組
import re # 導入正則表達式模組以解析報告內文與表格 # 正則模組
import json # 導入 JSON 模組以處理結構化輸出 # 資料模組
import argparse # 導入命令列參數解析模組 # CLI 模組
from typing import Dict, List, Optional, Tuple, Any # 導入型別提示標記以相容 Python 3.9 # 型別提示模組

# 將當前腳本所在目錄加入搜尋路徑以利引用同級模組
current_dir = os.path.dirname(os.path.abspath(__file__)) # 取得當前腳本所在絕對目錄
if current_dir not in sys.path: # 檢查是否已在搜尋路徑中
    sys.path.insert(0, current_dir) # 若無則插入至首位

try: # 嘗試導入驗證收據模組
    import validation_receipt # 導入收據防偽管理模組
except ImportError: # 若導入失敗則宣告為 None
    validation_receipt = None # 容錯降級處理

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 審查發現與資料結構定義 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
class ConsistencyFinding: # 定義跨檔案一致性檢查發現類別
    """封裝單一跨檔案一致性檢查發現（包含參數名稱、嚴重等級、兩端檔案與數值偏差）。""" # 類別說明
    def __init__( # 初始化建構式
        self, # 實例指標
        param_name: str, # 參數名稱 (例如 加權目標價, 基準進場, 停損點位 等)
        severity: str, # 嚴重等級：HARD (致命矛盾，阻擋管線) / SOFT (次要建議) / INFO (參考資訊)
        message: str, # 具體錯誤或偏差描述字串
        source_file: str, # 來源檔案名稱 (例如 trader-proposal.md)
        source_value: Any, # 來源檔案中抽取之數值
        target_file: str, # 目標比對檔案名稱 (例如 final-decision.md)
        target_value: Any, # 目標檔案中抽取之數值
        diff: Optional[str] = None # 兩者數值差異說明
    ) -> None: # 建構式無回傳值
        self.param_name = param_name # 設定參數名稱屬性
        self.severity = severity # 設定嚴重等級屬性
        self.message = message # 設定訊息內容屬性
        self.source_file = source_file # 設定來源檔案屬性
        self.source_value = source_value # 設定來源數值屬性
        self.target_file = target_file # 設定目標檔案屬性
        self.target_value = target_value # 設定目標數值屬性
        self.diff = diff # 設定偏差說明屬性

    def __str__(self) -> str: # 定義字串格式化表示法
        parts = [f"[{self.severity}] [{self.param_name}] {self.message}"] # 組合標頭與核心描述
        parts.append(f"{self.source_file}: {self.source_value}") # 記錄來源檔與其數值
        parts.append(f"{self.target_file}: {self.target_value}") # 記錄目標檔與其數值
        if self.diff: # 若有偏差說明
            parts.append(f"偏差: {self.diff}") # 追加偏差字串
        return " | ".join(parts) # 以分隔線連接並回傳

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 數值抽取輔助函式 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def _clean_num(text: str) -> str: # 清除千分位與修飾符輔助函式
    """清除字串中的千分位逗號、貨幣符號與首尾空白。""" # 說明文件
    return text.replace(",", "").replace("$", "").strip() # 替換逗號與美元符號

def _safe_float(text: str) -> Optional[float]: # 安全字串轉浮點數函式
    """自字串開頭或數值區段抽取第一個有效浮點數。""" # 說明文件
    m = re.search(r"[-+]?\d+(?:\.\d+)?", _clean_num(text)) # 正則搜尋數字
    return float(m.group(0)) if m else None # 成功回傳浮點數否則 None

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 結構化摘要區塊解析器 (YAML Frontmatter / YAML Code Block 優先解析) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def extract_structured_frontmatter(text: str) -> Dict[str, Any]: # 定義結構化區塊抽取函式
    """自報告中解析 YAML Frontmatter 或 YAML 代碼塊（全標準庫實現，零第三方相依）。""" # 說明文件
    extracted: Dict[str, Any] = {} # 初始化結果字典
    
    # 支援兩種結構化區塊：
    # 1. 以 --- 包裹的 Frontmatter 區塊 (開頭或結尾)
    # 2. 以 ```yaml ... ``` 包裹的代碼區塊
    raw_blocks: List[str] = [] # 初始化區塊文字串列
    
    # 搜尋所有 --- ... --- 區塊 (開頭、結尾或獨立段落)
    fm_matches = re.findall(r"(?:^|\n)---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.DOTALL) # 正則匹配
    for block in fm_matches: # 遍歷匹配區塊
        raw_blocks.append(block) # 加入清單
        
    # 搜尋所有 ```yaml ... ``` 區塊
    yaml_code_blocks = re.findall(r"```yaml\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE) # 匹配代碼塊
    for block in yaml_code_blocks: # 遍歷匹配
        raw_blocks.append(block) # 加入清單
        
    for block in raw_blocks: # 遍歷所有候選區塊
        for line in block.splitlines(): # 逐行掃描
            line = line.strip() # 去除首尾空白
            if not line or line.startswith("#"): # 略過空行或純註解行
                continue # 換下一行
            if ":" in line: # 檢查是否包含鍵值分隔符冒號
                k, v = line.split(":", 1) # 切分鍵與值
                k_clean = k.strip().lower().replace("_", "").replace("-", "") # 清理鍵名為純小寫
                v_clean = v.strip().split("#")[0].strip() # 去除行尾註解
                
                num_val = _safe_float(v_clean) # 安全轉為浮點數
                if num_val is None: # 若非有效數值
                    continue # 換下一行
                    
                # 欄位標準對應表 (Entry, StopLoss, TP1, TP2, TP3, WeightedTarget, PositionSize)
                if k_clean in ["entry", "entryprice", "suggestedentry", "baseentry"]: # 進場
                    if "entry_price" not in extracted: extracted["entry_price"] = num_val # 記錄進場價
                elif k_clean in ["stoploss", "sl", "stop"]: # 停損
                    if "stop_loss" not in extracted: extracted["stop_loss"] = num_val # 記錄停損價
                elif k_clean in ["tp1", "target1"]: # 第一目標
                    if "tp1" not in extracted: extracted["tp1"] = num_val # 記錄 TP1
                elif k_clean in ["tp2", "target2"]: # 第二目標
                    if "tp2" not in extracted: extracted["tp2"] = num_val # 記錄 TP2
                elif k_clean in ["tp3", "target3"]: # 第三目標
                    if "tp3" not in extracted: extracted["tp3"] = num_val # 記錄 TP3
                elif k_clean in ["weightedtarget", "weightedtp", "expectedtarget"]: # 加權目標
                    if "weighted_target" not in extracted: extracted["weighted_target"] = num_val # 記錄加權目標
                elif k_clean in ["positionsize", "position", "size", "sizing"]: # 倉位
                    if "position_size" not in extracted: extracted["position_size"] = num_val # 記錄倉位
                elif k_clean in ["rrratio", "rr", "riskreward"]: # 風報比
                    if "rr_ratio" not in extracted: extracted["rr_ratio"] = num_val # 記錄風報比
                    
    return extracted # 回傳抽取結果

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 核心交易參數抽取器 (支援結構化區塊優先、表格與內文正則備援) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def extract_execution_parameters(file_path: str, text: str) -> Dict[str, Any]: # 定義執行參數抽取函式
    """自給定報告內容抽取核心交易參數字典（Frontmatter 優先，表格與正文備援）。""" # 說明文件
    base_name = os.path.basename(file_path) # 取得檔案名稱基底
    params: Dict[str, Any] = { # 初始化參數字典
        "file": base_name, # 記錄檔案名稱
        "path": file_path, # 記錄完整路徑
        "entry_price": None, # 建議進場點位 / 混合成本
        "stop_loss": None, # 停損價位
        "tp1": None, # 第一目標價 (TP1)
        "tp2": None, # 第二目標價 (TP2)
        "tp3": None, # 第三目標價 (TP3)
        "weighted_target": None, # 加權目標價 / 加權期望目標
        "rr_ratio": None, # 風險報酬比
        "position_size": None # 建議倉位百分比
    } # 結束初始化字典

    # 1. 【最優先】嘗試由 YAML Frontmatter / 結構化摘要區塊抽取
    structured_params = extract_structured_frontmatter(text) # 解析結構化區塊
    for k, v in structured_params.items(): # 遍歷結構化數值
        if v is not None and k in params: # 若為有效鍵
            params[k] = v # 優先填入

    # 2. 【備援一】由 Markdown 表格列中進行抽取 (僅當對應欄位尚未被結構化區塊設定時填入)
    lines = text.splitlines() # 將全文逐行切分
    for line in lines: # 遍歷每一行文字
        stripped = line.strip() # 去除兩側空白
        if not (stripped.startswith("|") and stripped.endswith("|")): # 檢查是否為表格列
            continue # 非表格列則跳過
        parts = [p.strip() for p in stripped.split("|")[1:-1]] # 切分欄位並去頭尾空字串
        if not parts or parts[0].startswith("---"): # 檢查是否為分隔線列
            continue # 分隔線列跳過
        
        row_key = parts[0] # 取第一欄作為項目鍵名
        row_val = parts[1] if len(parts) > 1 else "" # 取第二欄作為主要數值內容
        row_note = parts[2] if len(parts) > 2 else "" # 取第三欄作為備註或計算公式

        # 2.1 加權目標價專用抽取 (表格備援)
        if params["weighted_target"] is None and any(k in row_key for k in ["加權目標", "加權期望", "機率加權", "Weighted Target"]): # 匹配加權目標鍵名
            val = _safe_float(row_val) # 自數值欄抽取數字
            if val is not None: # 若抽取成功
                params["weighted_target"] = val # 存入加權目標價

        # 2.2 在其他綜合列中搜尋「加權目標 $XXX」標記
        if params["weighted_target"] is None: # 若尚未取得
            wt_m = re.search(r"加權目標[^\$\d]*\$?([0-9,]+(?:\.[0-9]+)?)", row_val + " " + row_note) # 搜尋加權目標數值
            if wt_m: # 若找到
                params["weighted_target"] = _safe_float(wt_m.group(1)) # 存入加權目標價

        # 2.3 進場點位 / 基準進場 / 混合成本 (表格備援)
        if params["entry_price"] is None and any(k in row_key for k in ["進場點位", "建議進場", "基準進場", "混合成本", "進場價", "Entry Price", "進場區間", "進場範圍"]): # 匹配進場鍵名
            # 優先搜尋表格列中是否明確標註 (混合成本基準 $XXX) 或 (加權成本 $XXX)
            cost_spec = re.search(r"(?:加權成本|混合成本|基準成本|加權進場成本|成本基準)[^\$\d]*\$?([0-9,]+(?:\.[0-9]+)?)", row_val + " " + row_note) # 搜尋明確成本標註
            if cost_spec: # 若有明確單一成本基準
                params["entry_price"] = _safe_float(cost_spec.group(1)) # 存入成本價
            else: # 否則檢查是否為進場區間 $A ~ $B
                range_spec = re.search(r"\$?([0-9,]+(?:\.[0-9]+)?)\s*[-~～–—]\s*\$?([0-9,]+(?:\.[0-9]+)?)", row_val) # 搜尋區間兩端
                if range_spec: # 若為區間
                    p1 = _safe_float(range_spec.group(1)) # 取得區間下限
                    p2 = _safe_float(range_spec.group(2)) # 取得區間上限
                    if p1 is not None and p2 is not None: # 若兩者皆有效
                        params["entry_price"] = round((p1 + p2) / 2.0, 2) # 取中點為基準進場價
                else: # 若非區間
                    val = _safe_float(row_val) # 自數值欄抽取單一數值
                    if val is not None and params["entry_price"] is None: # 若有效且未設定
                        params["entry_price"] = val # 存入進場價

        # 1.4 停損點位 (表格)
        if any(k in row_key for k in ["停損點", "停損價", "Stop Loss", "SL"]): # 匹配停損鍵名
            val = _safe_float(row_val) # 自數值欄抽取浮點數
            if val is not None and params["stop_loss"] is None: # 若有效且未設定
                params["stop_loss"] = val # 存入停損價

        # 1.5 目標價階層 (TP1, TP2, TP3) (表格)
        if any(k in row_key for k in ["第一目標", "TP1", "Target 1", "第一獲利點"]): # 匹配 TP1
            val = _safe_float(row_val) # 抽取數值
            if val is not None and params["tp1"] is None: # 若有效
                params["tp1"] = val # 存入 TP1
        if any(k in row_key for k in ["第二目標", "TP2", "Target 2", "第二獲利點"]): # 匹配 TP2
            val = _safe_float(row_val) # 抽取數值
            if val is not None and params["tp2"] is None: # 若有效
                params["tp2"] = val # 存入 TP2
        if any(k in row_key for k in ["第三目標", "TP3", "Target 3", "牛市目標"]): # 匹配 TP3
            val = _safe_float(row_val) # 抽取數值
            if val is not None and params["tp3"] is None: # 若有效
                params["tp3"] = val # 存入 TP3

        # 1.6 風報比 (表格)
        if any(k in row_key for k in ["風險報酬比", "風報比", "R:R", "Risk/Reward"]): # 匹配風報比鍵名
            # 若為複合說明列，優先尋找「加權目標 $XXX 為 1:YY」或整體風報比
            rr_spec = re.search(r"(?:加權目標[^\d]*|整體風報比[^\d]*)1[:：]\s*([0-9]+(?:\.[0-9]+)?)", row_val + " " + row_note) # 搜尋加權風報比
            if rr_spec: # 若找到專屬加權風報比
                params["rr_ratio"] = float(rr_spec.group(1)) # 存入浮點數
            else: # 否則抓取第一個風報比
                rr_gen = re.search(r"1[:：]\s*([0-9]+(?:\.[0-9]+)?)", row_val) # 搜尋一般風報比
                if rr_gen and params["rr_ratio"] is None: # 若找到且未設定
                    params["rr_ratio"] = float(rr_gen.group(1)) # 存入風報比

        # 1.7 建議倉位 (表格)
        if any(k in row_key for k in ["建議倉位", "Position Sizing", "建倉比例", "部位上限"]): # 匹配倉位鍵名
            pos_m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", row_val) # 搜尋百分比
            if pos_m and params["position_size"] is None: # 若找到且未設定
                params["position_size"] = float(pos_m.group(1)) # 存入倉位值

    # 2. 正文散文區正則抽取補齊 (當表格未明確列出時作為備援或交叉核實)
    # 2.1 加權目標價正則抽取
    if params["weighted_target"] is None: # 若加權目標尚未取得
        wt_patterns = [ # 定義加權目標匹配模式清單
            r"(?:機率加權目標|加權目標價|加權目標|Weighted Target)[^\$\d]*\$?([0-9,]+(?:\.[0-9]+)?)", # 模式 1: 標頭加權目標
            r"加權目標＝[^|]*?=\s*\$?([0-9,]+(?:\.[0-9]+)?)" # 模式 2: 公式等於後之數值
        ] # 結束模式清單
        for pat in wt_patterns: # 遍歷模式
            m = re.search(pat, text, re.I) # 執行正則搜尋
            if m: # 若匹配成功
                v = _safe_float(m.group(1)) # 轉為浮點數
                if v is not None: # 若轉換有效
                    params["weighted_target"] = v # 存入加權目標
                    break # 結束迴圈

    # 2.2 基準進場 / 混合成本正則抽取
    if params["entry_price"] is None: # 若進場價尚未取得
        entry_patterns = [ # 定義進場價格匹配清單
            r"(?:基準進場|混合成本|建議進場|加權進場成本|核定進場|Entry Price)[^\$\d]*\$?([0-9,]+(?:\.[0-9]+)?)", # 模式 1
            r"以(?:基準進場|混合成本)\s*\$?([0-9,]+(?:\.[0-9]+)?)\s*計算" # 模式 2
        ] # 結束清單
        for pat in entry_patterns: # 遍歷模式
            m = re.search(pat, text, re.I) # 執行搜尋
            if m: # 若命中
                v = _safe_float(m.group(1)) # 轉換數值
                if v is not None: # 若有效
                    params["entry_price"] = v # 存入進場價
                    break # 中斷搜尋

    # 2.3 停損點位正則抽取
    if params["stop_loss"] is None: # 若停損尚未取得
        sl_patterns = [ # 定義停損匹配清單
            r"(?:停損點|停損價|Stop Loss|SL)[^\$\d]*\$?([0-9,]+(?:\.[0-9]+)?)", # 模式 1
            r"(?:距停損|停損)\s*\$?([0-9,]+(?:\.[0-9]+)?)" # 模式 2
        ] # 結束清單
        for pat in sl_patterns: # 遍歷模式
            m = re.search(pat, text, re.I) # 執行搜尋
            if m: # 若命中
                v = _safe_float(m.group(1)) # 轉換數值
                if v is not None: # 若有效
                    params["stop_loss"] = v # 存入停損價
                    break # 中斷搜尋

    # 2.4 目標價 (TP1, TP2, TP3) 內文抽取
    if params["tp1"] is None: # 若 TP1 未取得
        tp1_m = re.search(r"(?:TP1|第一目標)[^\$\d]*\$?([0-9,]+(?:\.[0-9]+)?)", text, re.I) # 搜尋 TP1
        if tp1_m: # 若命中
            params["tp1"] = _safe_float(tp1_m.group(1)) # 存入 TP1
            
    if params["tp2"] is None: # 若 TP2 未取得
        tp2_m = re.search(r"(?:TP2|第二目標)[^\$\d]*\$?([0-9,]+(?:\.[0-9]+)?)", text, re.I) # 搜尋 TP2
        if tp2_m: # 若命中
            params["tp2"] = _safe_float(tp2_m.group(1)) # 存入 TP2

    if params["tp3"] is None: # 若 TP3 未取得
        tp3_m = re.search(r"(?:TP3|第三目標|牛市目標)[^\$\d]*\$?([0-9,]+(?:\.[0-9]+)?)", text, re.I) # 搜尋 TP3
        if tp3_m: # 若命中
            params["tp3"] = _safe_float(tp3_m.group(1)) # 存入 TP3

    # 2.5 建議倉位正則抽取
    if params["position_size"] is None: # 若倉位尚未取得
        pos_m = re.search(r"(?:建議倉位|Position Sizing|建倉比例)[^\d]*([0-9]+(?:\.[0-9]+)?)\s*%", text, re.I) # 搜尋倉位
        if pos_m: # 若命中
            params["position_size"] = float(pos_m.group(1)) # 存入倉位值

    # 2.6 風報比正則抽取
    if params["rr_ratio"] is None: # 若風報比未取得
        rr_m = re.search(r"(?:加權目標[^\d]*|整體風報比[^\d]*)1[:：]\s*([0-9]+(?:\.[0-9]+)?)", text, re.I) # 搜尋加權風報比
        if rr_m: # 若命中
            params["rr_ratio"] = float(rr_m.group(1)) # 存入風報比

    return params # 回傳抽取之參數字典

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 跨檔案參數比對核心邏輯 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def compare_file_parameters( # 定義雙檔案參數比對函式
    source_params: Dict[str, Any], # 來源檔案參數字典
    target_params: Dict[str, Any], # 目標檔案參數字典
    tolerance: float = 0.05 # 數值絕對值比對容差 (預設 5 美分以內)
) -> List[ConsistencyFinding]: # 回傳比對發現清單
    """比對兩個檔案中抽取的交易核心參數，發現矛盾時記錄 findings。""" # 說明文件
    findings: List[ConsistencyFinding] = [] # 初始化發現項目清單
    src_file = source_params.get("file", "source") # 取得來源檔案名稱
    tgt_file = target_params.get("file", "target") # 取得目標檔案名稱

    # 定義欲比對的核心數值清單與顯示名稱
    metrics_to_compare = [ # 定義指標比較設定清單
        ("weighted_target", "加權目標價", True), # (鍵名, 繁體中文名稱, 是否為 HARD 核心執行參數)
        ("stop_loss", "停損點位", True), # 停損價位為關鍵防線
        ("entry_price", "進場點位 / 成本", True), # 進場成本為算術基準
        ("tp1", "第一目標價 (TP1)", True), # 第一目標價
        ("tp2", "第二目標價 (TP2)", True), # 第二目標價
        ("tp3", "第三目標價 (TP3)", True), # 第三目標價
        ("position_size", "建議倉位上限", True), # 倉位限制
        ("rr_ratio", "風報比 (R:R)", True) # 整體風報比
    ] # 結束比較清單定義

    for key, label, is_hard in metrics_to_compare: # 遍歷所有比對項目
        v_src = source_params.get(key) # 自來源取得數值
        v_tgt = target_params.get(key) # 自目標取得數值

        # 兩者皆有明確數值時才進行比對
        if v_src is not None and v_tgt is not None: # 檢查是否皆存在
            diff = abs(v_src - v_tgt) # 計算絕對差異值
            # 針對風報比與倉位可稍微放寬浮點比較，金額項嚴格限制 0.05
            active_tol = 0.1 if key in ["position_size", "rr_ratio"] else tolerance # 動態設定容差
            
            if diff > active_tol: # 若差異超過容許上限
                severity = "HARD" if is_hard else "INFO" # 判定嚴重等級
                msg = f"跨檔案數值矛盾：{src_file} 與 {tgt_file} 之【{label}】不一致" # 組合錯誤訊息
                diff_str = f"差異 ${diff:.2f}" if "價" in label or "成本" in label or "點位" in label else f"差異 {diff:.2f}" # 格式化差異
                findings.append(ConsistencyFinding( # 記錄一致性發現
                    param_name=label, # 參數名稱
                    severity=severity, # 嚴重等級
                    message=msg, # 錯誤訊息
                    source_file=src_file, # 來源檔案
                    source_value=f"${v_src:.2f}" if "價" in label or "成本" in label or "點位" in label else str(v_src), # 來源值
                    target_file=tgt_file, # 目標檔案
                    target_value=f"${v_tgt:.2f}" if "價" in label or "成本" in label or "點位" in label else str(v_tgt), # 目標值
                    diff=diff_str # 偏差說明
                )) # 結束發現加入

    return findings # 回傳比對發現清單

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 跨檔案完整驗證流程 (支援目錄自動掃描與階段自適應) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def validate_cross_file_consistency( # 定義跨檔案驗證主流程函式
    report_dir: str, # 報告目錄路徑
    files_map: Optional[Dict[str, str]] = None, # 手動傳入檔案路徑字典 (選填)
    phase: Optional[int] = None, # 指定當前階段 (選填，例如 3 或 4)
    auto_invalidate: bool = True # 當發現 HARD 錯誤時是否自動銷毀收據 (預設 True)
) -> Tuple[bool, List[ConsistencyFinding]]: # 回傳 (是否通過, 發現清單)
    """掃描報告目錄下之 Phase 2/3/4 檔案，執行完整跨檔案網狀一致性比對。""" # 說明文件
    all_findings: List[ConsistencyFinding] = [] # 初始化總發現清單
    
    # 標準檔案清單推導
    expected_files = { # 定義標準檔案映射表
        "plan": "investment-plan.md", # Phase 2 投資計畫
        "trader": "trader-proposal.md", # Phase 3 交易提案
        "final": "final-decision.md", # Phase 4 最終裁決
        "summary": "trading-summary.md" # 總結摘要
    } # 結束映射表定義

    # 若未自訂則從 report_dir 中定位現存檔案
    resolved_files: Dict[str, str] = {} # 初始化已解析路徑字典
    for role_key, default_fname in expected_files.items(): # 遍歷標準清單
        if files_map and role_key in files_map and files_map[role_key]: # 若外部有指派
            resolved_files[role_key] = files_map[role_key] # 採用外部指派路徑
        elif report_dir and os.path.isdir(report_dir): # 若目錄有效
            cand_p = os.path.join(report_dir, default_fname) # 構建候選路徑
            if os.path.isfile(cand_p): # 若實體檔案存在
                resolved_files[role_key] = cand_p # 記錄已解析路徑

    # 抽取所有已存在檔案之參數
    extracted: Dict[str, Dict[str, Any]] = {} # 初始化參數儲存字典
    for role_key, file_p in resolved_files.items(): # 遍歷已解析檔案
        try: # 嘗試讀取與解析檔案
            with open(file_p, "r", encoding="utf-8") as f: # 開啟檔案
                content = f.read() # 讀取全文
            extracted[role_key] = extract_execution_parameters(file_p, content) # 抽取核心參數
        except Exception as e: # 若讀取發生錯誤
            print(f"[WARN] 無法讀取或抽取檔案 {file_p}: {e}", file=sys.stderr) # 印出警告

    def missing_finding(role, message, value=None):
        return ConsistencyFinding("資料完整性", "HARD", message,
                                  expected_files.get(role, role), value, "驗證要求", "必要資料")

    # 指定階段要求完整資料；未指定階段仍不得把空目錄當作成功。
    required_roles = {3: ("plan", "trader"), 4: tuple(expected_files)}.get(phase, ())
    if not extracted:
        all_findings.append(missing_finding("目錄", "沒有可讀取的必要報告檔案"))
    for role in required_roles:
        if role not in extracted:
            all_findings.append(missing_finding(role, "目前階段缺少必要報告檔案"))
            continue
        parameters = extracted[role]
        for key in ("entry_price", "stop_loss"):
            if parameters.get(key) is None:
                all_findings.append(missing_finding(role, f"必要執行參數缺值：{key}"))
        if not any(parameters.get(key) is not None for key in ("tp1", "tp2", "tp3", "weighted_target")):
            all_findings.append(missing_finding(role, "必要執行參數缺值：目標價"))
    # 已在任一必要檔案明確指定的可選階梯／倉位／風報比，不得在另檔遺漏。
    for key in ("tp1", "tp2", "tp3", "weighted_target", "position_size", "rr_ratio"):
        if any(extracted.get(role, {}).get(key) is not None for role in required_roles):
            for role in required_roles:
                if role in extracted and extracted[role].get(key) is None:
                    all_findings.append(missing_finding(role, f"跨檔案必要參數缺值：{key}"))

    # 建立需比對的配對關係
    # 配對 1: trader-proposal.md vs final-decision.md (最核心：防範偷換目標價與點位)
    if "trader" in extracted and "final" in extracted: # 若兩者皆存在
        findings = compare_file_parameters(extracted["trader"], extracted["final"]) # 執行比對
        all_findings.extend(findings) # 合併發現

    # 配對 2: final-decision.md vs trading-summary.md (防範風控裁決與一頁式總結脫節)
    if "final" in extracted and "summary" in extracted: # 若兩者皆存在
        findings = compare_file_parameters(extracted["final"], extracted["summary"]) # 執行比對
        all_findings.extend(findings) # 合併發現

    # 配對 3: trader-proposal.md vs trading-summary.md (防範交易員提案與一頁式總結脫節)
    if "trader" in extracted and "summary" in extracted: # 若兩者皆存在
        findings = compare_file_parameters(extracted["trader"], extracted["summary"]) # 執行比對
        all_findings.extend(findings) # 合併發現

    # 配對 4: investment-plan.md vs trader-proposal.md (防範交易員越權偏離研究計畫)
    if "plan" in extracted and "trader" in extracted: # 若兩者皆存在
        findings = compare_file_parameters(extracted["plan"], extracted["trader"], tolerance=0.1) # 執行比對容差稍寬
        all_findings.extend(findings) # 合併發現

    # 統計 HARD 錯誤數量
    hard_errors = [f for f in all_findings if f.severity == "HARD"] # 篩選 HARD 錯誤
    is_pass = len(hard_errors) == 0 # 若無 HARD 錯誤則判定為通過

    # 若未通過且啟用收據聯動，自動銷毀相關違規檔案之收據
    if not is_pass and auto_invalidate and validation_receipt is not None: # 檢查聯動條件
        for f in hard_errors: # 遍歷所有 HARD 錯誤
            # 取得違規兩端的路徑並銷毀收據
            for rk in ["trader", "final", "summary", "plan"]: # 遍歷角色鍵
                if rk in extracted and extracted[rk].get("file") in [f.source_file, f.target_file]: # 匹配檔案
                    p_to_inv = extracted[rk].get("path") # 取得實體路徑
                    if p_to_inv and os.path.exists(p_to_inv): # 檢查檔案存在
                        validation_receipt.invalidate_receipt(p_to_inv) # 執行收據註銷

    return is_pass, all_findings # 回傳驗證結果與清單

# ------------------------------------------------------------------------------ # 分隔裝飾線
# 命令列主進入點 (CLI) # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def main() -> int: # 定義命令列主入口函式
    """處理命令列輸入參數，執行跨檔案比對並以 Exit Code 反映結果。""" # 說明文件
    parser = argparse.ArgumentParser( # 建立命令列解析器
        description="Phase 2/3/4 Cross-File Number Consistency Gate - 跨檔案數字一致性檢驗關卡" # 描述說明
    ) # 結束解析器建立
    parser.add_argument("report_dir", nargs="?", default="", help="報告目錄路徑 (包含各階段 .md 檔案)") # 目錄參數
    parser.add_argument("--plan", default="", help="指定 investment-plan.md 路徑") # 指定計畫
    parser.add_argument("--trader", default="", help="指定 trader-proposal.md 路徑") # 指定交易提案
    parser.add_argument("--final", default="", help="指定 final-decision.md 路徑") # 指定最終決策
    parser.add_argument("--summary", default="", help="指定 trading-summary.md 路徑") # 指定摘要
    parser.add_argument("--phase", type=int, default=None, help="指定當前執行階段 (例如 3 或 4)") # 指定階段
    parser.add_argument("--json", action="store_true", help="以 JSON 格式輸出詳細比對結果") # JSON 輸出標記
    args = parser.parse_args() # 解析輸入參數

    if not args.report_dir and not (args.trader or args.final): # 檢查是否提供足夠路徑資訊
        print("錯誤：必須提供 report_dir 或至少指定 --trader 與 --final 進行比對！", file=sys.stderr) # 印出錯誤訊息
        return 1 # 回傳 Exit Code 1

    files_map = { # 建立自訂檔案映射
        "plan": args.plan, # 計畫路徑
        "trader": args.trader, # 交易提案路徑
        "final": args.final, # 最終決策路徑
        "summary": args.summary # 摘要路徑
    } # 結束映射建立

    print("=== [Cross-File Consistency Gate] 開始跨檔案數字一致性審查 ===") # 印出標頭訊息
    if args.report_dir: # 若有目錄
        print(f"• 報告目錄: {args.report_dir}") # 印出目錄
    if args.phase: # 若有指定階段
        print(f"• 當前階段: Phase {args.phase}") # 印出階段
    print("--------------------------------------------------------------------------------") # 分隔線

    is_pass, findings = validate_cross_file_consistency( # 執行跨檔案驗證核心
        report_dir=args.report_dir, # 傳入目錄
        files_map=files_map, # 傳入檔案映射
        phase=args.phase, # 傳入階段
        auto_invalidate=True # 啟用收據自動失效
    ) # 取得結果

    hard_count = sum(1 for f in findings if f.severity == "HARD") # 計算 HARD 數量
    soft_count = sum(1 for f in findings if f.severity == "SOFT") # 計算 SOFT 數量
    info_count = sum(1 for f in findings if f.severity == "INFO") # 計算 INFO 數量

    if args.json: # 若要求 JSON 格式輸出
        out_dict = { # 建構輸出字典
            "status": "PASS" if is_pass else "BLOCKED", # 狀態標記
            "hard_errors": hard_count, # HARD 計數
            "soft_issues": soft_count, # SOFT 計數
            "findings": [str(f) for f in findings] # 清單轉換
        } # 結束字典建立
        print(json.dumps(out_dict, ensure_ascii=False, indent=2)) # 序列化印出
        return 0 if is_pass else 1
    else: # 標準終端文字輸出
        if is_pass: # 若通過
            print(f"[CROSS-FILE PASS] 跨檔案數字一致性檢驗全數通過！(0 HARD 錯誤, {info_count} 項參考標記)") # 通過訊息
            for f in findings: # 遍歷印出
                print(f"  • {f}") # 印出發現項目
            return 0 # 回傳 Exit Code 0 (PASS)
        else: # 若未通過 (BLOCKED)
            print(f"[CROSS-FILE BLOCKED] 發現 {hard_count} 項跨檔案核心執行參數致命矛盾！打回重做。") # 阻擋訊息
            for f in findings: # 遍歷印出所有發現
                print(f"  • {f}") # 印出詳細內容
            print("[RECEIPT ALERT] 相關檔案之驗證收據已自動註銷，嚴防穿透放行！", file=sys.stderr) # 印出收據註銷警告
            return 1 # 回傳 Exit Code 1 (BLOCKED)

if __name__ == "__main__": # 當腳本直接執行時
    sys.exit(main()) # 執行 main 函式並將回傳碼交給系統
