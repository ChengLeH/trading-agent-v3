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
  → SUSPICIOUS／FAIL → 第二輪深度對抗覆核（可用同一模型）；PASS 不進第二層
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

全部角色與審查統一透過既有 Antigravity `agy` 命令列連線，沿用其登入狀態及環境代理，不需要 `GEMINI_API_KEY`。
模型 ID 由執行者從 `agy models` 清單明確選定，verifier（第一層驗證）與 deep（深度覆核）可使用相同或不同的模型 ID；兩者都是透過 Antigravity 接入。若要同一模型，將 `--verifier-model` 與 `--deep-model` 設為相同值即可。兩層仍分別提出請求、使用不同提示並保存各自的驗證紀錄；同模型兩輪覆核不代表跨模型交叉驗證，也可能重複同一模型的誤判。
程式由 PATH 尋找 `agy`，或使用執行者設定的 `AGY_EXECUTABLE`。不安裝外掛、不建立／讀取金鑰檔、不啟動本地模型；失敗立即封鎖，不切換 direct API（直接 API）或其他後端。
所有 prompt（提示）均以 stdin（標準輸入）傳送完整 `user` 事件，使用 `--input-format stream-json --output-format stream-json`。先收到並檢查啟動資訊，再送出任何提示位元組，不再使用 `-p`。協定依據：[Antigravity 官方 stdin 文件](https://www.antigravity.google/docs/cli/headless/#stream-prompts-from-stdin)。
程式先逐欄驗證內附 `evidence_agent.md` 的結構化設定，再於隔離工作目錄建立每次唯一的 `v4-evidence-only-<uuid>` 代理，透過 `--add-dir` 明列該隔離目錄。設定空工具清單、`excludeDefaultComponents: true`、`inheritCustomizations: false` 與 `commandExecutionPolicy: off`。只有 `init` 回報本次代理、指定模型及正確隔離目錄才送出證據，不退回預設代理。`init.tools` 是 CLI 宣告的全域工具登錄表，不是此代理的有效權限；非空不代表設定失效，也不能用它證明工具已停用。自檢分別回報 `declared_tools`、設定雜湊與 `advertised_tool_count`。依據：[官方代理規格](https://www.antigravity.google/docs/subagents/)、[排除預設工具與自訂設定的更新紀錄](https://www.antigravity.google/docs/changelog?tab=cli)。
角色產生、原子驗證、深度覆核、修補、語義連結及全局掃描使用同一工具邊界，只依輸入物件作業。工具限制由專用代理設定提出，啟動檢查確認代理選取，並不獨立證明 CLI 的內部權限執行。若執行中仍出現工具或子代理事件，立即中止並拒收結果；這是違規偵測，不能保證事件一定先於工具執行。串流須恰有一個成功完整的 `result`，不能把中途片段當成答案。
暫存工作目錄只放代理設定，不保存研究輸入、檢查點或回覆；正式狀態與稽核仍存於 `--out`。不修改全域 `settings.json`，不開放命令、不加入略過權限旗標。保留終端沙箱與停用指令展開，移除在此組合下無效的 `--mode plan`，不呼叫 V3 的 API fallback（失敗轉接）。
超過 64 KiB UTF-8 的長文單次逾時為設定值與 1800 秒的較大者；短文預設也提高至 1200 秒（20 分鐘），可由 V4 入口的 `--request-timeout` 指定；不是 agy 新旗標，內部仍使用 `--print-timeout`，且外層等待上限一致。啟動握手另限最多 60 秒。提高等待上限不會加速模型，只避免 5～10 分鐘的有效請求被 120 秒截斷。空回覆（含純空白）及可重試傳輸失敗最多 3 次（首次＋2 次），間隔 15 秒。已識別的權限拒絕、代理設定不合格及工具誤用不重試；`AntigravityError` 提供安全的 `code/retryable/exit_code/attempts`，不公開原始 stderr 或 exception（例外）文字。非空答案仍須嚴格 JSON，不猜修。
回覆必須是完整 JSON，不能從 prose（一般文字）猜取或修補 JSON。每次成功回應記錄 transport 與 requested_model；文字輸出不提供可靠的實際服務模型身分及用量，因此 served_model 留空，不推測帳單或後端。

程式庫亦可注入具 `execution="cloud"`、`identity`、`complete(prompt)` 的既有雲端連線介面。
若連線介面另提供 `complete_structured(prompt, response_schema)`，原子與深度審查會明確傳入每條 Claim（或批次中逐條項目）的 JSON Schema（結構化回覆規格）；內建 Antigravity 把完整規格放進 stdin 的 `<response-schema>`，不使用 `--json-schema`：Mac agy 1.4.0 實測此旗標會出現多回合及重複串接 JSON，與官方宣告的 `structured_output` 行為不符。程式不從重複文字挑一份、不補預設 PASS；仍嚴格解析與逐條驗收。舊介面仍收到含完整規格的 prompt，回覆一樣須通過嚴格驗收。

可先執行不含模型推論的啟動自檢：

```sh
python -m v4 --check-transport \
  --author-model "$AUTHOR_MODEL" --verifier-model "$VERIFIER_MODEL" \
  --deep-model "$DEEP_MODEL" --global-model "$GLOBAL_MODEL" \
  --out /path/to/v4-capability-check
```

自檢只讀啟動事件，永不送出使用者訊息；成功為 `transport_ready`，不是報告完成。失敗為 `blocked:cloud_policy` 並附安全錯誤分類；不能略過再跑。實際每次請求仍重新檢查。程式庫可用 `client.check_capabilities()`；VM 自訂 driver（驅動程式）必須遵守例外的 `retryable=false`，不要自行包成另一層盲目重試。
這是呼叫端的信任界線；V4 不會把任意呼叫介面的自述當成供應商認證。
`prepare_*`／`collect_*` API（程式介面）可供既有 assistant coordinator（助理協調流程）逐步驅動；每次 collect 必須提交完整 JSON。
逐步入口與自動入口都要求非空的模型識別；第一層與深度審查可用同一模型，但各自的驗證紀錄仍必須完整，缺漏身分時不會發布驗證紀錄。
舊名稱 `prepare_phase6`、`prepare_phase6_verify`、`collect_phase6`、`prepare_phase6_revision` 已重新指向 V4 契約，其回傳形狀不再與 V3 相同。

## P6 效能、可選分批與進度

預設維持每個請求驗證一條 Claim、最多 4 個並行請求。Mac 實測每批 2／4 條及 8 個工作槽均未比單條／4 槽更快，因此不預設啟用；不要從請求數減少推定時間或帳單一定下降。

`--review-batch-size 2` 或 `4` 可選擇分批，`--review-batch-size 1` 回復逐條；`--max-workers` 控制並行請求（1..32），不是每批的 Claim 數。`--review-batch-max-bytes` 預設 65536，計入 UTF-8 提示與完整 schema，限制的是分組目標；單條超過目標仍完整送出並使用長文等待上限，不裁來源或跳過語義驗證。每批來源與相依物件只依明確 ID 去重，同 ID 內容矛盾即拒絕；每條 Claim 的 text、assertion、binding、引用與逐引用判定都保留。

自動入口 `execute_phase6` 會依設定分組原子與深度審查。手動協調流程可用 `prepare_phase6_atomic_batches`／`collect_phase6_atomic_batches`，深度層用 `prepare_phase6_deep_review_batches`／`collect_phase6_deep_review_batches`。prepare 回傳 `review_batches`，各項有 `request_id/claim_ids/prompt/response_schema`；以 request_id 收回 `{results:[逐 Claim 結果]}`。原有逐條 API 與 `atomic_prompts` 仍保留。舊 VM driver 若繼續逐條讀 `atomic_prompts`，不會因更新程式自動變成分批，須改用新介面或自動入口。

同批缺漏或格式不符時，只讓缺漏／不合格 Claim 用獨立請求重新語義審查，最多沿用既有 3 次契約預算；不補 `[]` 或自動 PASS。重複／未知 Claim ID 拒收。PASS 不進 deep；修補仍只重驗受影響 Claim 與 descendants（下游依賴），最後全局掃描不能省略。

P6 的單條與分批路徑都會隨完成順序寫入合格收據與 `checkpoint.json`／`progress.json`，不等 87 條全部回來才保存。`progress.json` 包含 total_claims／atomic_verified／deep_verified、最後更新時間及工作槽設定；每項 response archive 另有 started_at、elapsed_seconds 與成功／失敗狀態。單項出錯會取消未啟動的請求，已在執行的兄弟仍收完保存。最後交付狀態仍以 `result.json` 為準。

新執行會保存分批設定；續跑沿用檢查點，只有明確傳入旗標才覆蓋。沒有分批設定的舊檢查點預設逐條。重新載入後的舊語義收據仍只作稽核，須用新驗證識別碼重新驗證；這個安全契約沒有為省用量而取消。同一 runner 的失敗重試才可略過已合格且輸入雜湊未變的 Claim。

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
兩層審查都另需 `reference_checks`，對 `refs ∪ fact_refs ∪ dependencies` 中每個 ID 恰回一筆，沒有引用時明確回 `[]`。
每筆包含 `ref_id/relation/attributed_assertion/source_evidence/reason`，relation 為 SUPPORTS（支持）、CONTRADICTS（矛盾）或 INSUFFICIENT（證據不足）。
模型必須以原文實際歸因的命題，對照 assertion、binding 與真實來源內容；不能抄正確來源欄位、卻不檢查原文是否把租金負債說成營收。
引用反證時，檢查原文對反證內容的歸因是否正確，不要求每個來源獨立支持整個投資結論。
漏回、重複、未知引用、缺說明都會被拒絕；矛盾必須 FAIL，證據不足不能 PASS。第二輪覆核推翻第一層時，也必須重新交完整逐引用紀錄。
漏回、錯 ID、重複 ID、非法 verdict、過期回覆，都不能通過。深度審查可以推翻第一層的誤報。
原子與深度審查的結構化規格明列四個必要欄位，並固定 Claim ID、引用 ID 集合與逐引用紀錄筆數；即使 schema（規格）合格，程式仍檢查重複引用、覆蓋完整性及 verdict／relation 一致性，不把欄位齊全等同語義正確。
有效 JSON 卻缺欄位或不符合回覆契約時，collector（結果收集器）不再因單筆錯誤丟棄整批：已合格的驗證保存，不合格項不發布驗證紀錄，回傳 `blocked=true`、`needs_retry`、`retry_claim_ids`、`retry_prompts`、逐 Claim 的 `response_schemas` 與 `validation_errors`。
內建執行流程只重新送出不合格／漏回的 Claim，使用完整原始 Claim、來源與相依物件，要求模型重新判斷，不由程式補 `[]` 或沿用舊 PASS。每個 runner（本次執行）、層級、模型與輸入版本最多收集 3 次不合格結果；用盡回 `blocked:atomic_contract` 或 `blocked:deep_contract`，不進後續階段、不附報告。載入檢查點後全部審查重新開始，契約重試預算也重新開始；與連線層的 3 次重試及 Claim 修訂輪次分開計算，可能增加雲端呼叫。
自訂 driver（驅動流程）須在 `needs_retry=true` 時送出 `retry_prompts`，並把對應的 `response_schemas[claim_id]` 傳給支援結構化輸出的介面，再收集回覆；不得把 blocked 視為完成。非 JSON 回覆與過期狀態依原有錯誤處理封鎖。
回覆契約版本為 `v4-semantic-review/2`。每次新執行／重載使用新的驗證識別碼，每次契約修復另有新的請求識別碼。外接快取必須將完整 prompt（含版本與識別碼）、模型與來源脈絡納入鍵，不能只以 Claim ID 命中；內建 journal（執行紀錄）另保存回覆規格雜湊，不重用舊語義驗證。缺少 `reference_checks` 的舊快取只能保留作稽核，不能手工補欄位冒充驗收。
確定性驗證失敗不會使該 Claim 跳過第一層 LLM；它會在所有有效 Claim 驗過之後阻止最終交付。
來源或計算本身錯誤時回報 blocked，不允許修稿模型藉改 Python 或來源物件來消除錯誤。

finding 只按明確 claim_id／claim_ids 路由；全局 finding 缺 ID 時由 LLM semantic linker（語義連結器）定位。
連結器必須對每筆 finding 完整回覆，不能空對應、重複索引或引用不存在的 Claim。
無法定位時 blocked，不做文字反搜。修補只接受預先授權 ID，先在副本驗證完整資料契約，再一次發布。
同步更新分析視圖、清除舊證據包，並重驗修改前／後相依圖的後代聯集。

每次成功修補都必須重新完成全局掃描；任何未處理 finding、缺掃描或舊報告雜湊都不能 finalize。
全局掃描改回 JSON object：`{"findings":[],"dependency_reviews":[...]}`，不能再用單獨的空陣列通過。
`dependency_reviews` 對每條 Claim 恰回一項 `claim_id/required_dependencies/unresolved/reason`，即使無相依也要明列空陣列與語義理由。
模型須對照整份 Claim 目錄與 `history.claim_changes` 的 before／after 原物件，找出真正但未宣告的因果前提；不能以主題相近就猜連線。
程式只檢查模型提供的 ID／完整性／循環，不從文字推斷因果。找到漏邊後只加入明確連線，不自動刪舊連線，經原有局部修補路徑清除該節點及後代的舊驗證，然後重驗並重新全局掃描。
無法確認相依或補邊造成循環時封鎖。若同一全局回覆另有內容問題，補圖後仍保留待處理 finding；缺 ID 時繼續交給語義連結器，不因先修圖而丟失問題。
每筆全局 finding 都由程式依審查輸入與陣列位置指定獨立識別碼；即使文字相同也分別保留，連結與重新載入不會合併不同問題的目標。
超過修訂上限、雲端失敗、來源錯誤或無法定位，均回傳 blocked 且不附交付報告。
命令列輸入或檢查點讀取失敗也會寫入本次 blocked 結果，覆蓋舊成功結果；檢查點保存失敗回報 blocked:checkpoint。
若連結果檔都無法寫入，標準輸出會明列 blocked:output 與 result_written=false，不能把磁碟上的舊結果當成本次交付。
執行輸出 `result.json` 包含報告、sidecar（來源與驗證附檔）、各層驗證紀錄、修訂歷程與雲端呼叫資訊；它是當次結果的唯一交付入口。

## 持久化與重新載入

```sh
python -m v4 --resume /path/to/v4-output/checkpoint.json \
  --author-model "$AUTHOR_MODEL" --verifier-model "$VERIFIER_MODEL" \
  --deep-model "$DEEP_MODEL" --global-model "$GLOBAL_MODEL" \
  --out /path/to/resumed-output
```

checkpoint（檢查點）以暫存檔與原子替換保存正式物件及稽核資訊，不包含憑證。
命令列啟動時先將 `result.json` 標成 `running`／`blocked=true`，避免程序被直接中止時留下舊成功報告。
資料匯入完成、每批角色提交、原子／深度驗證收集、語義連結、局部修補與全局掃描完成時，立即更新 `--out/checkpoint.json`，不等整份流程返回才寫。
每筆模型呼叫開始前與成功返回後，分別將 `in_flight`／`received` 狀態寫入 `--out/responses/`；成功回覆另保留於 `responses/archive/`。並行工作的一筆失敗不會抹去其他已保存回覆。
已分類的 Antigravity 失敗也立即保存 `failed` 與安全的錯誤碼／重試屬性／退出碼；沒有假造回覆，重新執行不會把失敗紀錄或更早的舊答案當成本次成功。
未提交角色的不可重試失敗（含非法 JSON）會在一般續跑時保持封鎖；修正原因後，須明確用 `--retry-role` 指定該角色才重新請求。暫時性傳輸失敗可於新執行續試。這與每次執行都重新審查全部 Claim 的規則分開。
暫存檔只在同一輸出目錄內用於原子替換，檔案與目錄皆同步至磁碟。`--out` 必須由操作者指向持久磁碟或掛載目錄；指定 `/tmp`、非持久 VM 磁碟，或刪除該磁碟後，程式無法保證資料仍在。輸出目錄含研究資料，須按原本權限保存，不能提交到 Git。
重新載入會驗證 schema 與 DAG，重建分析視圖；序列化的舊審查紀錄只作稽核，不能當成新執行的通行證。
`--resume` 會先補齊尚未完成的角色階段，已提交的角色不重新生成；未提交批次的成功回覆只有在 run／角色／模型／完整 prompt 雜湊相同時才可重用，也可從原檢查點旁的 responses 續接到新的輸出目錄。階段次序錯亂或缺半批正式角色仍封鎖，不能跳過缺少的角色交付。
全部 Claim 與全局掃描仍重新驗證，不重播舊語義驗證作為新通行證，因此會有額外雲端用量。若未提交的角色回覆格式錯誤，可明確加入 `--retry-role bull` 等選項，只重呼指定角色；其他同批成功回覆繼續重用，舊嘗試保留作稽核。已提交角色須走原有 Claim patch（論述修補）流程。
保存失敗會停止後續流程；若程序在雲端收到請求後、回覆寫入前中止，該次呼叫是否完成或計費無法確知，續跑可能重送，不宣稱完全不重複呼叫。
初始提交、修補、重新載入與最終驗證都共同要求 trader 的唯一 trade_plan，且它必須與衍生交易狀態一致。要撤單請修成 WAIT 方案，不能刪除或改型後留下舊 BUY／SHORT。
V3 的 Markdown 或舊 Claim 缺欄位時，不能自動用文字猜成 V4；必須重新執行 V4 角色流程，或由明確結構化資料完成遷移。
舊 V4 檢查點若有修訂歷史、卻缺少有效的修改前後 Claim 原物件，也會被拒絕，須由 manifest 重跑；沒有修訂歷史的既有檢查點可載入，但仍必須通過新契約的全部審查。
尚待處理的全局內容 finding 會隨檢查點保存，重新載入不能藉重新驗圖而跳過它。
待處理 finding 缺獨立識別碼或識別碼重複的檢查點會被拒絕，須從有效檢查點或 manifest 重跑。

## 驗證與限制

```sh
python -m unittest discover -s v4 -t .
python -m v3.test_invariants
python -m v3.test_p6_dedup
```

測試使用標準函式庫，不需要 pytest、模型權重、雲端憑證或網路。
涵蓋來源匯入至完整九角色與最終報告、非法綁定、ID／DAG、漏驗、第二層範圍、局部修補／後代、語義連結、全局掃描封鎖、交易狀態同步、重載與雲端失敗。
整合測試把 V3 舊證據包、舊驗證器與舊提示詞入口設為一經呼叫即失敗，證明沒有繞回舊流程。

已在開發環境讀取既有 `agy --help` 與 `agy models`，確認旗標與模型目錄可用；尚未執行真實雲端模型的投資報告驗收。測試替身證明流程與不變條件，不能證明語義判斷品質、實際延遲、費用、模型推論可用性或市場資料正確性。
測試以真正的替身子程序驗證約 189 KB 的 stdin 傳輸與雜湊一致，沒有執行真實模型推論。長文內容不裁切；模型本身的 context（脈絡）容量限制仍由既有錯誤與封鎖流程處理。
不支援重算的 Calc 操作會留下明確 WARN（警告），不會宣稱已有程式重算證明；LLM 仍收到引用脈絡。
目前角色證據包保留完整結構化資料；大型資料集仍可能需要明確的來源選取或分頁介面，不能以截掉 Claim／Fact 或語義快篩處理。

影響只限 `v4/`，不需要重啟 V3 服務。回滾時停用 `python -m v4` 或回到既有 V3 入口；不要將 V4 checkpoint 當作 V3 狀態讀取。
