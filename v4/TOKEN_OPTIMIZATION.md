# Token 與完成摘要操作

這份指南定義兩種精簡回報：U0 是原生工作目錄成本盤點，U1 是工作完成摘要。它們只提供索引與計數；完整 prompt、來源、回覆、審查收據與報告仍保存在既有原始工件中。

## U0：唯讀成本盤點

程式庫入口：

```python
from v4.token_metrics import audit_native_work

metrics = audit_native_work("/path/to/native-work")
```

盤點支援舊版 inline `v4-native-work/1`，以及 cold archive 格式 `v4-native-work/2`。`/2` 封存只在記憶體中還原；盤點不開啟會寫入 lock 或回存狀態的 workflow transaction。

輸出按 packet `kind` 計算 packet、item、canonical item prompt 數量與 UTF-8 `prompt_bytes`。既有 `prompt_bytes` 欄位保留其相容語義：只加總 item prompts。若 packet 有 `wire_requests`，另以 `wire_requests_by_kind` 和 `wire_request_prompt_bytes` 統計已準備的批次 request 數及其中 `prompt` bytes，不和 canonical item prompts 合併。兩者是不同表示層，不能相加宣稱實際模型輸入用量；它們也不含供應商實際 token usage 的證明。

`physical_bytes` 分開列出 `agent-state.json`、`native-archive/`、`responses/archive/` 與 `task-delivery/objects/` 的檔案 bytes 和檔案數。`archive_bytes` 維持只包含原生 cold archive 與 response archive；delivery objects 另列，並納入新的 `state_plus_archives_and_task_delivery_file_bytes` 總數。重複前綴只計算 `NativeArchive` 使用的精確 prompt frame，並標為可去重的前綴 bytes；這不是供應商 prompt cache 命中，也不是 token 節省量。

執行期目前沒有把 provider token usage counters 保存到原生工作工件。因此 `usage_tokens` 的 input、output、total 都回傳 `null`。不可從 bytes 推算 tokens、價格或雲端用量；只有實際記錄的 counters 才能填入。

CLI 入口：

```sh
python -m v4.agent_cli audit-cost --work-dir /path/to/native-work
```

## U1：精簡完成摘要

程式庫入口：

```python
from v4.completion_summary import summarize_completion

# 結果檔的 hash 是磁碟上原始 bytes 的 SHA256；檔案不會被修改。
summary = summarize_completion("/path/to/result.json")

# 只有先由原生 workflow 從原始 response archives 重建的收據，
# 才能作為 accepted_receipts 傳入。
summary = summarize_completion(
    result_object,
    artifact_path="/path/to/result.json",
    accepted_receipts={
        "atomic": runner.receipts,
        "deep": runner.deep_receipts,
        "global": runner.global_receipt,
    },
)
```

摘要只輸出 task／packet／run 識別、完成與失敗計數、介入次數、收據狀態，以及結果工件路徑和 SHA256。沒有明確數值時保留 `null`。若使用 `--result PATH`，輸入中的完成計數與 verdict 一律視為未驗證；單獨看到 `status=complete`、`PASS` 或 `accepted_receipts` 欄位不代表原生驗收收據。

要回報已驗收項目，Host 必須載入原生 work-dir，讓 workflow 依原始 response archives 重建收據，再把重建結果提供給摘要函式。不可從任意 response JSON 自行製作 accepted receipts。原始工件仍是完整結果的唯一來源，摘要不可取代或覆寫它。

CLI 入口：

```sh
python -m v4.agent_cli completion-summary --result /path/to/result.json
python -m v4.agent_cli completion-summary --work-dir /path/to/native-work
```

## Host 與 subagent 交接

1. Host 保存原始 work-dir、packet、prompt、response archive 與 canonical result。精簡 brief 只傳目標、限制、packet ID、item IDs，以及原始工件位置；不可用 brief 摘要取代原始 prompt 或證據。
2. 批次 item 數預設為 4，可依任務調整為 1..16，且仍受既有 item／packet UTF-8 byte budgets 約束。保留原始 `packet_id`、`input_hash` 與 item IDs；不得截短 prompt、證據或結果來符合批次大小。
3. 收到回覆後，先由既有 workflow 驗收並保存完整結果，再產生 U1 摘要。Host 的 `--result PATH` 摘要只作未驗證回報；需要 accepted receipts 時改由 work-dir 路徑重建。
4. Host 回覆 parent 時只送以下欄位，不貼 findings、prompt、來源證據或完整模型回覆：

```json
{
  "task_id": "...",
  "packet_id": "...",
  "completed_count": 0,
  "failed_count": null,
  "intervention_count": null,
  "receipt_status": "unverified",
  "result_artifact": {
    "path": "/path/to/result.json",
    "sha256": "..."
  }
}
```

`receipt_status` 清楚標為 `accepted`、`none` 或 `unverified`。缺少資料時用 `null`，不靠推測補值。Host 與 subagent 都保留完整原始工件，parent 可依路徑和 hash 回查。

## 共用任務與批次操作（U2–U4）

原有 inline 模式相容保留；本機 Host 採用下列入口才能取得精簡交付：

```sh
python -m v4.agent_cli next --work-dir /path/to/native-work --delivery-files
python -m v4.agent_cli claim-reviews --work-dir /path/to/native-work --executor host/reviewer --batch-size 4 --delivery-files
python -m v4.agent_cli read-task --manifest /path/to/manifest --sha256 HASH --item ITEM_ID --action brief
python -m v4.agent_cli read-task --manifest /path/to/manifest --sha256 HASH --item ITEM_ID --action index
python -m v4.agent_cli read-task --manifest /path/to/manifest --sha256 HASH --item ITEM_ID --action query --path '["evidence_catalog","SOURCE_ID"]'
```

回傳的 `tasks` 列出可派發的 item/request ID 和原本明示的 Claim IDs。`brief` 提供共用規則與回覆格式，`query` 使用精確 JSON 路徑讀取原始資料。角色來源可用 `--action role-query --catalog facts --ref SOURCE_ID`，或 `--action index` 取得目錄。派給一個審查員的是整個 bounded request，不另為每條 Claim 開一個完整 Agent。每條仍須回獨立 verdict；用既有 `submit-review-request` 提交原始批次 JSON，由管線逐條驗收，不能把短報當作模型回覆。

`--action materialize` 還原完整原始 packet，包含逐位元組相同的 prompts/schema，適用於需要完整輸入的執行者。AGY 的既有路徑保持自足證據，不傳本機路徑要求它開工具。共享檔案節省磁碟/交付 bytes，不代表模型必然少讀同量 tokens。

Host 需實際採用本指南的批次和短報入口。V4 不控制外部 Slack 接收器或 Host 自動將子代理最終訊息加入對話的方式；它提供可直接使用的精簡資料與回報工具，不會宣稱已改動未在此 repo 內的執行器。

## 提交並接下一包（U5）

`submit --next --delivery-files` 先持久化回覆，再回傳 `submission` 與精簡 `next`。若後續交付失敗，`submission_saved=true` 並指示只重試 next；不要求重答。已收到原始回覆但未完成語義處理時，恢復只確認收件，普通 next 仍會檢查 archive、epoch、完整性與 verdict。這合併 CLI 往返與啟動，並未宣稱每次都只載入一次狀態，或等待時間必定減半。

## 目錄依賴與重驗（U6）

Claim 可選填 `assertion.catalog_scope`：

- `referenced_sources`：命題完全依照明示引用與前提判斷；只有自身與所有祖先都使用此範圍，才採用局部來源指紋。
- `full_catalog`：命題涉及整體目錄、完整性或缺席。目錄內容變更仍會讓該 Claim 及依賴後代重驗。
- 省略：保留舊版目錄 metadata 指紋與收據相容性，不自動替舊 Claim 加上新的宣告。這並不保證舊 Claim 對未引用 Fact 的原地值變更（metadata 不變）都會失效；全局狀態仍使用完整 canonical hash。

宣告不是語義豁免。若命題其實依賴資料缺席，而提供的引用不足以證明，原子、深度與批次審查均要求回 SUSPICIOUS。不要從文句自動猜 scope，也不要手改 checkpoint 擦除收據。既有 Claim 若要改宣告，須透過正式修補及重驗。

## 已完成的離線量測

以保留的 INTC 2026-10-22 舊樣本 55 包 packet views（含 schema，不只 prompt）做無損交付測試：inline JSON 19,596,166 bytes，共用 objects 4,061,990 bytes，檔案儲存下降約 79.27%；55 包皆可還原。這不是目前股票 run 的 token 用量、AGY 費用或投資語義品質驗收。真實模型成本與牆鐘效果另行量測。
