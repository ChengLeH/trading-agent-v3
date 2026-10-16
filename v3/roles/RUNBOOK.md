# V3 Pipeline Runbook（Subagent 封裝版）

9 個角色全部用 subagent 跑，不用 LLM API key。Assistant（我）負責調配。

## 執行順序

```
Phase 1: 數據（Python，不用 subagent）
  1. Orchestrator.start_run(ticker, manifest)
  2. Orchestrator.ingest(run_id)
  3. Orchestrator.build_facts(run_id)
  → 產出 FACT/CALC stores

Phase 2: 分析師（4 個 subagent，可並行）
  4. 對每個角色：Orchestrator.get_packet(run_id, role)
  5. Spawn subagent，prompt = 角色模板 + evidence packet JSON
  6. 收集 claims JSON，調用 Orchestrator.submit_analysis(run_id, role, text, claims)
  7. Orchestrator.validate_stage(run_id, "analysis")

  角色：market_analyst → fundamentals_analyst → news_analyst → social_analyst
  模板：v3/roles/{role}.md

Phase 3: 辯論（2 個 subagent，可並行，但需等 Phase 2 完成）
  8. Bull subagent：輸入 Phase 2 的全部 claims
  9. Bear subagent：輸入 Phase 2 的全部 claims
  10. Submit + validate

Phase 4: 決策（3 個 subagent，順序執行）
  11. Research Manager subagent：輸入 bull + bear claims
  12. Trader subagent：輸入 RM plan + 技術價位
  13. 調用 Orchestrator.register_trade(run_id, trade)
  14. Risk Manager subagent：輸入全部
  15. Orchestrator.validate_stage(run_id, "final")

Phase 5: 報告（Python）
  16. Orchestrator.snapshot(run_id)
  17. reporting.render_report(snapshot)
  → 產出報告 markdown + sidecar

Phase 6: 對抗式覆核＋修正迴圈（2026-10-16 新增；兩段式）
  18a. 【第一段：外部驗真】runner.prepare_phase6_verify() → prompt，
       經 v3/gemini_client.py 送 Gemini 3.8 Flash（獨立模型家族）。
       Gemini 從報告抽取 15–30 條負重事實並獨立驗證，回傳 mismatch JSON。
       runner.merge_verifier_findings() → V-findings（category=V-fact_verification）
  18b. 【第二段：內部審理】runner.prepare_phase6() → (role, prompt)，
       spawn adversarial_reviewer subagent（已知 verifier 驗過什麼，避免重複開事實 finding；
       可檢查「錯誤事實是否被一致地使用」）。
  19. 收集 findings JSON array（V-findings＋普通 findings 一起）→ runner.collect_phase6(findings)
  20. 若回傳 needs_revision=True：
      a. runner.prepare_phase6_revision(p0/p1 findings) → [(role, prompt)]，
         按 author_role 分組，每組 spawn 對應角色的原作者 subagent 修訂
      b. 收集各組修正後的 claims → runner.collect_phase6_revision(revised_claims)
         → 自動重渲染、重驗證，回傳新的 reviewer prompt
      c. 回到步驟 18b（最多 2 輪；外部驗真只在第一輪做一次）
  21. 若回傳 blocked=False：最終報告（含附錄）；blocked=True：停下，報告給 user

  硬邊界：修正迴圈修的永遠是報告內容（claims 的文字／數字／refs），
  絕不碰程式碼、角色模板、FACT 定義。collect_phase6_revision 會擋掉
  帶有多餘欄位的 claims。
```

## Subagent Prompt 模板

```
{角色模板全文}

## 你的證據包
```json
{packet JSON}
```

## 之前角色的輸出（如有）
```json
{previous claims JSON}
```

請按上面的輸出格式，直接回傳 JSON array，不要 markdown。
```

## 依賴關係

```
market ──┐
fund ────┼──→ bull ──┐
news ────┤          ├──→ RM → trader → risk → P6 覆核 → 修訂 → 重渲染 → 再覆核
social ──┘          │
                    └──→ bear ──┘
```

- Phase 2 的 4 個可並行（無依賴）
- Bull/Bear 需等 Phase 2 全完成
- RM 需等 Bull/Bear
- Trader 需等 RM
- Risk 需等全部

## 錯誤處理

- 任一 subagent 失敗：重試 1 次，再失敗則該角色 claims 留空，validation 會標記
- Gate 擋下（Phase 2 analysis／final）：不直接停，進修正迴圈——
  prepare_gate_revision → reviser 修報告內容 → collect_phase2_revision／
  collect_final_revision → 重跑 gate，最多 2 輪；2 輪後還擋才停下回報 user
- Phase 6：有問題就進修正迴圈（見 Phase 6 一節），只修報告內容
- 硬邊界：修正迴圈永遠只動報告內容（claims 文字／數字／refs），絕不碰程式碼；
  改程式碼一律要 user 明確批准
- Subagent 輸出非 JSON：嘗試修復，修不好則重跑該角色

## claim_type 合約（2026-10-15 修）
所有角色的 claim_type 只能用 `source_fact` / `derived_fact` / `semantic_fact` 三種（validator 只認這三種）。
refs 是單一陣列。詳見各角色模板的「合約說明」。

## Bull/Bear 設計（2026-10-15 修）
Bull 和 Bear **完全並行**，互相不看對方的輸出。兩者都只讀 Phase 2 的分析師 claims。
反駁（rebuttal）由 Research Manager 在 Phase 4 統一做 adjudication。
不要加第二輪 rebuttal——複雜度不值得。
