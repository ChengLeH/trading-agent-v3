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
