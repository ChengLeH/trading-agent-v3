"""Empty patch responses block unresolved revisions without consuming work."""
from copy import deepcopy
import unittest
import json
import tempfile
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from .test_integration import CloudDouble, seeded, review_all, global_verdict
from .checkpoint import save_checkpoint, load_checkpoint


class EmptyRevision(unittest.TestCase):
    def run_patch_cli(self, runner, patch_text, directory, *, verifier=None, patch_dependents=False, global_findings=None):
        from .__main__ import main

        root = Path(directory)
        checkpoint, patch_file, out = root / "source.json", root / "patches.json", root / "out"
        save_checkpoint(runner, checkpoint)
        source_bytes = checkpoint.read_bytes()
        patch_file.write_text(patch_text, encoding="utf-8")
        restored = []
        def read_checkpoint(path, **kwargs):
            restored.append(load_checkpoint(path, **kwargs))
            return restored[-1]
        clients = dict(author=CloudDouble("test/author", lambda _: self.fail("supplied patches must not invoke author")),
                       verifier=verifier or CloudDouble("test/verifier"), deep=CloudDouble("test/deep"),
                       global_reviewer=CloudDouble("test/global", lambda _: global_verdict(restored[-1], global_findings)))
        args = ["--resume", str(checkpoint), "--apply-patches", str(patch_file), "--allow-ephemeral-out", "--out", str(out),
                "--author-model", "author", "--verifier-model", "verifier", "--deep-model", "deep",
                "--global-model", "global_reviewer"]
        if patch_dependents:
            args.append("--patch-dependents")
        with patch("v4.__main__.load_checkpoint", side_effect=read_checkpoint), \
             patch("v4.__main__.AntigravityCloudClient", side_effect=lambda model, **_: clients[model]), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = main(args)
        self.assertEqual(checkpoint.read_bytes(), source_bytes)
        return rc, json.loads((out / "result.json").read_text()), load_checkpoint(out / "checkpoint.json"), clients

    def prepared(self):
        runner = seeded()
        review_all(runner)
        findings = [dict(claim_id="a", issue="Unresolved assertion", evidence="fixture", suggested_fix="Correct assertion")]
        runner.prepare_phase6_revision_v4(findings)
        return runner, findings

    def test_repair_candidates_cover_transitive_descendants_and_survive_reload(self):
        runner, findings = self.prepared()
        self.assertEqual(runner.pending_patch["ids"], ["a", "b", "c"])
        self.assertEqual(runner.pending_patch["findings"], findings)
        self.assertEqual(runner.pending_patch["dependent_candidate_ids"], ["b", "c"])
        before = deepcopy(runner.receipts)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            save_checkpoint(runner, path)
            restored = load_checkpoint(path, continue_verification=True)
            self.assertEqual(restored.pending_patch, runner.pending_patch)
            review_all(restored)
            before = deepcopy(restored.receipts)
            result = restored.collect_phase6_revision([
                dict(claim_id='a', text='correct premise'),
                dict(claim_id='c', text='correct downstream interpretation')])
            self.assertEqual(set(result['revalidate_claim_ids']), {'a', 'b', 'c'})
            self.assertTrue(set(before) - {'a', 'b', 'c'} <= set(restored.receipts))
            self.assertIsNone(restored.global_receipt)

    def test_candidate_scope_cannot_authorize_unrelated_claim_on_reload_or_patch(self):
        runner, _ = self.prepared()
        with self.assertRaises(ValueError):
            runner.collect_phase6_revision([dict(claim_id='independent', text='unauthorized')])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            save_checkpoint(runner, path)
            data = json.loads(path.read_text())
            data['pending_patch']['ids'].append('independent')
            data['pending_patch']['dependent_candidate_ids'].append('independent')
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'dependency closure'):
                load_checkpoint(path)

    def test_empty_response_preserves_pending_findings_and_all_review_state(self):
        runner, findings = self.prepared()
        before = deepcopy(dict(claims=runner.run.claims, receipts=runner.receipts,
                               deep=runner.deep_receipts, global_review=runner.global_receipt,
                               pending=runner.pending_patch, history=runner.history))
        with patch.object(runner.orch, "patch", side_effect=AssertionError("empty patches must not reach pipeline")):
            result = runner.collect_phase6_revision([])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["status"], "blocked:revision_no_changes")
        self.assertEqual(result["findings"], findings)
        self.assertNotIn("report_markdown", result)
        self.assertEqual(runner.revision_round, 0)
        self.assertEqual(before, dict(claims=runner.run.claims, receipts=runner.receipts,
                                     deep=runner.deep_receipts, global_review=runner.global_receipt,
                                     pending=runner.pending_patch, history=runner.history))
        with self.assertRaises(ValueError):
            runner.finalize()

    def test_unprepared_or_stale_empty_response_still_rejected(self):
        runner = seeded()
        with self.assertRaisesRegex(ValueError, "stale or unprepared"):
            runner.collect_phase6_revision([])
        runner, _ = self.prepared()
        runner.run.claims[0]["text"] = "Changed after preparation"
        with self.assertRaisesRegex(ValueError, "stale or unprepared"):
            runner.collect_phase6_revision([])

    def test_nonempty_noop_patch_preserves_findings_receipts_and_revision_budget(self):
        runner, findings = self.prepared()
        before = deepcopy(dict(claims=runner.run.claims, receipts=runner.receipts,
                               deep=runner.deep_receipts, global_review=runner.global_receipt,
                               pending=runner.pending_patch, history=runner.history))
        claim = next(c for c in runner.run.claims if c["claim_id"] == "a")

        result = runner.collect_phase6_revision([dict(claim_id="a", text=claim["text"])])

        self.assertTrue(result["blocked"])
        self.assertEqual(result["status"], "blocked:revision_no_changes")
        self.assertEqual(result["findings"], findings)
        self.assertEqual(runner.revision_round, 0)
        self.assertEqual(before, dict(claims=runner.run.claims, receipts=runner.receipts,
                                     deep=runner.deep_receipts, global_review=runner.global_receipt,
                                     pending=runner.pending_patch, history=runner.history))
        self.assertNotIn("report_markdown", result)

    def test_noop_patch_does_not_discard_descendant_receipts(self):
        runner, _ = self.prepared()
        receipts_before = deepcopy(runner.receipts)
        original_patch = runner.orch.patch
        with patch.object(runner.orch, "patch", wraps=original_patch) as apply_patch:
            claim = next(c for c in runner.run.claims if c["claim_id"] == "a")
            runner.collect_phase6_revision([dict(claim_id="a", text=claim["text"])])

        self.assertEqual(apply_patch.call_count, 1)
        self.assertEqual(runner.receipts, receipts_before)
        self.assertEqual(runner.revision_round, 0)

    def test_partial_patch_keeps_sibling_global_findings_for_fresh_sweep(self):
        runner = seeded()
        review_all(runner)
        findings = [
            dict(finding_id="global.a", issue="repair A", claim_ids=["a"]),
            dict(finding_id="global.b", issue="repair B", claim_ids=["b"]),
        ]
        runner.prepare_phase6_revision_v4(findings)

        result = runner.collect_phase6_revision([dict(claim_id="a", text="fixed A")])

        self.assertEqual(runner.revision_round, 1)
        self.assertEqual(result["revalidate_claim_ids"], ["a", "b", "c"])
        self.assertEqual({f["finding_id"] for f in runner.deferred_global_findings},
                         {"global.b"})
        self.assertEqual([f["finding_id"] for f in runner.history[-1]["invalidated_global_findings"]],
                         ["global.a"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "partial-patch.json"
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
            self.assertEqual({f["finding_id"] for f in restored.deferred_global_findings},
                             {"global.b"})

    def test_pipeline_nonempty_patch_contract_is_preserved(self):
        runner, _ = self.prepared()
        with self.assertRaisesRegex(ValueError, "nonempty claim patches"):
            runner.orch.patch(runner.run_id, [], ["a"])

    def test_prior_clean_global_receipt_cannot_release_pending_repairs(self):
        runner = seeded()
        review_all(runner)
        runner.prepare_phase6_global_sweep()
        runner.collect_phase6_global_sweep(global_verdict(runner), model_identity="test/global")
        runner.prepare_phase6_revision_v4([dict(claim_id="a", issue="New finding")])
        runner.collect_phase6_revision([])
        with self.assertRaisesRegex(ValueError, "pending claim repairs"):
            runner.finalize()

    def test_execute_returns_empty_patch_block_without_repeating_patcher(self):
        runner = seeded()
        verifier = CloudDouble()
        deep = CloudDouble("test/deep")
        patcher = CloudDouble("test/patcher", lambda _: [])
        finding = dict(claim_ids=["a"], issue="Unresolved global assertion", evidence="fixture", suggested_fix="Correct assertion")
        global_reviewer = CloudDouble("test/global", lambda _: [finding])
        result = runner.execute_phase6(verifier, deep, patcher, global_reviewer)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["status"], "blocked:revision_no_changes")
        self.assertEqual(len(patcher.calls), 1)
        self.assertEqual(len(global_reviewer.calls), 1)
        self.assertEqual(runner.revision_round, 0)
        self.assertEqual(runner.history, [])
        self.assertTrue(result["findings"])
        self.assertNotIn("report_markdown", result)

    def test_pending_empty_revision_survives_reload_without_cloud_retry(self):
        runner = seeded()
        review_all(runner)
        runner.prepare_phase6_global_sweep()
        runner.collect_phase6_global_sweep(global_verdict(runner), model_identity="test/global")
        findings = [dict(claim_id="a", issue="Manually prepared repair after clean global review")]
        runner.prepare_phase6_revision_v4(findings)
        runner.collect_phase6_revision([])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            save_checkpoint(runner, path)
            for continuation in (True, False):
                restored = load_checkpoint(path, continue_verification=continuation)
                self.assertEqual(restored.pending_patch, runner.pending_patch)
                client = CloudDouble("test/unused", lambda _: self.fail("pending repair must not invoke cloud"))
                for result in (restored.execute_phase6(client, client, client, client),
                               restored.resume(client, client, client, client)):
                    self.assertTrue(result["blocked"])
                    self.assertEqual(result["status"], "blocked:revision_no_changes")
                    self.assertEqual(result["findings"], findings)
                    self.assertNotIn("report_markdown", result)
                self.assertEqual(restored.revision_round, 0)
                self.assertEqual(restored.history, [])
                self.assertEqual(client.calls, [])

    def test_checkpoint_rejects_corrupt_or_missing_pending_repair(self):
        runner, _ = self.prepared()
        runner.collect_phase6_revision([])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            save_checkpoint(runner, path)
            original = json.loads(path.read_text())
            invalid = [None, {}, {**runner.pending_patch, "input_hash": "stale"},
                       {**runner.pending_patch, "ids": ["missing"]},
                       {**runner.pending_patch, "ids": ["a", "a"]},
                       {**runner.pending_patch, "findings": []},
                       {**runner.pending_patch, "findings": [dict(claim_id="b", issue="Different target")]},
                       {**runner.pending_patch, "findings": [dict(claim_id="a", issue="")]},
                       {**runner.pending_patch, "findings": [dict(claim_ids="a", issue="Malformed IDs")]}]
            for pending in invalid:
                with self.subTest(pending=pending):
                    data = deepcopy(original)
                    data["pending_patch"] = pending
                    path.write_text(json.dumps(data))
                    with self.assertRaisesRegex(ValueError, "pending claim repair"):
                        load_checkpoint(path)

    def test_preparation_persists_finding_before_patcher_is_called(self):
        runner = seeded()
        review_all(runner)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            with patch.object(runner, "_persist", side_effect=lambda: save_checkpoint(runner, path)):
                runner.prepare_phase6_revision_v4([dict(claim_id="a", issue="Needs explicit repair")])
            restored = load_checkpoint(path)
            self.assertEqual(restored.pending_patch, runner.pending_patch)
            self.assertEqual(restored.status, "blocked:pending_revision")

    def test_revision_limit_preserves_manual_repair_without_resetting_budget(self):
        runner = seeded()
        runner.max_revisions = runner.revision_round = 5
        review_all(runner)
        findings = [dict(claim_id="a", issue="Correct the premise")]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "limited.json"
            with patch.object(runner, "_persist", side_effect=lambda: save_checkpoint(runner, path)):
                result = runner.prepare_phase6_revision_v4(findings)
            self.assertEqual(result["status"], "blocked:revision_limit")
            restored = load_checkpoint(path)
            self.assertEqual(restored.pending_patch["findings"], findings)
            self.assertEqual(restored.pending_patch["ids"], ["a", "b", "c"])
            self.assertEqual((restored.revision_round, restored.max_revisions), (5, 5))
            clients = [CloudDouble() for _ in range(4)]
            paused = restored.execute_phase6(*clients)
            self.assertTrue(paused["blocked"])
            self.assertTrue(all(not c.calls for c in clients))
            rc, result, repaired, clients = self.run_patch_cli(
                restored, json.dumps([dict(claim_id="a", text="Corrected premise")]), Path(temp) / "cli")
        self.assertEqual(rc, 0, result)
        self.assertEqual((repaired.revision_round, repaired.max_revisions), (6, 5))
        self.assertEqual(len(clients["global_reviewer"].calls), 1)
        self.assertFalse(clients["author"].calls)

    def test_manual_repair_at_limit_does_not_bypass_new_global_findings(self):
        runner, _ = self.prepared()
        runner.revision_round = runner.max_revisions
        with tempfile.TemporaryDirectory() as temp:
            rc, result, restored, clients = self.run_patch_cli(
                runner, json.dumps([dict(claim_id="a", text="Corrected premise")]), temp,
                global_findings=[dict(claim_id="b", issue="Still uses an unsupported premise")])
        self.assertEqual(rc, 2)
        self.assertEqual(result["status"], "blocked:revision_limit")
        self.assertEqual(restored.pending_patch["ids"], ["b", "c"])
        self.assertEqual(restored.revision_round, runner.max_revisions + 1)
        self.assertEqual(restored.max_revisions, runner.max_revisions)
        self.assertEqual(len(clients["global_reviewer"].calls), 1)
        self.assertFalse(clients["author"].calls)

    def test_legacy_revision_limit_recovers_only_current_mapped_global_findings(self):
        runner = seeded()
        review_all(runner)
        runner.prepare_phase6_global_sweep()
        runner.collect_phase6_global_sweep(global_verdict(runner, [dict(claim_id="a", issue="Repair premise")]),
                                           model_identity="test/global")
        runner.status = "blocked:revision_limit"
        runner.revision_round = runner.max_revisions
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.json"
            save_checkpoint(runner, path)
            data = json.loads(path.read_text())
            restored = load_checkpoint(path)
            self.assertEqual(restored.pending_patch["ids"], ["a"])
            self.assertIsNone(restored.global_receipt)  # diagnostics never become verification
            self.assertEqual(restored.receipts, {})
            for field in ("input_hash", "history_hash"):
                stale = deepcopy(data)
                stale["audit"]["global_review"][field] = "0" * 64
                path.write_text(json.dumps(stale))
                self.assertIsNone(load_checkpoint(path).pending_patch)
            for finding in (dict(issue="Unmapped"), dict(claim_id="a", issue="External", resolution_route="human_decision")):
                excluded = deepcopy(data)
                excluded["audit"]["global_review"]["findings"] = [finding]
                path.write_text(json.dumps(excluded))
                self.assertIsNone(load_checkpoint(path).pending_patch)

    def test_global_dependency_edges_apply_together_without_content_patcher(self):
        runner = seeded()
        targets = [c["claim_id"] for c in runner.run.claims if c["claim_id"].startswith("role.")][:5]
        def sweep(_):
            response = global_verdict(runner)
            for row in response["dependency_reviews"]:
                if row["claim_id"] in targets:
                    row["required_dependencies"] = ["a", "b"]
            return response
        patcher = CloudDouble("test/author", lambda _: self.fail("structured edges need no content patcher"))
        reviewer = CloudDouble("test/global", sweep)
        result = runner.execute_phase6(CloudDouble(), CloudDouble("test/deep"), patcher, reviewer)
        self.assertFalse(result["blocked"], result)
        self.assertEqual(runner.revision_round, 1)
        self.assertEqual({p["claim_id"] for p in runner.history[0]["patches"]}, set(targets))
        self.assertEqual(len(reviewer.calls), 2)
        self.assertFalse(patcher.calls)

    def test_cli_applies_pending_patches_then_revalidates_and_runs_global_sweep(self):
        runner, _ = self.prepared()
        patches = [dict(claim_id="a", text="Corrected canonical Claim")]
        with tempfile.TemporaryDirectory() as temp:
            rc, result, restored, clients = self.run_patch_cli(runner, json.dumps(patches), temp)
        self.assertEqual(rc, 0, result)
        self.assertFalse(result["blocked"])
        self.assertIsNone(restored.pending_patch)
        self.assertEqual(next(c["text"] for c in restored.run.claims if c["claim_id"] == "a"), patches[0]["text"])
        self.assertEqual(restored.revision_round, 1)
        self.assertEqual(restored.history[-1]["patches"], patches)
        self.assertEqual(len(clients["verifier"].calls), len(runner.run.claims))
        self.assertEqual(len(clients["global_reviewer"].calls), 1)
        self.assertEqual(clients["author"].calls, [])
        self.assertEqual(set(result["sidecar"]["atomic_reviews"]), {c["claim_id"] for c in runner.run.claims})

    def test_cli_empty_or_noop_patch_remains_pending_without_cloud_calls(self):
        for patches in ([], [dict(claim_id="a", text="任意論述")]):
            with self.subTest(patches=patches), tempfile.TemporaryDirectory() as temp:
                runner, findings = self.prepared()
                before_hash = runner._hash()
                rc, result, restored, clients = self.run_patch_cli(runner, json.dumps(patches), temp)
                self.assertEqual(rc, 2)
                self.assertEqual(result["status"], "blocked:revision_no_changes")
                self.assertEqual(result["findings"], findings)
                self.assertEqual(restored._hash(), before_hash)
                self.assertEqual(restored.revision_round, 0)
                self.assertEqual(restored.pending_patch, runner.pending_patch)
                self.assertEqual(restored.history, [])
                self.assertTrue(all(client.calls == [] for client in clients.values()))

    def test_cli_invalid_patch_file_preserves_pending_state_without_cloud_calls(self):
        invalid = ["not JSON", "{}", '[{"claim_id":"a","claim_id":"b","text":"ambiguous"}]',
                   '[{"claim_id":"a","confidence":NaN}]',
                   '[{"claim_id":"unknown","text":"unauthorized"}]',
                   '[{"claim_id":"a","text":"first"},{"claim_id":"a","text":"second"}]']
        for patch_text in invalid:
            with self.subTest(patch_text=patch_text), tempfile.TemporaryDirectory() as temp:
                runner, findings = self.prepared()
                before_hash = runner._hash()
                rc, result, restored, clients = self.run_patch_cli(runner, patch_text, temp)
                self.assertEqual(rc, 2)
                self.assertEqual(result["status"], "blocked:revision_patch")
                self.assertEqual(result["findings"], findings)
                self.assertEqual(restored._hash(), before_hash)
                self.assertEqual(restored.pending_patch, runner.pending_patch)
                self.assertEqual(restored.history, [])
                self.assertTrue(all(client.calls == [] for client in clients.values()))

    def test_cli_apply_patches_requires_resume(self):
        from .__main__ import main
        args = ["--manifest", "unused.json", "--apply-patches", "unused-patches.json", "--allow-ephemeral-out", "--out", "unused",
                "--author-model", "author", "--verifier-model", "verifier", "--deep-model", "deep",
                "--global-model", "global"]
        with patch("v4.__main__.AntigravityCloudClient") as clients, \
             contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as caught:
            main(args)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--apply-patches requires --resume", errors.getvalue())
        clients.assert_not_called()

    def test_wait_patch_replaces_stale_risk_claims_transactionally_with_explicit_dependent_scope(self):
        runner = seeded()
        trade = dict(direction="SHORT", entry=173.16, stop=184, target=148, position_pct=5)
        runner.orch.register_trade(runner.run_id, trade)
        plan_id, risk_id = "role.trader", "role.risk_manager"
        for claim in runner.run.claims:
            if claim["claim_id"] == plan_id:
                claim.update(text="SHORT execution plan", dependencies=["role.research_manager"],
                             assertion=dict(kind="trade_plan", trade=trade))
            elif claim["claim_id"] == risk_id:
                fact = runner.run.fact_store.get("fact.Trade.Entry").to_dict()
                claim.update(text="Approve the original SHORT plan", dependencies=[plan_id],
                             refs=[fact["fact_id"]], fact_refs=[fact["fact_id"]],
                             bindings=[dict(ref_id=fact["fact_id"], **{
                                 key: fact.get(key) or "unknown"
                                 for key in ("entity", "concept", "period", "scope", "unit", "basis")})])
        runner.orch._sync_views(runner.run)
        review_all(runner)
        runner.prepare_phase6_revision_v4([dict(claim_id=plan_id, issue="Trader must follow RM WAIT")])
        # Recreate the narrow pending scope stored by older versions.
        runner.pending_patch.pop("dependent_candidate_ids", None)
        runner.pending_patch["ids"] = [plan_id]
        runner.pending_patch["findings"] = [dict(claim_id=plan_id, issue="Trader must follow RM WAIT")]
        patches = [dict(claim_id=plan_id, text="WAIT; no new position", assertion=dict(kind="trade_plan", trade=dict(direction="WAIT"))),
                   dict(claim_id=risk_id, text="WAIT acknowledged; the old SHORT approval no longer applies",
                        refs=[], fact_refs=[], bindings=[])]
        for authorized in (False, True):
            with self.subTest(patch_dependents=authorized), tempfile.TemporaryDirectory() as temp:
                rc, result, restored, clients = self.run_patch_cli(
                    runner, json.dumps(patches), temp, patch_dependents=authorized)
                if not authorized:
                    self.assertEqual(rc, 2)
                    self.assertEqual(restored.run.trade, trade)
                    self.assertEqual(restored.pending_patch, runner.pending_patch)
                    self.assertTrue(all(client.calls == [] for client in clients.values()))
                    continue
                self.assertEqual(rc, 0, result)
                self.assertEqual(restored.run.trade, {"direction": "WAIT"})
                self.assertEqual(list(restored.run.trade_generated), [[], []])
                self.assertFalse(set(runner.run.trade_generated[0]) & restored.run.fact_store.to_dict().keys())
                self.assertFalse(set(runner.run.trade_generated[1]) & restored.run.calc_store.to_dict().keys())
                risk = next(c for c in restored.run.claims if c["claim_id"] == risk_id)
                self.assertEqual(risk["dependencies"], [plan_id])
                self.assertEqual(set(restored.history[-1]["affected_claim_ids"]), {plan_id, risk_id})
                self.assertEqual(restored.history[-1]["repair_scope"],
                                 dict(requested_claim_ids=[plan_id], include_dependents=True,
                                      allowed_claim_ids=[plan_id, risk_id]))
                self.assertEqual(len(clients["global_reviewer"].calls), 1)
                self.assertEqual(clients["author"].calls, [])

    def test_dependent_scope_does_not_allow_unrelated_claim_edits(self):
        runner, _ = self.prepared()
        with tempfile.TemporaryDirectory() as temp:
            rc, result, restored, clients = self.run_patch_cli(
                runner, '[{"claim_id":"independent","text":"not related"}]', temp, patch_dependents=True)
        self.assertEqual(rc, 2)
        self.assertEqual(result["status"], "blocked:revision_patch")
        self.assertEqual(restored._hash(), runner._hash())
        self.assertEqual(restored.pending_patch, runner.pending_patch)
        self.assertTrue(all(client.calls == [] for client in clients.values()))

    def test_new_trade_revision_prepares_direct_dependents_without_expanding_to_whole_report(self):
        runner = seeded()
        plan_id, risk_id = "role.trader", "role.risk_manager"
        next(c for c in runner.run.claims if c["claim_id"] == risk_id)["dependencies"] = [plan_id]
        runner.orch._sync_views(runner.run)
        review_all(runner)

        revision = runner.prepare_phase6_revision_v4([dict(claim_id=plan_id, issue="Reconcile trade decision")])

        self.assertEqual(revision["allowed_claim_ids"], [plan_id, risk_id])
        self.assertEqual(runner.pending_patch["findings"][-1]["claim_id"], risk_id)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scoped.json"
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
        self.assertEqual(restored.pending_patch, runner.pending_patch)

    def test_mixed_noop_and_effective_patches_keep_valid_reloadable_history(self):
        runner, _ = self.prepared()
        next(c for c in runner.run.claims if c["claim_id"] == "b")["dependencies"] = ["a"]
        patches = [dict(claim_id="a", text="corrected"), dict(claim_id="b", text="任意論述")]
        runner.collect_phase6_revision(patches, include_dependents=True)
        self.assertEqual(runner.history[-1]["patches"], patches[:1])
        self.assertEqual(runner.history[-1]["submitted_patches"], patches)
        self.assertEqual(runner.revision_round, 1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mixed.json"
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
        self.assertEqual(restored.history, runner.history)

    def test_trade_resource_consumer_is_prepared_even_without_claim_dependency(self):
        runner = seeded()
        trade = dict(direction="SHORT", entry=100, stop=110, target=80, position_pct=5)
        runner.orch.register_trade(runner.run_id, trade)
        fact = runner.run.fact_store.get("fact.Trade.Entry").to_dict()
        for claim in runner.run.claims:
            if claim["claim_id"] == "role.trader":
                claim["assertion"] = dict(kind="trade_plan", trade=trade)
            elif claim["claim_id"] == "role.risk_manager":
                claim.update(refs=[fact["fact_id"]], fact_refs=[fact["fact_id"]], bindings=[
                    dict(ref_id=fact["fact_id"], **{key: fact.get(key) or "unknown"
                        for key in ("entity", "concept", "period", "scope", "unit", "basis")})])
        runner.orch._sync_views(runner.run)
        review_all(runner)
        result = runner.prepare_phase6_revision_v4([dict(claim_id="role.trader", issue="Cancel plan")])
        self.assertEqual(result["allowed_claim_ids"], ["role.trader", "role.risk_manager"])
        self.assertEqual(runner.pending_patch["findings"][-1]["evidence"]["generated_source_refs"],
                         ["fact.Trade.Entry"])

    def test_applied_patch_is_saved_before_subsequent_verifier_failure(self):
        runner, _ = self.prepared()
        patches = [dict(claim_id="a", text="durably corrected")]
        with tempfile.TemporaryDirectory() as temp:
            def interrupted(_):
                saved = json.loads((Path(temp) / "out" / "checkpoint.json").read_text())
                self.assertIsNone(saved["pending_patch"])
                self.assertEqual(saved["history"][-1]["patches"], patches)
                self.assertEqual(saved["state"]["stage"], "atomic")
                raise RuntimeError("verification interrupted after repair")
            rc, result, restored, _ = self.run_patch_cli(
                runner, json.dumps(patches), temp, verifier=CloudDouble("test/verifier", interrupted))
        self.assertEqual(rc, 2)
        self.assertEqual(result["status"], "blocked:execution")
        self.assertIn("verification interrupted after repair", result["reason"])
        self.assertIsNone(restored.pending_patch)
        self.assertEqual(restored.revision_round, 1)
        self.assertEqual(next(c["text"] for c in restored.run.claims if c["claim_id"] == "a"),
                         "durably corrected")


if __name__ == "__main__":
    unittest.main()
