# trading-agents-vm

雲端 VM 版交易分析管線：把 Mac 上 trading-agents-v2 的架構搬到這台 VM，
封裝成我自己的 skill。資料層走 OpenBB（免 key），驗證層沿用 V2 的 Python
算子——**所有數字驗算一律走 Python 程式，不准用腦補**（之後整套要搬回
本地 Hermes，驗證必須是程式碼不是人）。

## 架構

```
trading-agents-vm/
  SKILL.md
  .venv/                  # Python 3.12 venv，裝 openbb
  scripts/
    openbb_data.py        # 資料層：OpenBB keyless（yfinance 股價、sec 基本面）
    fin_kernel.py         # 確定性金融計算核心（V2 移植）
    validate_numbers.py   # 報告數字驗證器（V2 移植）
    cross_file_consistency.py  # 跨檔一致性（V2 移植）
  references/             # 第二階段：analyst prompts（待改寫給 subagents 用）
  bin/                    # CLI 包裝
```

## 資料來源對照（V2 → VM）

| 用途 | V2（Mac） | VM 版 |
|---|---|---|
| OHLCV/股價 | Yahoo chart 直連 | OpenBB `obb.equity.price.historical(provider="yfinance")` |
| 基本面 | SEC EDGAR XBRL 直連 | OpenBB `obb.equity.fundamental.*(provider="sec")` |
| 新聞 | Alpha Vantage NEWS_SENTIMENT（免費 key 被拒）→ web search fallback | `browser.search` news vertical + `social.search` |
| 社群/Reddit | Reddit API（Mac 被擋） | `social.search`（VM 直連） |

## 驗證鐵律

1. 任何報告數字，先過 `fin_kernel.py` / `validate_numbers.py` / `cross_file_consistency.py`。
2. 口徑混用（稅前/稅後）、算術重算、CAGR、OHLCV 規格——四個缺口算子補上前，
   人工用 Python 重算，不准心算。
3. V3（六合一驗證器）規格由使用者出，先在我身上試驗，通過後才裝回 Hermes。

## 執行

```bash
VENV=~/workspace/custom-skills/trading-agents-vm/.venv/bin/python
$VENV scripts/openbb_data.py --ticker AMZN --out /tmp/amzn_data.json
$VENV scripts/fin_kernel.py --help
```
