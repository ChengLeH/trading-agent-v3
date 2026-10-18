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
程式先逐欄驗證內附 `evidence_agent.md` 的結構化設定，再於隔離工作目錄建立每個任務唯一的 `v4-evidence-only-<uuid>` 代理，透過 `--add-dir` 明列該隔離目錄。設定空工具清單、`excludeDefaultComponents: true`、`inheritCustomizations: false` 與 `commandExecutionPolicy: off`。只有 `init` 回報本次代理、指定模型及正確隔離目錄才送出證據，不退回預設代理。`init.tools` 是 CLI 宣告的全域工具登錄表，不是此代理的有效權限；非空不代表設定失效，也不能用它證明工具已停用。自檢分別回報 `declared_tools`、設定雜湊與 `advertised_tool_count`。依據：[官方代理規格](https://www.antigravity.google/docs/subagents/)、[排除預設工具與自訂設定的更新紀錄](https://www.antigravity.google/docs/changelog?tab=cli)。
角色產生、原子驗證、深度覆核、修補、語義連結及全局掃描使用同一工具邊界，只依輸入物件作業。工具限制由專用代理設定提出，啟動檢查確認代理選取，並不獨立證明 CLI 的內部權限執行。若執行中仍出現工具或子代理事件，立即中止並拒收結果；這是違規偵測，不能保證事件一定先於工具執行。串流須恰有一個成功完整的 `result`，不能把中途片段當成答案。
一般模型請求的隔離工作目錄、代理設定、原始請求、對話 ID 與串流事件都持久保存在 `--out/sessions/`；只有不送提示的啟動自檢使用暫存目錄。直接使用 client 時，預設為目前目錄的 `.v4-sessions/`，可透過 `session_root` 或 `V4_SESSION_DIR` 指定持久位置。不修改全域 `settings.json`，不開放命令、不加入略過權限旗標。保留終端沙箱與停用指令展開，移除在此組合下無效的 `--mode plan`，不呼叫 V3 的 API fallback（失敗轉接）。
超過 64 KiB UTF-8 的長文單次逾時為設定值與 1800 秒的較大者；短文預設也提高至 1200 秒（20 分鐘），可由 V4 入口的 `--request-timeout` 指定；不是 agy 新旗標，內部仍使用 `--print-timeout`，且外層等待上限一致。啟動握手另限最多 60 秒。提高等待上限不會加速模型，只避免 5～10 分鐘的有效請求被 120 秒截斷。確認尚未送出任務的可重試啟動失敗預設最多 3 次（首次＋2 次），可由 `--request-attempts` 調整，間隔 15 秒。任務送出後逾時或回空字串則保存並回報 `session_pending`，不在 client 內自動新開對話。已識別的權限拒絕、代理設定不合格及工具誤用不重試；`AntigravityError` 提供安全的 `code/retryable/exit_code/attempts`，不公開原始 stderr 或 exception（例外）文字。非空答案仍須完整 JSON；僅容許整份回覆外包一層 JSON／無語言標記的程式碼框，不猜修內容。
回覆必須是完整 JSON；完整單一程式碼框可在本機移除外框，不增加模型呼叫。不能從 prose（一般文字）猜取、拼接片段或修補 JSON；語義欄位與引用驗收仍照原規則執行。每次成功回應記錄 transport 與 requested_model；文字輸出不提供可靠的實際服務模型身分及用量，因此 served_model 留空，不推測帳單或後端。

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

`--review-batch-size 2` 或 `4` 可選擇分批，`--review-batch-size 1` 回復逐條；`--max-workers` 控制並行請求（1..32），不是每批的 Claim 數。`--review-batch-max-bytes` 預設 65536，計入 UTF-8 提示與完整 schema，同時作為逐條請求的輸入預算；單條超過預算時會在模型呼叫前回報 `review_context_too_large`，要求提供範圍明確的結構化證據或明確調整預算，不偷偷裁來源或跳過語義驗證。每批來源與相依物件只依明確 ID 去重，同 ID 內容矛盾即拒絕；每條 Claim 的 text、assertion、binding、引用與逐引用判定都保留。

自動入口 `execute_phase6` 會依設定分組原子與深度審查。手動協調流程可用 `prepare_phase6_atomic_batches`／`collect_phase6_atomic_batches`，深度層用 `prepare_phase6_deep_review_batches`／`collect_phase6_deep_review_batches`。prepare 回傳 `review_batches`，各項有 `request_id/claim_ids/prompt/response_schema`；以 request_id 收回 `{results:[逐 Claim 結果]}`。原有逐條 API 與 `atomic_prompts` 仍保留。舊 VM driver 若繼續逐條讀 `atomic_prompts`，不會因更新程式自動變成分批，須改用新介面或自動入口。

同批缺漏或格式不符時，只讓缺漏／不合格 Claim 用獨立請求重新語義審查，最多沿用既有 3 次契約預算；不補 `[]` 或自動 PASS。重複／未知 Claim ID 拒收。PASS 不進 deep；修補仍只重驗受影響 Claim 與 descendants（下游依賴），最後全局掃描不能省略。

P6 的單條與分批路徑都會隨完成順序寫入合格收據與 `checkpoint.json`／`progress.json`，不等 87 條全部回來才保存。`progress.json` 包含 total_claims／atomic_verified／deep_verified、最後更新時間及工作槽設定；每項 response archive 另有 started_at、elapsed_seconds 與成功／失敗狀態。單項出錯會取消未啟動的請求，已在執行的兄弟仍收完保存。最後交付狀態仍以 `result.json` 為準。

新執行會保存分批設定；續跑沿用檢查點，只有明確傳入旗標才覆蓋。沒有分批設定的舊檢查點預設逐條。重新載入後的舊語義收據仍只作稽核，須用新驗證識別碼重新驗證；這個安全契約沒有為省用量而取消。同一 runner 的失敗重試才可略過已合格且輸入雜湊未變的 Claim。

## Claim 與資料契約

- 必填 `claim_id/text/claim_type/refs/fact_refs/dependencies/author_role/assertion/bindings`。
- ID 必須唯一；相依關係不得循環、缺失；Claim 引用若指向另一 Claim，也必須列在 dependencies。
- assertion 至少有 `kind`。`kind=value` 必須明列 `ref_id/value`；`kind=event` 必須明列 `event_ref/is_aggregate`。
- 每個來源引用恰一個 binding，必填 `ref_id/entity/concept/period/scope/unit/basis`。未知維度填 `unknown`，不得猜測；未知來源維度交由 LLM 判斷證據是否足夠。
- `refs` 可含來源與上游 Claim ID；Claim ref 仍須列於 `dependencies`。Claim binding 可省略；已提供的 Claim binding 會原樣保留供 LLM 驗證歸因，不當作 Fact 做來源維度比對。所有 binding 都須有完整欄位且 ref_id 不重複；來源 binding 的完整性與維度比對照常執行。ID 類型依實際目錄判斷，不靠名稱猜測。
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
實際不完整或非法 JSON 可在額度內用原對話補正；完整單一 JSON 程式碼框只在本機移除外框。未提交角色的不可重試失敗會在一般續跑時保持封鎖；修正原因後，須明確用 `--retry-role` 指定該角色才重新請求。任務已送出且確認接收後的暫時性失敗，可在原對話續接；未確認是否送達時封鎖，不盲目重送。
暫存檔只在同一輸出目錄內用於原子替換，檔案與目錄皆同步至磁碟。`--out` 必須由操作者指向持久磁碟或掛載目錄；指定 `/tmp`、非持久 VM 磁碟，或刪除該磁碟後，程式無法保證資料仍在。輸出目錄含研究資料，須按原本權限保存，不能提交到 Git。
重新載入會驗證 schema 與 DAG，重建分析視圖；序列化的舊審查紀錄只作稽核，不能當成新執行的通行證。
`--resume` 會先補齊尚未完成的角色階段，已提交的角色不重新生成；未提交批次的成功回覆只有在 run／角色／模型／完整 prompt 雜湊相同時才可重用，也可從原檢查點旁的 responses 續接到新的輸出目錄。階段次序錯亂或缺半批正式角色仍封鎖，不能跳過缺少的角色交付。
`--resume` 及 `load_checkpoint()` 預設延續原 verification epoch（驗證執行識別碼）。成功的回覆只在同一 run、epoch、完整提示、祖先／來源內容雜湊、回覆規格、模型與代理設定一致時重播，且重新通過逐引用與依賴契約驗收；不信任檢查點內的收據。缺少這些欄位的舊檢查點不能默默重跑付費驗證；要明確加入 `--fresh-verification`，程式庫則用 `load_checkpoint(..., continue_verification=False)` 啟動新一輪。若未提交的角色回覆格式錯誤，可明確加入 `--retry-role bull` 等選項，只重呼指定角色；其他同批成功回覆繼續重用，舊嘗試保留作稽核。已提交角色須走原有 Claim patch（論述修補）流程。
保存失敗會停止後續流程。若請求可能已送出但沒有接收確認，回報 `delivery_unconfirmed`，不自動另開請求；不宣稱能確知供應商是否計費。若成功 result 已寫進串流事件、但最終回覆索引尚未寫入，會先恢復該結果，仍須嚴格驗收。
初始提交、修補、重新載入與最終驗證都共同要求 trader 的唯一 trade_plan，且它必須與衍生交易狀態一致。要撤單請修成 WAIT 方案，不能刪除或改型後留下舊 BUY／SHORT。
V3 的 Markdown 或舊 Claim 缺欄位時，不能自動用文字猜成 V4；必須重新執行 V4 角色流程，或由明確結構化資料完成遷移。
舊 V4 檢查點若有修訂歷史、卻缺少有效的修改前後 Claim 原物件，也會被拒絕，須由 manifest 重跑；沒有修訂歷史、但缺驗證執行識別碼的舊檢查點，須明確選擇新一輪驗證才能載入。
尚待處理的全局內容 finding 會隨檢查點保存，重新載入不能藉重新驗圖而跳過它。
待處理 finding 缺獨立識別碼或識別碼重複的檢查點會被拒絕，須從有效檢查點或 manifest 重跑。

## 將明確修補送入待修檢查點

`blocked:pending_revision` 表示修補要求已保存，單純 `--resume` 不會自動重複支付 patcher（修補模型）的費用。可在續跑指令加入 `--apply-patches /path/to/patches.json`，把明確的 JSON 陣列交給既有 Claim collector（結果驗收器），成功後接回原子、必要深度覆核及最後全局掃描。這個選項只能與 `--resume` 一起使用；不會直接交付未驗證報告。

```json
[
  {"claim_id":"trader.plan","text":"依上游裁決保持觀望，目前不建立新部位。",
   "assertion":{"kind":"trade_plan","trade":{"direction":"WAIT"}}}
]
```

請使用目前 Checkpoint（檢查點）中的精確 ID 與實際修補內容。檔案須為完整 JSON 陣列；重複欄位、非標準數值、未知或未授權 ID 都會拒絕。空陣列或沒有實際變更時仍回 `blocked:revision_no_changes`，保留待修問題與額度，不呼叫任何模型；格式或套用失敗回 `blocked:revision_patch` 並保留理由。有效修改會先保存，再開始模型重驗；後續中斷時，從新 Checkpoint 普通 `--resume`，不要再次套用已完成的修補檔。

新準備的交易方案修補會把直接依賴該方案的 Claim、以及明確引用其衍生 Fact／Calc 的 Claim 一併提供給修補模型，讓撤單或改方向時能在同一筆交易中移除失效引用；這只是結構化相依範圍，是否需要改文字仍由 LLM 判斷，不預設每個下游都有錯，也不重帶整份報告。

舊 Checkpoint 的待修範圍若只有原始問題 Claim，而修正需要連帶更新下游，可明確加 `--patch-dependents`，允許修補檔修改該範圍的 DAG（依賴圖）後代。此旗標只在 `--apply-patches` 時有效，不授權其他無關 Claim；歷史保存原始目標、實際授權範圍與修改前後物件。多個問題指向同一 Claim 時合併成一個 patch；只修改必要欄位，不以刪除真正的依賴掩蓋決策衝突。

修成 WAIT 時要同步修改 `assertion.trade`，並修正仍引用舊交易資料或批准舊方案的下游 Claim。程式會移除該筆舊交易產生的 Fact／Calc，保留其他來源；若仍有失效引用，整組候選會被拒絕而不發布半成品。混合有效修改與無實際變更的輸入，只將有效修改列入正式修訂歷史，另保留完整提交內容供稽核。

若 `--out` 使用原 run 目錄，會在該處持續更新 Checkpoint；需要保留原始快照時，先備份整個輸出目錄或使用另一個 `--out`。原有 `sessions/`、`responses/` 仍須保留，才能重播未受影響的已完成模型結果。

## P6 共用資料縮減與中斷續接

每條 Claim 的線上任務只包含自己的完整物件、直接父 Claim 及兩者引用的完整來源；Calc／Event 的明確來源連結仍遞迴展開。每個父 Claim 本身另有必做的 LLM 驗證，不在每個子節點重新審整串祖先。收據與提示仍綁定全部祖先及來源的 canonical（正式狀態）雜湊，祖先或來源改變就使下游結果失效。這是結構化依賴投影，沒有以文字猜測重要性、截斷證據或漏驗 Claim。

深度覆核保留第一層判決及 findings，不重帶第一層逐來源的冗長說明；仍獨立審原始證據。修補同樣使用局部任務。最終全局掃描仍檢查所有 Claim、章節／作者、DAG、交易物件及每次修改前後的完整 Claim；不再附內容重複的 Markdown、整份舊 patch 歷史或 Evidence 的原始摘錄；只把因 Claim 變更而待重驗的舊 global findings 加入必要掃描。完整正式狀態與歷史的雜湊仍綁定該次掃描。

`session_pending` 不等於成功，也不允許自動 API 轉接或從頭重送。命令列以相同檢查點 `--resume`；程式庫使用同一 RunJournal，或直接 client 的 `resume_request(prompt, response_schema)`，會帶原本 `--conversation <id>`、相同代理設定與隔離目錄續接，只傳「接續完成原任務」的訊息，要求重新交付完整原始物件／陣列及所有批次 Claim，不拼接跨回合片段。普通 `client.complete()` 預設會在有限額度內自動接續相同對話，不開新對話；可用 `--no-auto-continue` 停用。新任務預設兩次續接、總時間為單回合期限三倍，但這些是可調政策：`--session-max-continuations` 與 `--session-budget-seconds` 可提高既有任務的總上限，保留已用次數與時間；`--request-attempts` 控制單次呼叫的嘗試次數。回覆格式補正與修訂輪數也可用 `--max-contract-attempts`、`--max-revisions` 調整。細節及續跑範例見 [P6 修復驗收](P6_BUGFIX_ACCEPTANCE.md#可調整的重試與續跑政策2026-10-18)。預算與次數跨程序保存，硬中止會保守扣除當次預留額度。每個任務的跨程序鎖防止同時重複呼叫；若主程序硬中止但舊子程序仍在，會回報 `session_busy`，不另開付費回合，也不擅自殺掉可能重用的程序識別碼。事件檔損壞則封鎖並保留原檔，不猜修、不盲目重算。

這是恢復同一雲端對話的上下文，不是供應商保證的逐 token（輸出片段）游標續算；逾時仍會結束本機子程序，部分輸出保存作為進度證據，不能當作合格判決。只有完整終端答案且通過驗收才能交付；已知串流中斷的 ERROR 若附完整 JSON，可保留異常註記後進入相同驗收。舊 run 沒有保存對話 ID，無法事後憑空恢復。

Mac 真實測試已在接收任務後刻意中斷，再用原對話 ID 續接；未重傳原始測試標記，回覆仍正確保留標記及算術答案。這證明上下文續接可用，沒有宣稱 87 條真實分析的總時間或帳單已下降。87 層合成依賴鏈的回歸測試驗證線上資料量小於舊全祖先版本的十分之一，同時遠端祖先修改仍使最後一節點失效。

## 耗盡請求的指定恢復

`blocked:session_exhausted` 的結果會列 `session_budget`（耗盡原因、已用／上限與上一個錯誤）及 `request_context`（精確 task／Claim IDs）。attempts 包含本機檢查與續接，不一定等於實際模型呼叫次數。`resumable=true` 表示可調整額度後續接；`retryable=false` 表示不能無策略地盲目重試。

要保留原對話，先提高 `--session-max-continuations`／`--session-budget-seconds`。若操作者明確選擇重開該請求，在原本 `--resume` 指令加入 `--recover-session <CID> --replay-reason "授權理由"`。它只歸檔同 run／epoch 的指定耗盡對話並授權一個新世代；成功請求仍重播快取，不清資料、不換 API 或模型。結果中的 `session_recovery` 保留定位與歸檔資訊。沒有完整 sessions／responses 時不能單靠錯誤 JSON 恢復。

## 全局依賴漏審的補審

全局掃描回覆若只涵蓋部分 Claim，原生流程保留已完成列與全局 findings，列出 `missing_dependency_claim_ids`，再以 `dependency_completion` 只要求補審缺漏項。補審仍用完整 DAG 判斷前提；不將漏項視為沒有依賴，也不重做已完成的全文掃描。

次數使用 `--max-contract-attempts`（包含初次掃描回覆）；耗盡時回 `blocked:global_dependency_coverage`，提高上限可續跑。`global_review_progress` 隨檢查點保存作診斷；重啟從完整 request archives（請求回覆檔）重播並重新驗收，不直接相信檢查點中的審查收據。請保留整個 responses／sessions 目錄。完整覆蓋、修補及必要重驗完成前，報告不能算通過。

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

本輪共用流程根因、驗收數據及已知限制：[P6_RECOVERY.md](P6_RECOVERY.md)。


### 已知串流中斷的恢復

agy 的結束事件若明確回報 `status=ERROR`、`error_code=null`（或省略），且錯誤為 `The stream was interrupted. Please continue the task you were working on.`，V4 會分類為 `stream_interrupted`。這是 CLI（命令列介面）協定診斷的精確比對，不讀 Claim 自然語言來決定驗收結果，也不把未知錯誤視為可恢復斷線。

自動恢復使用原對話 ID、隔離目錄與既有上下文，每次呼叫仍受 `--request-attempts` 約束，嘗試之間等待 15 秒；`--no-auto-continue` 可停用。斷線恢復記入持久化 `transport_retries`，不占用內容續接的 `continuations`；每回合執行時間仍扣既有 `session-budget-seconds`，重啟不歸零。單次嘗試用完時回 `retryable=true`、`resumable=true`，可從原檢查點再續跑，不自動丟棄 session 或換 API。總時間耗盡仍停止，不承諾恢復一定成功。

舊版保存的 `result_failed`／`continuation_exhausted`，只在當次完整事件檔有唯一、最後且符合上述診斷的結束事件，並通過送達／工具檢查時，才重新分類；已扣的舊內容次數不會被清零。一般 `--resume` 可恢復這條已確認的傳輸路徑，不需要先使用 `--recover-session` 放棄對話。事件缺失、未知錯誤、權限拒絕或工具違規不適用。


### 串流中斷仍帶完整答案

已知 `stream_interrupted` 結束事件若同時包含完整 JSON `response`，V4 先回收答案，再走原本的結構／引用／語義驗收，不因 ERROR 標記直接重試。即時接收、保存事件的恢復及放棄 session 前的保護，共用同一份解析規則。JSON 可為單一完整程式碼框；不拼接 deltas（輸出片段）、不填補欄位，也不把 FAIL 改成 PASS。

原始 events.jsonl 保留 ERROR 和原答案，session.json 保存正規化結果及 `transport_anomaly="stream_interrupted"`。恢復僅使用目前 attempt（呼叫輪次）的唯一最後結束事件；完整答案可在次數／時間已耗盡時零模型呼叫取回。已有完整答案時，放棄 session 的操作會拒絕並要求先恢復。

空白、截斷或無法解析的答案仍走既有同對話重試。未知 ERROR、權限／工具違規、重複結束事件、送達不明及異常 CLI 程序退出檢查不因這個例外放寬。回收到 JSON 只代表傳輸完成；缺少必要語義驗收欄位仍會在 collector（結果收集器）被拒絕。


### 僅修改依賴也必須更新舊 finding 狀態

局部修補以完整 canonical Claim（正式論述物件）前後比較判斷驗證輸入是否改動，包含 dependencies，不再維護容易漏欄位的文字欄位清單。針對真正改動 Claim 的 deferred global findings（延後全局問題）移出舊狀態的待修佇列，寫入該輪 history.invalidated_global_findings（新歷史格式 v4-revision/2，重載會檢查該欄位完整性）；未改的 Claim、尚未定位的問題與無實際變化的 patch 都保留原狀。

這是舊判定失效，不是 Python 判定問題已解決。受影響 Claim 與 dependency descendants（依賴後代）仍需重驗，舊 global 收據作廢；既有必要的全局掃描會讀 history.findings_to_recheck，重新檢查舊問題，仍成立就回 findings。沒有新增額外的覆核階段，也不提高 max_revisions。有問題的最終報告仍不能交付。

更新後重啟驅動程序。這項狀態更新發生於 collect_phase6_revision；更新程式不會追溯重寫舊檢查點已完成輪次，也不會無證據刪除其 deferred findings。已卡在 revision_limit 的舊 run 不能保證只換版本就解除；需檢查其當前物件、修訂歷史及 findings，再決定如何重驗，不能直接把上限或歷史次數歸零。
