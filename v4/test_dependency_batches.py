"""Regression coverage for scoped, out-of-order global dependency chunks."""
import copy
import tempfile
import unittest

from .agent_queue import NativeWorkQueue
from .native_dependency_batches import NativeDependencyBatchMixin
from .schema import digest
from .test_integration import global_verdict, review_all, seeded


class _BatchHarness(NativeDependencyBatchMixin):
    def __init__(self, runner, directory):
        self.runner = copy.deepcopy(runner)
        self.queue = NativeWorkQueue(directory)
        self.queue.create({}, runner._hash())

    def _load(self, state):
        return copy.deepcopy(self.runner)

    def _save(self, state, runner):
        self.runner = copy.deepcopy(runner)
        state["checkpoint"] = {}
        state["input_hash"] = runner._hash()

    def next(self):
        raise AssertionError("parallel dependency claiming must not call next()")


class DependencyBatches(unittest.TestCase):
    def _start_partial_global(self, *, batch_size=2):
        runner = seeded()
        runner.global_dependency_batch_size = batch_size
        review_all(runner)
        runner.prepare_phase6_global_sweep()
        response = global_verdict(runner)
        primary_claim_ids = [row["claim_id"] for row in response["dependency_reviews"][:2]]
        response["dependency_reviews"] = [row for row in response["dependency_reviews"]
                                           if row["claim_id"] in primary_claim_ids]
        raw_response = copy.deepcopy(response)
        runner.collect_phase6_global_sweep(response, model_identity="test/global")
        return runner, raw_response, primary_claim_ids

    def test_scoped_chunks_accept_out_of_order_and_preserve_both_reviewer_identities(self):
        runner, primary_response, primary_ids = self._start_partial_global(batch_size=2)
        self.assertEqual(runner.pending_global["sweep_response_hash"], digest(primary_response))
        missing = runner._global_coverage_status()["missing_dependency_claim_ids"]
        chunks = [missing[index:index + 2] for index in range(0, len(missing), 2)]
        prepared = {}
        for chunk in chunks:
            role, prompt = runner.prepare_phase6_global_sweep(dependency_claim_ids=chunk)
            self.assertEqual(role, "global_dependency_completion")
            self.assertIn("resolved_external_findings 必須回傳空陣列 []", prompt)
            prepared[tuple(chunk)] = prompt

        base = global_verdict(runner)
        for index, chunk in reversed(list(enumerate(chunks))):
            response = dict(findings=[], resolved_external_findings=[],
                            dependency_reviews=[row for row in base["dependency_reviews"]
                                                if row["claim_id"] in chunk])
            outcome = runner.collect_phase6_global_sweep(
                response, model_identity="test/global",
                dependency_reviewer=f"test/dependency-{index}", dependency_claim_ids=chunk)
            if index != 0:
                self.assertIsNone(runner.global_receipt)
                self.assertTrue(outcome["blocked"])

        self.assertIsNone(runner.pending_global)
        receipt = runner.global_receipt
        self.assertEqual(receipt["model"], "test/global")
        self.assertEqual(receipt["sweep_response_hash"], digest(primary_response))
        self.assertEqual({cid: receipt["dependency_reviewers"][cid] for cid in primary_ids},
                         {cid: "test/global" for cid in primary_ids})
        for index, chunk in enumerate(chunks):
            self.assertEqual({receipt["dependency_reviewers"][cid] for cid in chunk},
                             {f"test/dependency-{index}"})
        self.assertFalse(receipt["findings"])

    def test_scoped_chunk_cannot_add_global_findings_or_escape_issued_claims(self):
        runner, _, _ = self._start_partial_global(batch_size=1)
        target = runner._global_coverage_status()["missing_dependency_claim_ids"][0]
        _, prompt = runner.prepare_phase6_global_sweep(dependency_claim_ids=[target])
        self.assertIn("findings 必須回傳空陣列 []", prompt)
        all_rows = global_verdict(runner)["dependency_reviews"]
        extra = next(row for row in all_rows if row["claim_id"] != target)

        with self.assertRaisesRegex(ValueError, "must not return findings"):
            runner.collect_phase6_global_sweep(
                dict(findings=[dict(issue="unscoped")], dependency_reviews=[]),
                model_identity="test/global", dependency_reviewer="test/chunk",
                dependency_claim_ids=[target])
        with self.assertRaisesRegex(ValueError, "unrequested"):
            runner.collect_phase6_global_sweep(
                dict(findings=[], dependency_reviews=[extra]), model_identity="test/global",
                dependency_reviewer="test/chunk", dependency_claim_ids=[target])

        self.assertIn(target, runner._global_coverage_status()["missing_dependency_claim_ids"])
        self.assertIsNone(runner.global_receipt)
        with self.assertRaisesRegex(ValueError, "global sweep still pending"):
            runner.finalize()

    def test_native_claims_create_distinct_leased_chunks_and_wait_when_all_are_held(self):
        runner, _, _ = self._start_partial_global(batch_size=2)
        with tempfile.TemporaryDirectory() as directory:
            workflow = _BatchHarness(runner, directory)
            claimed = []
            while True:
                result = workflow.claim_dependency_reviews("host/reviewer", max_items=2)
                if result["status"] == "global_dependency_waiting":
                    break
                self.assertEqual(result["status"], "dependency_reviews_claimed")
                claimed.append(result)
            self.assertGreater(len(claimed), 1)
            packet_ids = [row["packet_id"] for row in claimed]
            self.assertEqual(len(packet_ids), len(set(packet_ids)))
            all_targets = [cid for row in claimed for cid in row["target_claim_ids"]]
            self.assertEqual(len(all_targets), len(set(all_targets)))
            self.assertEqual(set(all_targets), set(runner._global_coverage_status()[
                "missing_dependency_claim_ids"]))
            for row in claimed:
                self.assertTrue(row["global_context"]["sweep_response_hash"])
                self.assertNotIn("coverage_round", row["global_context"])
                self.assertEqual(row["lease"]["owner"], "host/reviewer")
            with workflow.queue.transaction() as state:
                packets = [state["packets"][pid] for pid in packet_ids]
                self.assertTrue(all(packet.get("parallel_dependency") is True for packet in packets))
                self.assertTrue(all(packet["kind"] == "global" for packet in packets))

    def test_native_claims_respect_current_dispatch_failure_exclusions(self):
        runner, _, _ = self._start_partial_global(batch_size=2)
        missing = runner._global_coverage_status()["missing_dependency_claim_ids"]
        with tempfile.TemporaryDirectory() as directory:
            workflow = _BatchHarness(runner, directory)
            result = workflow.claim_dependency_reviews(
                "host/reviewer", max_items=2, exclude_claim_ids=missing)
            self.assertEqual(result["status"], "global_dependency_waiting")
            self.assertEqual(result["excluded_claim_ids"], missing)
            with workflow.queue.transaction() as state:
                self.assertFalse(state["packets"])


if __name__ == "__main__":
    unittest.main()
