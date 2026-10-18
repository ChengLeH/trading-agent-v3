"""Empty patch responses block unresolved revisions without consuming work."""
from copy import deepcopy
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from .test_integration import CloudDouble, seeded, review_all, global_verdict
from .checkpoint import save_checkpoint, load_checkpoint


class EmptyRevision(unittest.TestCase):
    def prepared(self):
        runner = seeded()
        review_all(runner)
        findings = [dict(claim_id="a", issue="Unresolved assertion", evidence="fixture", suggested_fix="Correct assertion")]
        runner.prepare_phase6_revision_v4(findings)
        return runner, findings

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
                         {"global.a", "global.b"})
        self.assertEqual([f["finding_id"] for f in runner.history[-1]["invalidated_global_findings"]],
                         ["global.a"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "partial-patch.json"
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
            self.assertEqual({f["finding_id"] for f in restored.deferred_global_findings},
                             {"global.a", "global.b"})

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


if __name__ == "__main__":
    unittest.main()
