"""Scoped deep review may overlap unfinished atomic review without weakening global gates."""
import unittest

from .test_integration import reference_ids, seeded, verdict


class ReviewOverlap(unittest.TestCase):
    def _collect_atomic(self, runner, statuses):
        runner.prepare_phase6_atomic()
        tasks = runner._tasks()
        return runner.collect_phase6_atomic(
            {cid: verdict(cid, status, ref_ids=reference_ids(tasks[cid].claim))
             for cid, status in statuses.items()},
            model_identity="test/first",
            allow_partial=True,
        )

    def _deep_verdict(self, runner, cid):
        return verdict(cid, ref_ids=reference_ids(runner._tasks()[cid].claim))

    def test_requested_ready_claim_can_deep_review_while_atomic_siblings_are_pending(self):
        runner = seeded()
        self._collect_atomic(runner, {"a": "SUSPICIOUS"})

        prepared = runner.prepare_phase6_deep_review(["a"])
        self.assertEqual(prepared["deep_review_claim_ids"], ["a"])
        self.assertEqual([cid for cid, _ in prepared["deep_prompts"]], ["a"])

        result = runner.collect_phase6_deep_review(
            {"a": self._deep_verdict(runner, "a")}, model_identity="test/deep")

        self.assertFalse(result["blocked"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["partial"])
        self.assertEqual(runner.status, "atomic")
        self.assertIn("a", runner.deep_receipts)
        self.assertEqual(set(result["pending_atomic_claim_ids"]), set(runner._tasks()) - {"a"})
        self.assertEqual(result["pending_deep_claim_ids"], [])
        with self.assertRaisesRegex(ValueError, "all current claims require semantic verification"):
            runner.prepare_phase6_global_sweep()

    def test_partial_deep_collection_preserves_unreceived_scope_and_atomic_status(self):
        runner = seeded()
        self._collect_atomic(runner, {"a": "SUSPICIOUS", "b": "FAIL"})
        runner.prepare_phase6_deep_review(["a", "b"])

        first = runner.collect_phase6_deep_review(
            {"a": self._deep_verdict(runner, "a")},
            model_identity="test/deep", allow_partial=True)
        self.assertIn("a", runner.deep_receipts)
        self.assertNotIn("b", runner.deep_receipts)
        self.assertEqual(set(runner.pending_deep), {"b"})
        self.assertEqual(first["pending_deep_claim_ids"], ["b"])
        self.assertEqual(runner.status, "atomic")

        second = runner.collect_phase6_deep_review(
            {"b": self._deep_verdict(runner, "b")},
            model_identity="test/deep", allow_partial=True)
        self.assertIn("b", runner.deep_receipts)
        self.assertIsNone(runner.pending_deep)
        self.assertEqual(runner.status, "atomic")
        self.assertEqual(set(second["pending_atomic_claim_ids"]), set(runner._tasks()) - {"a", "b"})
        with self.assertRaisesRegex(ValueError, "all current claims require semantic verification"):
            runner.prepare_phase6_global_sweep()

    def test_deep_review_rejects_pass_and_default_scope_keeps_full_atomic_gate(self):
        runner = seeded()
        self._collect_atomic(runner, {"a": "PASS"})
        with self.assertRaisesRegex(ValueError, "SUSPICIOUS/FAIL"):
            runner.prepare_phase6_deep_review(["a"])
        with self.assertRaisesRegex(ValueError, "all current claims require semantic verification"):
            runner.prepare_phase6_deep_review()

    def test_global_sweep_still_requires_deep_review_for_every_atomic_nonpass(self):
        runner = seeded()
        self._collect_atomic(runner, {"a": "SUSPICIOUS", "b": "FAIL"})
        tasks = runner._tasks()
        remaining = [cid for cid in tasks if cid not in runner.receipts]
        runner.prepare_phase6_atomic()
        runner.collect_phase6_atomic(
            {cid: verdict(cid, "PASS", ref_ids=reference_ids(tasks[cid].claim))
             for cid in remaining},
            model_identity="test/first", allow_partial=True)

        runner.prepare_phase6_deep_review(["a"])
        deep = runner.collect_phase6_deep_review(
            {"a": self._deep_verdict(runner, "a")}, model_identity="test/deep")
        self.assertEqual(runner.status, "deep")
        self.assertEqual(deep["pending_deep_claim_ids"], ["b"])
        with self.assertRaisesRegex(ValueError, "every non-PASS claim requires a separate deep review"):
            runner.prepare_phase6_global_sweep()
