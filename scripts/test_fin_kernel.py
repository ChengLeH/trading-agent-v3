#!/usr/bin/env python3 # 指定腳本使用 Python 3 直譯器執行
# -*- coding: utf-8 -*- # 指定原始碼檔案編碼為 UTF-8
# ============================================================================== # 分隔裝飾線
# 腳本名稱：test_fin_kernel.py # 標示腳本名稱
# 功能描述：確定性金融計算核心 (fin_kernel.py) 綜合單元與邊界整合測試套件 # 標示測試套件說明
# 覆蓋範圍：MU 歷史錯誤案例、型別防禦邊界、SSOT 元數據三要素、兩層插槽防呆與 CLI # 標示測試覆蓋範圍
# 依據標準：相容 Python 3.9+、每行皆附中文註解、自動化斷言驗證 # 標示技術規範
# ============================================================================== # 分隔裝飾線
import os # 導入作業系統模組以處理檔案與路徑 # 系統模組
import sys # 導入系統模組以退出程式並管理模組搜尋路徑 # 系統模組
import json # 導入 JSON 模組以解析與生成測試資料 # 資料模組
import tempfile # 導入暫存檔案模組以建立受控測試環境 # 暫存模組
import subprocess # 導入子行程模組以測試 CLI 命令列調用 # 子行程模組
from typing import Dict, Any # 導入型別提示標記以相容 Python 3.9 # 型別模組
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 動態載入待測核心模組 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
current_dir = os.path.dirname(os.path.abspath(__file__)) # 取得當前測試腳本所在目錄
if current_dir not in sys.path: # 檢查是否已在 Python 模組搜尋路徑中
    sys.path.insert(0, current_dir) # 若不在則將當前目錄置於搜尋優先位置
import fin_kernel # 導入被測目標 fin_kernel 模組
from fin_kernel import ( # 自被測模組導入關鍵函式與例外類別
    calc_enterprise_value, # 企業價值嚴格計算函式
    calc_weighted_entry_cost, # 加權進場成本計算函式
    calc_risk_reward_ratio, # 風報比計算函式
    calc_fcf_per_share, # 每股自由現金流計算函式
    process_financial_data, # 核心金融資料處理與 SSOT 生成函式
    scan_and_validate_report_text, # 報告文本一致性正則校驗函式
    FinancialDefenseError, # 金融安全防禦例外類別
    FinancialUnitMismatchError # 金融單位不符例外類別
) # 結束導入
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 測試案例 1：型別介面防禦——傳入帳面權益時強制報錯 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def test_calc_enterprise_value_type_defense() -> None: # 定義 EV 介面型別防禦測試函式
    """驗證 calc_enterprise_value 傳入帳面權益參數時必須明確報錯，嚴禁靜默接受。""" # 測試說明文檔
    print("[TEST 1] 測試 calc_enterprise_value 型別介面防禦...") # 印出測試進度
    caught = False # 初始化異常捕捉標記
    try: # 嘗試傳入帳面權益參數呼叫 EV 計算
        calc_enterprise_value( # 呼叫企業價值計算
            market_cap=1100000.0, # 傳入正常市值
            total_debt=5179.0, # 傳入總負債
            cash_and_equivalents=73453.0, # 傳入現金
            book_equity=138378.0 # 違規傳入帳面權益參數
        ) # 結束呼叫
    except FinancialDefenseError as exc: # 預期捕捉到特定金融防禦異常
        caught = True # 標記成功攔截
        assert "禁止以帳面權益" in str(exc) # 斷言錯誤訊息明確指出禁止帳面權益
    assert caught, "失敗：傳入 book_equity 未拋出 FinancialDefenseError！" # 斷言必須拋錯
    caught_stockholders = False # 初始化股東權益異常標記
    try: # 嘗試傳入 stockholders_equity
        calc_enterprise_value( # 呼叫 EV 計算
            market_cap=1100000.0, # 傳入正常市值
            total_debt=5179.0, # 傳入總負債
            cash_and_equivalents=73453.0, # 傳入現金
            stockholders_equity=138378.0 # 違規傳入股東權益
        ) # 結束呼叫
    except FinancialDefenseError: # 預期捕捉異常
        caught_stockholders = True # 標記成功捕捉
    assert caught_stockholders, "失敗：傳入 stockholders_equity 未拋出例外！" # 斷言必須拋錯
    valid_ev = calc_enterprise_value( # 測試正常合法參數計算
        market_cap=1130000.0, # 正常市值 1.13 兆美元
        total_debt=5179.0, # 總負債 51.79 億美元
        cash_and_equivalents=73453.0 # 現金及投資 734.53 億美元
    ) # 結束呼叫
    expected_ev = 1130000.0 + (5179.0 - 73453.0) # 理論計算 EV = 1130000 - 68274 = 1061726.0
    assert abs(valid_ev - expected_ev) < 1e-4, f"EV 計算不符: {valid_ev} != {expected_ev}" # 斷言數值吻合
    print("  ✓ PASS: calc_enterprise_value 成功阻擋帳面權益參數，正常計算精確。") # 印出通過訊息
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 測試案例 2：MU 歷史點位脫節測試——兩段成本 $964.79 vs 三段核定 $986.00 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def test_mu_weighted_cost_and_rr_recalculation() -> None: # 定義 MU 成本與風報比重算測試函式
    """驗證輸入 MU 數據時，核心能正確依三段進場重算加權成本 $986.00 與風報比 1:1.39。""" # 測試說明文檔
    print("[TEST 2] 測試 MU 兩段成本 ($964.79) 升級為三段成本 ($986.00) 與風報比重算...") # 印出進度
    mu_tranches_2 = [ # 定義 MU 原交易員二段進場點位
        {"range": [985, 1000], "weight": 0.4}, # 第一段：40% @ 中點 $992.5
        {"range": [940, 955], "weight": 0.6} # 第二段：60% @ 中點 $947.5
    ] # 結束二段定義
    cost_2 = calc_weighted_entry_cost(mu_tranches_2) # 計算二段成本
    assert abs(cost_2 - 965.50) < 0.1, f"二段成本應為 $965.50 附近，實際為: {cost_2}" # 驗證原二段計算
    mu_tranches_3 = [ # 定義 MU 決策主管修改後之三段進場點位 (2% + 2% + 1%)
        {"range": [985, 1000], "weight": 2.0}, # 第一段：權重 2 (40%) @ 中點 $992.5
        {"range": [940, 955], "weight": 2.0}, # 第二段：權重 2 (40%) @ 中點 $947.5
        {"price": 1050.0, "weight": 1.0} # 第三段：權重 1 (20%) @ 突破追價 $1050.0
    ] # 結束三段定義
    cost_3 = calc_weighted_entry_cost(mu_tranches_3) # 計算三段核定成本
    assert cost_3 == 986.00, f"三段成本必須為精確 $986.00，目前為: {cost_3}" # 斷言加權成本精確吻合
    stop_loss = 898.0 # 定義硬性停損價
    tp1 = 1108.0 # 定義第一目標獲利價
    tp2 = 1255.0 # 定義第二目標獲利價
    rr_tp1 = calc_risk_reward_ratio(cost_3, stop_loss, tp1) # 計算三段進場之 TP1 風報比
    assert rr_tp1 == 1.39, f"三段進場 TP1 風報比必須為 1:1.39，目前為: {rr_tp1}" # 斷言風報比精確吻合
    rr_tp2 = calc_risk_reward_ratio(cost_3, stop_loss, tp2) # 計算三段進場之 TP2 風報比
    assert rr_tp2 == 3.06, f"三段進場 TP2 風報比必須為 1:3.06，目前為: {rr_tp2}" # 斷言風報比精確吻合
    print("  ✓ PASS: MU 三段進場核定成本 $986.00、TP1 風報比 1:1.39 驗證精確。") # 印出通過訊息
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 測試案例 3：FCF 量綱單位鎖定測試 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def test_fcf_units_and_scaling() -> None: # 定義 FCF 量綱單位測試函式
    """驗證 FCF 百萬單位與每股單位換算正確，異常單位組合主動拋出例外。""" # 測試說明文檔
    print("[TEST 3] 測試 FCF 量綱單位鎖定與換算...") # 印出進度
    mu_fcf_millions = 62308.0 # MU FY26 調整後 FCF 為 62,308 百萬美元
    mu_diluted_shares_millions = 1147.0 # 稀釋股數為 1,147 百萬股
    fcf_ps = calc_fcf_per_share( # 計算每股現金流
        fcf_total=mu_fcf_millions, # 傳入百萬美元
        diluted_shares=mu_diluted_shares_millions, # 傳入百萬股
        fcf_unit="USD_MILLIONS", # 單位
        shares_unit="SHARES_MILLIONS" # 單位
    ) # 結束計算
    expected_fcf_ps = round(62308.0 / 1147.0, 2) # 理論值: 54.32 美元/股
    assert fcf_ps == expected_fcf_ps, f"每股 FCF 應為 {expected_fcf_ps}，實際為: {fcf_ps}" # 斷言吻合
    caught_unit_err = False # 初始化單位異常捕捉標記
    try: # 測試不相容之單位組合
        calc_fcf_per_share( # 呼叫換算
            fcf_total=62308.0, # 數值
            diluted_shares=1147.0, # 數值
            fcf_unit="INVALID_UNIT", # 非法單位
            shares_unit="SHARES_MILLIONS" # 單位
        ) # 結束呼叫
    except FinancialUnitMismatchError: # 預期捕捉單位不符例外
        caught_unit_err = True # 標記成功捕捉
    assert caught_unit_err, "失敗：傳入未知單位未拋出 FinancialUnitMismatchError！" # 斷言拋錯
    print("  ✓ PASS: FCF 單位鎖定 $54.32/股無誤，非相容單位成功攔截。") # 印出通過訊息
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 測試案例 4：SSOT canonical_metrics.json 元數據三要素驗證 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def test_canonical_metrics_metadata_contract() -> None: # 定義 SSOT 元數據三要素契約測試函式
    """驗證輸出的 canonical_metrics.json 中，每一個指標皆嚴格包含期間、單位、來源。""" # 測試說明文檔
    print("[TEST 4] 測試 canonical_metrics.json 元數據三要素 (period, unit, source)...") # 印出進度
    mu_raw_input: Dict[str, Any] = { # 建構模擬 MU 完整原始輸入資料
        "ticker": "MU", # 標的代碼
        "period": "FY2026", # 期間
        "fundamentals": { # 基本面區塊
            "revenue": 133188.0, # 營收 133,188M
            "operating_income": 99340.0, # 營業利益 99,340M
            "net_income": 84969.0, # 淨利 84,969M
            "stockholders_equity": 138378.0, # 帳面股東權益 138,378M
            "total_debt": 5179.0, # 總負債 5,179M
            "cash_and_equivalents": 73453.0, # 現金與投資 73,453M
            "ebitda": 108843.0, # EBITDA 108,843M
            "free_cash_flow": 62308.0, # FCF 62,308M
            "diluted_shares": 1147.0, # 股數 1,147M
            "source": "SEC EDGAR 10-K Filings (2026-10-09)" # 來源
        }, # 結束基本面
        "market": { # 行情區塊
            "market_cap": 1130000.0, # 市場交易市值 1.13 兆美元
            "source": "Market Trading Quote" # 來源
        }, # 結束行情
        "trading": { # 交易策略區塊
            "tranches": [ # 三段進場設定
                {"range": [985, 1000], "weight": 2.0}, # 段一
                {"range": [940, 955], "weight": 2.0}, # 段二
                {"price": 1050.0, "weight": 1.0} # 段三
            ], # 結束三段
            "claimed_cost": 964.79, # 模擬外部傳入之舊宣稱成本
            "stop_loss": 898.0, # 停損
            "tp1": 1108.0, # 目標價一
            "tp2": 1255.0 # 目標價二
        } # 結束交易策略
    } # 結束原始資料定義
    canonical_result = process_financial_data(mu_raw_input) # 呼叫核心處理資料
    assert "_metadata" in canonical_result, "缺少頂層 _metadata 區塊" # 驗證頂層元數據存在
    assert "metrics" in canonical_result, "缺少 metrics 度量區塊" # 驗證度量區塊存在
    assert "system_verified_constants_block" in canonical_result, "缺少常數注入區塊" # 驗證常數區塊存在
    metrics = canonical_result["metrics"] # 取得度量字典
    required_metadata_fields = {"period", "unit", "source", "value"} # 定義必備欄位集合
    for metric_name, entry in metrics.items(): # 逐一檢查每一個度量項目
        for req_field in required_metadata_fields: # 檢查四個必備欄位
            assert req_field in entry, f"指標 '{metric_name}' 缺少必備元數據欄位: '{req_field}'" # 斷言欄位齊全
            assert entry[req_field] is not None, f"指標 '{metric_name}' 的 '{req_field}' 不得為 None" # 斷言非空
    assert metrics["weighted_entry_cost"]["value"] == 986.00 # 驗證成本由核定值 986.00 掌管
    assert metrics["risk_reward_tp1"]["value"] == 1.39 # 驗證風報比由核定值 1.39 掌管
    assert metrics["stockholders_equity"]["value"] == 138378.0 # 驗證帳面權益獨立紀錄
    assert metrics["enterprise_value"]["value"] > 1000000.0 # 驗證 EV 係依據 1.13 兆市值計算
    assert metrics["ev_to_ebitda"]["value"] == 9.75 # 驗證 EV/EBITDA 正確為約 9.75 倍而非 0.64 倍
    print("  ✓ PASS: canonical_metrics.json 元數據三要素 100% 齊備，指標覆蓋無誤。") # 印出通過訊息
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 測試案例 5：插槽防呆第二層正則文本掃描測試 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def test_slot_filling_text_validator() -> None: # 定義正則文本校驗測試函式
    """驗證文本正則掃描函式：精準阻擋舊成本 $964.79 與權益充當市值，放行修正後文本。""" # 測試說明文檔
    print("[TEST 5] 測試插槽防呆文本正則校驗 (scan_and_validate_report_text)...") # 印出進度
    mock_canonical = { # 建立基準核定常數字典
        "metrics": { # 度量區塊
            "weighted_entry_cost": {"value": 986.00, "unit": "USD", "period": "2026-10-13", "source": "Kernel"}, # 核定成本
            "risk_reward_tp1": {"value": 1.39, "unit": "RATIO", "period": "2026-10-13", "source": "Kernel"} # 核定風報比
        } # 結束度量
    } # 結束字典
    stale_text = ( # 模擬 MU final-decision.md 中的殘留舊數文本
        "## 投行核心精要\n"
        "- 終審加權成本 $964.79 對停損 $898，風險報酬比 1:2.1 起跳。\n"
        "- 以申報權益基礎推算企業價值：股東權益 $138,378M 加淨現金調整後...\n"
    ) # 結束舊文本定義
    is_pass, errors = scan_and_validate_report_text(stale_text, mock_canonical) # 執行文字校驗
    assert not is_pass, "失敗：包含舊成本 $964.79 與權益算 EV 之文本竟未被攔截！" # 斷言必須被阻擋
    assert len(errors) >= 2, f"預期攔截至少 2 項問題，實際為: {errors}" # 斷言抓出兩項問題
    print(f"  ✓ 成功攔截舊報告問題 ({len(errors)} 項):") # 印出成功攔截資訊
    for err in errors: # 逐項印出
        print(f"    - {err}") # 印出錯誤項目
    correct_text = ( # 模擬修正後符合核定常數之合法文本
        "## 投行核心精要\n"
        "- 終審加權成本 $986.00 對停損 $898，第一目標風報比 1:1.39 (低於 1:2 門檻，採事件折衷理由)。\n"
        "- 核定企業價值 (EV) 基於市場交易總市值推算。\n"
    ) # 結束合法文本定義
    pass_ok, pass_errors = scan_and_validate_report_text(correct_text, mock_canonical) # 執行校驗
    assert pass_ok, f"失敗：合法文本遭到誤攔截: {pass_errors}" # 斷言必須通過
    print("  ✓ PASS: 正則文字校驗器精準攔截 $964.79、1:2.1 與權益算 EV，放行合法文本。") # 印出通過訊息
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 測試案例 6：CLI 命令列與 Subprocess 調用測試 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def test_cli_subprocess_integration() -> None: # 定義 CLI 命令列子行程整合測試函式
    """驗證 fin_kernel.py 可作為獨立腳本透過 subprocess 調用，支援 --input 與 --output。""" # 測試說明文檔
    print("[TEST 6] 測試 CLI 命令列與 Subprocess 調用...") # 印出進度
    kernel_script = os.path.join(current_dir, "fin_kernel.py") # 取得 fin_kernel.py 絕對路徑
    with tempfile.TemporaryDirectory() as tmp_dir: # 建立暫存測試目錄
        input_file = os.path.join(tmp_dir, "raw_input.json") # 定義暫存輸入檔案路徑
        output_file = os.path.join(tmp_dir, "canonical_metrics.json") # 定義暫存輸出檔案路徑
        test_payload = { # 建立測試資料
            "ticker": "NVDA", # 代碼
            "period": "FY2026", # 期間
            "fundamentals": {"revenue": 30000.0, "total_debt": 1000.0, "cash_and_equivalents": 5000.0}, # 基本面
            "market": {"market_cap": 3000000.0}, # 市值
            "trading": { # 交易
                "tranches": [{"price": 120.0, "weight": 1.0}], # 批次
                "stop_loss": 110.0, # 停損
                "tp1": 140.0 # 目標價
            } # 結束交易
        } # 結束測試資料
        with open(input_file, "w", encoding="utf-8") as f: # 寫入測試輸入檔
            json.dump(test_payload, f, ensure_ascii=False) # 寫入 JSON
        proc = subprocess.run( # 執行 CLI 指令
            [sys.executable, kernel_script, "--input", input_file, "--output", output_file], # 命令列參數
            capture_output=True, # 捕捉輸出
            text=True # 文字模式
        ) # 結束執行
        assert proc.returncode == 0, f"CLI 執行失敗: {proc.stderr}" # 斷言回傳代碼為 0
        assert os.path.exists(output_file), "輸出檔案未成功生成" # 斷言檔案存在
        with open(output_file, "r", encoding="utf-8") as f: # 讀取生成之輸出檔案
            generated_doc = json.load(f) # 解析 JSON
        assert generated_doc["_metadata"]["ticker"] == "NVDA" # 驗證代碼
        assert "weighted_entry_cost" in generated_doc["metrics"] # 驗證度量
        assert generated_doc["metrics"]["weighted_entry_cost"]["value"] == 120.0 # 驗證數值
    print("  ✓ PASS: CLI subprocess 調用轉換檔案運作正常無誤。") # 印出通過訊息
# ------------------------------------------------------------------------------ # 分隔裝飾線
# 主執行函式：依序執行全數測試案例 # 區塊說明註解
# ------------------------------------------------------------------------------ # 分隔裝飾線
def run_all_tests() -> None: # 定義執行所有測試之主進入函式
    """依序執行所有金融核心測試案例，回報整體驗收成果。""" # 函式說明文件
    print("==================================================================") # 分隔線
    print("開始執行確定性金融核心 (fin_kernel.py) 完整驗收測試套件") # 標題
    print("==================================================================") # 分隔線
    test_calc_enterprise_value_type_defense() # 執行測試 1
    test_mu_weighted_cost_and_rr_recalculation() # 執行測試 2
    test_fcf_units_and_scaling() # 執行測試 3
    test_canonical_metrics_metadata_contract() # 執行測試 4
    test_slot_filling_text_validator() # 執行測試 5
    test_cli_subprocess_integration() # 執行測試 6
    print("==================================================================") # 分隔線
    print("【全部通過】fin_kernel.py 6 大項金融驗收標準全數檢驗合格 (PASS)！") # 結尾宣告
    print("==================================================================") # 分隔線
if __name__ == "__main__": # 當腳本直接被執行時
    run_all_tests() # 執行全套測試
