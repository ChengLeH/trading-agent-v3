# P6 五項上游問題修復驗收（2026-10-18）

## Problem（問題）

附件五項都涉及 V4 共用流程，非單一股票或 VM 特例。第 1、2、5 項是程式缺陷；第 3 項是耗盡預算後缺少明確授權的恢復入口；第 4 項是錯誤處理及持久化缺口，但「存在未解問題時直接接受空修補」會使驗收失效，不能採用。

## Root Cause（根因與因果鏈）

| 項目 | 原因與影響位置 | 本次修正 |
|---|---|---|
| 1. 結果完成卻卡住 | `agy_session.py::_run_session` 原先等待管線結束／程序退出，模型已回 result 仍無法交給 `journal.py::_request_locked` 存檔 | `7607e57` 已修：以 result 結束回合，最多額外讀取 250ms 檢查立即出現的協定違規；不再等整個模型逾時。保存結果時間及首段輸出時間，重啟可從完整事件恢復收據 |
| 2. 應用錯誤誤當串流損壞 | `_run_session` 未辨識有型別的 `error_message`，`run_session` 因 invalid_stream 拒絕原對話續接 | 辨識為 result_failed／session_pending；只允許原對話、相同綁定及有限預算續接。舊 invalid_stream 只在目前 attempt 的完整事件確認是 application/system 訊息且沒有工具紀錄時恢復 |
| 3. 耗盡後沒有出口 | `run_session` 正確限制續接兩次，但沒有保留舊資料並明確授權新回合的 API | 新增 `session_replay.py::abandon_session`，保留整個舊目錄及理由；授權只對相同模型／提示／設定的一個新世代有效。跨程序鎖、兩階段標記與雜湊處理歸檔中斷，不自動清空或重跑 |
| 4. 空修補例外 | `runner.py::collect_phase6_revision` 將空陣列交給必須非空的 patch；`checkpoint.py` 未保存 pending_patch | 明確回傳 blocked:revision_no_changes，保存未解問題與待修 Claim；不改內容、不花修訂輪數、不算通過。重啟維持待修狀態；舊的全局通過收據無法繞過它 |
| 5. JSON 格式失敗後永遠卡住 | Cloud 層解析時 SessionState 已標成 complete，之後只能反覆讀相同壞格式 | JSON 驗證提前到完成狀態之前；格式錯誤以原 CID 要求重新完整輸出，最多兩次且共享原續接預算。保留原始錯誤回覆，不擷取 Markdown／片段，不跳過語義契約 |

第 1 項回合結束的依據為 [Antigravity 官方 headless 文件](https://www.antigravity.google/docs/cli/headless/) 的 result 事件語義，並以保留開啟管線的測試程序驗證。

另外修正：送出前先保存「尚未送出」身分，避免已寫 in_flight 卻尚未啟動模型的中斷被永久誤判；格式與送達旗標嚴格驗證布林型別；恢復授權與新回應紀錄共享生命週期鎖。沒有新增 API fallback、工具權限或本地模型。

## Recommended Tests（驗收）

- `test_journal.py`：四個平行 worker 的模型結果已完成但管線不結束；遺失登記後零新增模型呼叫恢復；in_flight 登記後尚未啟動即中斷，重啟只送一次。
- `test_recovery_errors.py`：有型別的應用錯誤及舊 invalid_stream 恢復；非布林送達旗標拒絕續接，子程序不啟動。
- `test_session_replay.py`：一次授權、綁定錯誤、存活程序、未確認送達、完整結果、鎖競爭、歸檔前後中斷、授權世代競態。
- `test_json_repair.py`：原 CID 修正、無第四次呼叫、壞 JSON 在中斷後不能誤當完成、結果已保存但狀態寫入失敗時零呼叫恢復。
- `test_empty_revision.py`：空修補保留內容／收據／問題、不增加輪數、檢查點重載及損壞拒絕、全局收據不能繞過未解問題。

Mac 真實雲端測試：既有 `gemini-3.8-flash-high` 的 fenced JSON 失敗對話，以同一 CID 接續一次，10.986 秒收到合法 JSON，逐引用契約通過。CID 前後相同；原任務沒有重送，沒有從壞文字抽取 JSON。原始資料保存在本次工作區 `artifacts/json-repair-real/`，未提交研究對話。

另做提示格式對照：小提示現行格式兩次成功，簡化格式一次成功／一次 fenced JSON；原 fundamentals.2 同證據對照，現行格式約 147 秒，簡化格式約 249 秒。樣本很小，沒有證據顯示改短格式能解決卡住，因此保留正式格式及原快取身分。這些時間不是整份報告耗時保證。

本次最終 Mac 驗證：`python3 -B -m unittest discover -s v4 -t .`，162 項通過；V3 基準腳本 24 項通過。`git diff --check` 通過。測試包含程序重啟／狀態重載與新請求世代；未執行 VM 全量重跑。

## Fix／更新方式與回滾

只修改 V4。更新分支後重新啟動 V4 驅動程序；保留整個輸出目錄，包括 `sessions/`、隱藏的生命週期鎖與重跑授權標記、歸檔、回應及檢查點。不要清除 atomic 快取，也不要用 fresh verification 取代普通續跑。完整結果會重新通過 collector（結果驗收器），不需重新呼叫模型。

只有操作者決定接受新付費回合時才呼叫下列 API；一般流程不會自動呼叫：

```python
from v4.agy_session import abandon_session
archive = abandon_session(session_directory, reason="操作者明確授權的原因")
# 再由原 journal/client 執行原請求；保留 archive 與隱藏標記。
```

已完成結果、舊程序仍在、送達不明或資料不合法時，此 API 會拒絕。待修 Claim 應提供實際修補後呼叫 collect_phase6_revision；空回覆維持 blocked，不能靠重啟洗成成功。

回滾可撤回本次修復提交並重啟驅動程序；先備份完整輸出資料，不還原或刪除研究狀態。舊版本不支援新增的重跑授權與待修狀態保護，回滾後不要直接繼續這些新狀態。

## Prevention／Confidence（預防與信心）

以協定事件判斷完成；付費前保存身分與送達邊界；先驗回覆再登記完成；所有自動修正都受同一對話及持久預算限制。結果復原、同對話修正與新付費回合是不同操作，不能混為盲目重試。

程式缺陷及回歸測試信心高；未操作 VM 的 87 條全量 run，不能宣稱它已完成，也未驗證整份報告的總時間／費用。外部 driver 仍須遵守非 retryable 狀態；本機恢復測試不保證供應商永久保留對話。


## 0.25 秒與慢速模型的界線

0.25 秒只在完整 `SUCCESS result` 事件通過解析後計時，供有限度檢查立即到達的尾端協定錯誤；不是 token（文字片段）間隔、首字等待或模型生成期限。完整 result 之前仍使用請求逾時，預設 1200 秒，可由 `--request-timeout` 指定；大提示至少 1800 秒。`test_slow_generation_and_fragmented_result_do_not_use_teardown_deadline` 以模擬程序讓文字間隔與結果分段間隔各達 0.4 秒，確認不會被 0.25 秒截斷。

本次未載入本地模型；V4 目前仍只接既有 Antigravity 雲端連線。未來其他供應商須用各自的明確「回合完成」協定接入，不能把一般文字片段當成完整 result。0.25 秒也不能保證捕捉任意晚到的程序診斷；它的目的正是避免已交卷後無限等待程序結束。


## 可調整的重試與續跑政策（2026-10-18）

原本將「防止重啟後重複花費」做成永久兩次上限，太僵硬。本版保留已用次數／時間，但讓操作者提高同一對話的總上限，不必清快取、重設執行身分或另開對話。設定不參與提示／語義契約雜湊，不會使已完成驗證失效。

| 設定 | 預設／行為 |
|---|---|
| `--session-max-continuations` | 未指定時保留既有值；舊資料預設 2。可明確提高，無寫死的最大次數 |
| `--session-budget-seconds` | 未指定時保留既有時間預算；新請求預設單回合期限的三倍。指定的是總預算，不是額外秒數；已用時間不歸零 |
| `--request-attempts` | 單次呼叫最多嘗試次數，預設 3，可提高；包含啟動重試、格式修正及原對話續接 |
| `--no-auto-continue` | 預設遇到可續接的 session_pending 會在上述上限內自動續接相同 CID；此旗標改為暫停交由操作者處理 |
| `--max-contract-attempts` | 每個相同 Claim／輸入的語義回覆格式補正次數，預設 3，可提高。檢查點保留既有計數及上限 |
| `--max-revisions` | 預設 3；取消最多 20 輪的硬上限。續跑可提高原有上限，保留已用輪數 |

例如在原本的 `--resume` 指令加上：

```text
--request-timeout 1800 --session-max-continuations 6 --session-budget-seconds 12600 --request-attempts 7 --max-contract-attempts 6 --max-revisions 10
```

這是可用範例，不是建議所有模型固定採用的設定；時間／重試越多可能增加耗時及費用。續接仍先從完整已保存結果復原，沒有結果才繼續原對話；不重送整份任務。若續接程序在送出新回合前啟動失敗，保留上一回合可續接狀態，不誤寫成永遠無法恢復。

仍保留：每條 Claim 的 LLM 語義驗證、逐引用證據核對、唯一 ID、型別綁定、修正後後代失效與重驗、最後全局掃描。不能把缺少逐引用判斷當作空陣列，也不能讓未解錯誤或空修補算通過。並行數與批次大小目前仍有保守範圍，這次沒有用提高並行度來假裝加速。

更新須重啟驅動程序。降低旗標不會回溯刪除已授權的持久上限；需要更嚴格政策時應另行明確設計停止／預算管理，不清除既有使用紀錄。回滾前備份整個輸出目錄；舊版只接受最多兩次的 session 格式，已超過兩次的新狀態不能交給舊版直接續跑。

本輪驗收：V4 170 項通過，V3 基準 24 項通過；獨立審查後修正續接啟動失敗的狀態覆蓋。新增測試涵蓋已耗盡上限增額、同 CID 自動續接、已用時間不歸零、舊檢查點、CLI 設定、四次以上格式補正及啟動失敗後再恢復。本輪未載入本地模型、未呼叫真實雲端模型，也未重跑 VM 全量；慢速與失敗條件以可重現的模擬子程序驗證。


## Dependency unresolved（語義依賴不確定）的診斷與恢復

### Problem（問題）

全局模型對某個 Claim 回覆 `unresolved=true` 時，舊版會直接拋一般 ValueError，原生入口只顯示 blocked:execution 與 Claim ID，沒有把模型理由帶進可修補結果。模型是否誤判須看其 reason、證據與 Claim，不能光看布林旗標決定。

### Root Cause（根因）

`3fe34e2` 的 `semantic_contracts.py:112` 在讀到 reason 後立即拋出例外 → `runner.py::collect_phase6_global_sweep` 無法建立全局稽核收據／修補問題 → `execute_phase6` 的例外處理只回傳一般錯誤，沒有理由及依賴。這是上游診斷與恢復流程缺口，不表示模型的不確定判斷本身一定錯誤。

目前原生入口使用 RunJournal：完整回覆會先保存在 `responses/` 及 `responses/archive/`；持久 session 的 `session.json` 與 `events.jsonl` 也保存完整終結結果。不能僅因看到串流 deltas 就判定資料必定遺失。未取得使用者該次 run 的檔案，無法確認其實際保存情況，亦無法判定該次 Claim 是資料問題或模型誤判。

### Fix（修正）

將合格但不確定的語義判斷轉為 `dependency_unresolved` 結構化 finding，保留 Claim ID、reason、完整原 review、現有依賴與 required_dependencies。它不會自動新增不確定的依賴邊，也不會放行；進入現有局部修補，再重驗該 Claim 及後代，最後再做全局掃描。

不確定 finding 保存到 deferred_global_findings，檢查點重載或先修其他 Claim 的缺邊都不會丟失；修補歷史保留原理由。修補模型失敗或修訂額度用完時，結果仍列出 finding 及理由。沒有加 `--force`，沒有靠文字比對猜依賴，也沒有省略原子驗證或全局掃描。

### Recommended Tests／Prevention（驗收與預防）

`test_semantic_acceptance.py` 覆蓋：不確定理由／依賴保存、重載後仍可修補、混合缺邊修補不丟其他不確定 finding、完整自動局部修補＋後代重驗＋再掃描、額度用完／修補模型失敗時理由可見；既有 stale／循環／未知 ID 及漏審拒絕測試仍保留。未解決前不能 finalize（交付）。

### Confidence／更新與回滾

原因與共用修復信心高；未取得真實 run 回覆，該次判斷的正確性未確認。本次不呼叫模型，也不改 VM。更新後重新啟動驅動程序，保留檢查點、sessions 及 responses。先前有完整原始全局回覆時，可透過原 RunJournal 重播並重新驗收，無須為此清空已完成的原子結果。若外部 driver 未使用 RunJournal，須保留並傳回原始完整回覆；不能從部分 deltas 猜造。

回滾可撤回本次提交並重啟；先備份完整輸出資料。舊版會再次對 unresolved 拋一般例外，不能視為問題已解決。

本輪最終驗收：V4 175 項通過，V3 基準 24 項通過，git diff --check 通過。獨立審查後補強混合缺邊／不確定 finding 在圖修補失敗或額度用完時的診斷合併；只依明確 finding_id 去除同一問題的副本，不以 prose（自然語言文字）合併問題。


## 角色輸出混合來源／Claim binding 的契約修正

### Problem（問題）

原生角色提示允許 `refs` 同時列出來源與上游 Claim，schema（資料格式）也允許 binding.ref_id 指向 refs 中的任一項，但 `submit_batch` 卻要求全部 binding ID 集合等於來源 ID 集合。模型提供了額外的上游 Claim binding 時，即使來源綁定完整、依賴也正確，仍在角色提交時被拒絕。

### Root Cause（根因與影響）

`95b6068` 的 `runner.py::ROLE_CONTRACT`／`schema.py::Claim.from_dict` 允許混合 refs 與 binding → `pipeline.py:181` 以所有 binding ID 對照只有來源的 expected 集合，額外 Claim binding 造成不相等 → ValueError，中止原生角色交接。僅修改提示詞不足以處理已產生的合法額外資訊；僅改 submit_batch 又會在 `bindings.py::validate_claim_bindings` 被當成「找不到來源」拒絕。

### Fix（修正與變更差異）

- `pipeline.py`：來源 refs 仍必須恰有一筆來源 binding；允許已存在、已宣告為 dependency 的可選 Claim binding，保留原資料。所有 binding 都不可重複；真正缺少來源 binding 時附上 claim_id、缺漏及重複 ID。
- `bindings.py`：以完整 Claim 目錄辨識上游 Claim；Claim 的歸因交給必做的逐引用 LLM 驗證，不把它當成 Fact 來源做維度比對。真正來源的完整性／數值維度驗證保留。
- `runner.py::ROLE_CONTRACT` 與 README：明示來源與 Claim binding 的差異、Claim binding 可省略及依賴要求。
- `test_integration.py`：重現原例外，新增混合 binding 提交、原生研究主管交接、漏驗拒絕、缺來源／重複／未知 ID／漏依賴拒絕、錯誤來源 unit 仍失敗，以及檢查點與局部修補保留資料的測試。

不以 ID 前綴或 prose（自然語言文字）猜類型，不刪模型輸出、不補造來源、不把 Claim 當作 Fact、不跳過語義驗證。來源與 Claim 撞 ID 仍拒絕；只有 Claim 引用而沒有實際來源的 grounded claim（有來源事實論述）仍不可假裝有來源。

### Recommended Tests／Prevention（驗收與預防）

新增測試先在原碼重現 `every source ref requires exactly one typed binding`，再驗證修復。`reference_checks` 的必驗 ID 仍含來源與 Claim；測試故意漏掉 Claim 檢查，確認不能通過原子驗收。來源 unit 不符依然 FAIL，說明放寬的是可選的 Claim metadata（中繼資料），不是來源正確性。

### Confidence／更新與回滾

共用契約矛盾已重現，修復信心高；未取得使用者本次完整角色 JSON，未重跑 VM，也未呼叫真實模型，不宣稱該次整份報告已跑完。修改限 V4；更新後重啟驅動程序，保留完整角色回覆、sessions、responses 與檢查點。已有完整角色 JSON 可重新交給對應 collector 驗收；提示詞版本更新會改變新角色請求身分，不擅自將不同提示的快取當相同請求。

回滾可撤回本次提交並重啟。先備份輸出目錄；舊版不能讀回含 Claim binding 的新檢查點，不能把回滾後的拒絕當作資料已損壞。

本輪最終驗收：V4 180 項、V3 基準 24 項通過，git diff --check 通過。獨立審查確認來源／Claim 依實際目錄區分，來源與 Claim ID 撞名仍拒絕；Claim binding 不會免除 DAG 檢查或逐引用 LLM 驗證。


## Global dependency review（全局依賴審查）漏項的局部補審

### Problem／Root Cause（問題與根因）

原先 `normalize_dependency_review` 用 coverage（覆蓋範圍）全等檢查發現漏項後直接拋一般 ValueError。模型已完成的跨章 findings 與 dependency rows 沒有進入可續接狀態，原生驅動程序也沒有只補漏項的分支。全體 Claim 都要審是正確性要求；沒有局部補審及精確缺漏診斷，才是此次共用流程缺口。

### Fix（變更與實際路徑）

1. 完整全局掃描仍必做。收到合法但不完整的 dependency_reviews 時，保留已完成 rows、原始 findings、模型及正式狀態／歷史雜湊；不把沒出現的 Claim 補成空依賴。
2. Collector（結果驗收器）回傳 reviewed_dependency_claim_ids、missing_dependency_claim_ids、coverage_round、needs_retry 及 blocked:global_dependency_coverage。原生 execute 自動再呼叫補審，target_claim_ids 只列漏項；仍給完整 DAG 作為判斷前提的脈絡，不重做全文一致性掃描。
3. 補審回覆合併後再共同檢查 ID、覆蓋與跨回覆的依賴循環；不得覆寫已接受列，不能換模型或沿用過期狀態。原 findings 與補審新 findings 都保留。全部覆蓋後才建立 global receipt（全局驗收紀錄），再進入既有局部修補或交付。
4. 次數上限使用 `--max-contract-attempts`，包含原掃描回覆。耗盡時列出缺漏 ID 與進度；提高上限可接續，不清空先前成果。
5. global_review_progress 隨檢查點保存，供診斷；重新載入不將它當作已接受的模型收據。原 RunJournal 重播原掃描及補審的完整回覆，再通過 collectors 重建驗收。必須保留完整 responses／sessions；已保存的相同請求不需新增模型呼叫。
6. 開始新掃描會撤銷先前的 clean receipt（無問題紀錄）；pending（未完成）掃描不能靠舊紀錄交付。補審模型變更在呼叫前拒絕，避免花費後才發現不符。

### 六項清單核對

| 項目 | 現版處理 |
|---|---|
| unresolved=true | 已轉成保留 reason 的結構化修補問題，不再僅拋一般例外；未解決不會假裝通過 |
| 漏 dependency review | 本輪新增局部補審與完整缺漏診斷，不默認無依賴 |
| 漏 reference_checks | 原子／深度層已有局部補正及可調上限；本輪錯誤訊息另列 missing_ref_ids，不能省掉真實的逐引用驗證 |
| 混合來源／Claim binding | e83f9f2 已區分必需來源 binding 與可選 Claim binding，未放寬真正來源正確性 |
| 空 patch | efda7e4 的協調層已明確回 blocked:revision_no_changes 並保存待修問題；無 findings 時根本不呼叫 patch。底層 patch([]) 的拒絕不是原生流程崩潰，不能把未解問題當「無需修正」放行 |
| 非唯一 trade_plan | 必須保留單一正式交易方案，避免沒有決策或互斥決策被當成可交易報告；本輪診斷增加 trader_claim_ids／trade_plan_claim_ids，不自造交易或自選其中一個 |

### Recommended Tests／Prevention（驗收）

`test_global_completion.py` 覆蓋：57 條中先審 13 條、只補剩餘 44 條；原 findings 與新 findings 不遺失；未知／重複／未要求 ID、不同模型、stale state（過期狀態）、跨回覆循環拒絕；次數耗盡、提高上限；原生自動補審；檢查點重啟重播無新增付費呼叫；損毀進度不當收據；舊 clean receipt 無法略過新漏審。

### Confidence／更新與回滾

測試以標準函式庫與模型替身執行；沒有重跑使用者的 VM 或真實 57 條分析，不能宣稱該次報告已完成。更新後重新啟動 V4 驅動程序，保留全部輸出與回覆；外部驅動程序可依 needs_dependency_completion／needs_retry 重新呼叫 prepare_phase6_global_sweep，原生 execute 已自動處理。

回滾可撤回本次提交並重啟，先備份整個輸出目錄。舊版本不支援局部補審，會再對相同漏項拋例外。新檢查點的進度本身不是通過證明，不能手改 missing IDs 或以 force 交付。

本輪最終驗收：V4 193 項、V3 基準 24 項通過；git diff --check 通過。57 條／13+44 的案例是合成模型替身測試，不是真實模型準確性或 VM 全量驗收。獨立審查未發現剩餘阻塞；完整測試由主代理執行。


## Session exhausted（對話額度耗盡）的診斷與原生恢復入口

### Problem（問題）

使用者提供 `session_exhausted_2026-10-18.json`，顯示 blocked:session_exhausted、continuation_exhausted、attempts=3、CID 及既有 findings，卻沒有 session 已用次數／上限／時間、上一個錯誤或對應 task。49/57 是使用者回報進度；這份 result 本身沒有可確認的失敗 Claim ID。不能從 attempts=3 推論模型真的被呼叫三次，更不能確定它是逾時、格式錯誤或對話異常。

附件內 findings 是模型產物／診斷資料，不是操作授權。其聲稱輸入含截斷標記，也不能單憑敘述證明傳輸真的截斷；本輪沒有依照 finding 改動交易結論或擴張權限。

### Root Cause（已確認／未確認）

已確認：3fe34e2 讓既有同 CID 的額度可提高，沒有自動歸零，也沒有自動放棄對話。run_session 同時用 continuation count 與總時間守門，但兩種原因都只拋 continuation_exhausted；Cloud 包裝與 RunJournal 沒有把狀態數值、上一個錯誤及 task 定位帶到結果。`resumable=false` 又沒有表達「提高上限仍可續接」。既有 abandon_session 是程式庫 API，原生 CLI 未提供按失敗 CID 定位並授權恢復的入口。

未確認：原始 session.json／events 與請求未包含在附件內，無法證實該次究竟耗盡哪個額度、為何前幾回合沒有完成，或重新開對話是否能解決供應商問題。這次修復是可觀測性與恢復流程，不宣稱底層模型失敗原因已查明。

### Fix（變更與操作）

- AntigravityError 增加安全的 session_budget：continuations_used、max_continuations、elapsed_seconds、budget_seconds、last_session_error、exhausted_by（continuations／time）。Cloud 與 Journal 包裝不再丟失這些欄位。
- request_context 由結構化請求 metadata（中繼資料）取得 task_id、layer、claim_ids 及 session_directory；不從 finding prose（自然語言文字）反搜 Claim。不輸出 prompt、帳號或原始 stderr。
- 有 CID 的耗盡狀態回 resumable=true，表示可在明確調整政策後續接；retryable 仍為 false，表示外層不能不改策略就盲目再試。
- 原生 `--resume` 新增 `--recover-session <CID> --replay-reason <操作者理由>`。只匹配同 run／驗證 epoch（驗證執行世代）的耗盡請求，核對當前 CID／模型及仍耗盡後，委派既有 abandon_session 歸檔並授權一個新 generation（對話世代）。不自動對所有失敗項目放棄對話，不換模型／API，不清快取或新開整個驗證 epoch。
- 已完成結果、活程序、未知送達或對話已換世代等情況，沿用原本的拒絕保護。提高上限後已不再耗盡時，也拒絕丟棄原對話，應先續跑。

先看 session_budget 決定策略：要保留同 CID 就使用既有 `--session-max-continuations`／`--session-budget-seconds`（必要時調整單回合 `--request-timeout`）；確定要換一個新對話時，在原本的 resume 指令附加：

```text
--recover-session <結果中的conversation_id> --replay-reason "操作者明確授權這個耗盡請求重開一次，保留舊資料"
```

結果附 session_recovery，包括 task_id、歸檔位置及新世代識別碼。該授權不等於內容通過；新回覆仍須經原子／深度／全局各層驗收。新對話也可能再次失敗，程式不承諾換對話就能解決模型／供應商問題。

### Recommended Tests／Prevention（驗收）

`test_exhaustion_recovery.py`：分別耗盡次數、時間或兩者；未啟動模型就列出安全預算診斷；Cloud／Journal／結果保存診斷；批次請求列出結構化 Claim IDs；49 個成功請求全部重播，只對指定失敗請求授權一個新對話；拒絕空理由、錯誤 run、未知 CID、已完成狀態及第二次重開；CLI 明確旗標與結果稽核；診斷欄位不能夾帶 prompt／帳號。

### Confidence／更新與回滾

已確認的程式缺口信心高，原 run 的模型失敗原因未確認。本次僅在 Mac 跑模擬子程序與回歸測試，沒有操作 VM、呼叫真實模型或放棄實際 CID。更新後重啟驅動程序，保留整個輸出目錄，包括隱藏授權標記、sessions、responses、archives 與檢查點。若資料已被清除，這個恢復入口不能憑 result.json 重建對話或 49 條通過紀錄。

回滾可撤回本次提交並重啟；先備份所有輸出。舊版本不認得新 CLI 恢復旗標，但既有原始檔與歸檔不會被本次修復刪除。

本輪最終驗收：V4 202 項、V3 基準 24 項通過，git diff --check 通過。獨立審查後新增限制：只准在已授權 run 根目錄的 sessions 直接子目錄恢復，拒絕外部路徑／跨目錄 symlink；核對 index metadata 雜湊、attempt ID、request.json 的 prompt／模型／schema footer 與 session 綁定。另測帶 schema 的真實請求格式與歸檔後中斷再授權，確認只保留原世代授權一次。


## 跨 Claim 耗盡：完整回覆恢復與續接指令修正

### Problem（問題）

使用者回報進度由 49/57 到 51/57 後，仍在不同 Claim 遇到 continuation_exhausted。附件 stuck_claim_49.md 只有 1,201 bytes，包含被截斷的 Claim 摘錄，不是完整請求、session 或新版本的 session_budget。不能重現這次交易計劃請求，也不能確定它耗盡的直接原因。

### Root Cause（已確認／未確認）

確認兩個共用缺陷：原續接指令同時要求「完整最終 JSON」與「不要重複輸出先前物件」，但收集器不會跨回合拼接片段，兩者互相矛盾。另有歷史真實測試的 terminal SUCCESS（回合成功結束事件）已含完整可驗收 JSON，只因外包一層 JSON 程式碼框而被當成 invalid_json，再耗一次同對話補正。

這兩點可能增加續接次數，但尚無證據證明就是此次 VM 交易計劃耗盡的原因。仍需失敗結果的 session_budget／last_session_error／exhausted_by 區分逾時、格式與供應商錯誤，不以單一 Claim 或 ticker 特例掩蓋未知原因。

### Fix（修正）

- 續接指令要求完整重新輸出原始物件／陣列，包括先前已輸出的欄位及所有批次 Claim；沿用原上下文，不重新搜尋或重傳整份任務。
- 共用 JSON 正規化只接受整份回覆為一個完整 JSON 值，或單一完整 JSON／無語言標記程式碼框。拒絕夾帶解說、多個物件／程式碼框、截斷片段及其他語言標記。不做語義猜測、欄位補值或片段合併。
- 原始事件保留原文，正式 session 結果保存移除外框後的 JSON。恢復端先查找安全的既有完整 SUCCESS，即使先前已標 invalid_json 或 continuation_exhausted，也可零模型呼叫恢復。仍要求有效的當次事件序列、唯一結果、送達證據及原本工具政策。
- abandon_session 使用相同解析規則，拒絕丟棄已有可用完整結果的對話。沒有 terminal SUCCESS 的部分輸出仍不能當成完成。Claim 語義、引用與 typed binding（型別綁定）驗收不變。

### Recommended Tests／Prevention（驗收）

V4 208 項、V3 基準 24 項通過；git diff --check 通過，獨立審查未發現阻塞。新增測試涵蓋完整外框、非法內容、耗盡後零呼叫恢復、缺少成功事件、完整物件續接與缺少 reference_checks 仍拒絕通過。

另將歷史真實 SUCCESS 原文與對應狀態複製到隔離目錄離線重播，禁止建立模型子程序：原子驗收 PASS、1 個 reference_check、續接次數仍為 0，沒有新增模型呼叫。這是先前測試回覆的恢復驗收，不是本次 COIN Claim，也不代表 VM 全量已通過。本輪未執行真實模型或操作 VM。

### Confidence／影響與回滾

已確認的兩項共用缺陷信心高；目前 VM 耗盡的直接原因仍未確認。影響所有使用共同 JSON 回覆／對話續接流程的 V4 階段，不修改 V3／main，不增加權限、重試額度或 API fallback（備援轉接）。更新後重啟驅動程序，保留完整輸出目錄並先一般續跑；已有完整外框結果可直接恢復，勿先丟棄 session。沒有完整結果的供應商失敗仍可能耗盡。

回滾可撤回本次提交並重啟；先備份輸出。舊版會再拒絕外包程式碼框的完整 JSON，原始事件不受修改。
