# Trading Agent V4

V4 的正式狀態是 Fact／Calc／Evidence／Event 與 Claim DAG（論述相依圖），Markdown 只是衍生報告。
所有自然語言論述，包括 interpretation 與 forecast，都必須先送 LLM（大型語言模型）做一次原子語義驗證。
沒有 regex（正規表示式）、數字字面比對或關鍵字快篩決定誰可以跳過語義驗證。

## 實際執行路徑

```text
python -m v4
  → PipelineRunnerV4.execute
  → OrchestratorV4：沿用 V3 manifest／source adapters／builders／FactStore／CalcStore
  → 四位分析師並行 → bull／bear 並行 → research_manager → trader → risk_manager
  → 每位角色回傳 V4 Claim schema，角色之間傳遞結構化物件
  → prepare_phase6_atomic：全部 Claim → 獨立、短證據脈絡的 LLM 驗證
  → SUSPICIOUS／FAIL → 不同模型深度對抗審查；PASS 不進第二層
  → 精確 Claim ID 的局部修補
  → 原 Claim + dependency descendants（相依後代）失效、重新原子驗證
  → V4 renderer 從正式物件重新產生報告
  → 必須完成 Global LLM Consistency Sweep（全局一致性掃描）
  → finalize 檢查目前輸入版本與驗證紀錄一致，才交付 result.json
```

V3 的 `runner.py`、角色模板、`ValidationService` 與 `reporting.py` 不在 V4 路徑中。
V3 維持原入口與行為，作為 baseline（比較基準）。V4 保留 V3 純財務計算核心，另以明確操作類型與輸入引用重算。
不搬入 `prepare_phase6_revision()` 的 finding 文字反搜、事件文字掃描、或缺章節時的文字分組。
新報告按 author_role 與明確 subsection 排列；本次沒有複製 V3 固定十二章的版型。

## 雲端入口

```sh
python -m v4 --manifest /path/to/manifest.json \
  --author-model "$AUTHOR_MODEL" --verifier-model "$VERIFIER_MODEL" \
  --deep-model "$DEEP_MODEL" --global-model "$GLOBAL_MODEL" \
  --out /path/to/v4-output
```

模型 ID 必須由執行者明確指定，verifier 與 deep 必須不同。憑證只讀取環境變數 `GEMINI_API_KEY`，不搜尋本機憑證檔、不建立金鑰、不啟動本地模型。
內建 adapter（連線介面）只直接連線 Google 固定 HTTPS API，不接受自訂本地網址、不走 agy、不跟隨重新導向，也不使用環境代理伺服器。
協定依據：[Google generateContent 官方文件](https://ai.google.dev/api/generate-content)。
每次實際回應記錄 requested_model、served_model（伺服器有提供時）、response_id 與 usage；請求的模型名稱不等於已證實的後端身分。
同一 API 供應商的不同模型提供模型層級獨立性，不能據此聲稱不同模型家族已驗證。

程式庫亦可注入具 `execution="cloud"`、`identity`、`complete(prompt)` 的既有雲端連線介面。
這是呼叫端的信任界線；V4 不會把任意呼叫介面的自述當成供應商認證。
`prepare_*`／`collect_*` API（程式介面）可供既有 assistant coordinator（助理協調流程）逐步驅動；每次 collect 必須提交完整 JSON。
逐步入口與自動入口都要求非空的模型識別；第一層與深度審查必須不同，缺漏身分時不會發布驗證紀錄。
舊名稱 `prepare_phase6`、`prepare_phase6_verify`、`collect_phase6`、`prepare_phase6_revision` 已重新指向 V4 契約，其回傳形狀不再與 V3 相同。

## Claim 與資料契約

- 必填 `claim_id/text/claim_type/refs/fact_refs/dependencies/author_role/assertion/bindings`。
- ID 必須唯一；相依關係不得循環、缺失；Claim 引用若指向另一 Claim，也必須列在 dependencies。
- assertion 至少有 `kind`。`kind=value` 必須明列 `ref_id/value`；`kind=event` 必須明列 `event_ref/is_aggregate`。
- 每個來源引用恰一個 binding，必填 `ref_id/entity/concept/period/scope/unit/basis`。未知維度填 `unknown`，不得猜測；未知來源維度交由 LLM 判斷證據是否足夠。
- FACT 維度與明確數值直接比較；保留 share_basis、effective_date、source_id、source_hash 等來源資訊。Evidence 或 Calc 沒有的維度不由 Python 從文字補猜。
- trader 恰一條 `assertion.kind=trade_plan` Claim，內含完整 `trade` 物件，與 trader 回覆的 trade 相同。修訂它會重建該方案產生的 Fact／Calc，並使相關驗證失效。
- 事件由 `OrchestratorV4.register_events()` 接受結構化 catalog（目錄）。每筆事件必填 event_id/date/source_ref 與完整維度、value、is_aggregate；子事件以 aggregate_ref 明列所屬合計，不從文字或相近數值猜關係。
- 來源、事件或 Claim 變更後，驗證輸入雜湊不同就不能沿用舊紀錄；來源移除後仍引用它會被封鎖。

## 修訂、全局掃描與 blocked（封鎖）狀態

第一層每項回覆必須有正確 claim_id、verdict 與 findings。PASS 的 findings 必須空；SUSPICIOUS／FAIL 必須解釋問題。
漏回、錯 ID、重複 ID、非法 verdict、過期回覆，都不能通過。深度審查可以推翻第一層的誤報。
確定性驗證失敗不會使該 Claim 跳過第一層 LLM；它會在所有有效 Claim 驗過之後阻止最終交付。
來源或計算本身錯誤時回報 blocked，不允許修稿模型藉改 Python 或來源物件來消除錯誤。

finding 只按明確 claim_id／claim_ids 路由；全局 finding 缺 ID 時由 LLM semantic linker（語義連結器）定位。
連結器必須對每筆 finding 完整回覆，不能空對應、重複索引或引用不存在的 Claim。
無法定位時 blocked，不做文字反搜。修補只接受預先授權 ID，先在副本驗證完整資料契約，再一次發布。
同步更新分析視圖、清除舊證據包，並重驗修改前／後相依圖的後代聯集。

每次成功修補都必須重新完成全局掃描；任何未處理 finding、缺掃描或舊報告雜湊都不能 finalize。
超過修訂上限、雲端失敗、來源錯誤或無法定位，均回傳 blocked 且不附交付報告。
執行輸出 `result.json` 包含報告、sidecar（來源與驗證附檔）、各層驗證紀錄、修訂歷程與雲端呼叫資訊；它是當次結果的唯一交付入口。

## 持久化與重新載入

```sh
python -m v4 --resume /path/to/v4-output/checkpoint.json \
  --author-model "$AUTHOR_MODEL" --verifier-model "$VERIFIER_MODEL" \
  --deep-model "$DEEP_MODEL" --global-model "$GLOBAL_MODEL" \
  --out /path/to/resumed-output
```

checkpoint（檢查點）以暫存檔與原子替換保存正式物件及稽核資訊，不包含憑證。
重新載入會驗證 schema 與 DAG，重建分析視圖；序列化的舊審查紀錄只作稽核，不能當成新執行的通行證。
因此重啟後全部 Claim 與全局掃描都會再驗一次，會有額外雲端費用。原角色生成尚未完成的檢查點會被封鎖，須重新由 manifest 執行角色流程；不會直接進 Phase 6 或交付半份報告。
初始提交、修補、重新載入與最終驗證都共同要求 trader 的唯一 trade_plan，且它必須與衍生交易狀態一致。要撤單請修成 WAIT 方案，不能刪除或改型後留下舊 BUY／SHORT。
V3 的 Markdown 或舊 Claim 缺欄位時，不能自動用文字猜成 V4；必須重新執行 V4 角色流程，或由明確結構化資料完成遷移。

## 驗證與限制

```sh
python -m unittest discover -s v4 -t .
python -m v3.test_invariants
python -m v3.test_p6_dedup
```

測試使用標準函式庫，不需要 pytest、模型權重、雲端憑證或網路。
涵蓋來源匯入至完整九角色與最終報告、非法綁定、ID／DAG、漏驗、第二層範圍、局部修補／後代、語義連結、全局掃描封鎖、交易狀態同步、重載與雲端失敗。
整合測試把 V3 舊證據包、舊驗證器與舊提示詞入口設為一經呼叫即失敗，證明沒有繞回舊流程。

開發驗證環境沒有 `GEMINI_API_KEY`；尚未執行真實雲端模型的投資報告驗收。測試替身證明流程與不變條件，不能證明語義判斷品質、實際延遲、費用、模型可用性或市場資料正確性。
不支援重算的 Calc 操作會留下明確 WARN（警告），不會宣稱已有程式重算證明；LLM 仍收到引用脈絡。
目前角色證據包保留完整結構化資料；大型資料集仍可能需要明確的來源選取或分頁介面，不能以截掉 Claim／Fact 或語義快篩處理。

影響只限 `v4/`，不需要重啟 V3 服務。回滾時停用 `python -m v4` 或回到既有 V3 入口；不要將 V4 checkpoint 當作 V3 狀態讀取。
