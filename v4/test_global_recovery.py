"""Global recovery preparation reuses one stable snapshot and archive index."""
import json
import unittest
from unittest.mock import patch

import v4.runner as runner_module
from .test_integration import seeded, review_all
from .schema import digest


class _AttemptJournal:
    def __init__(self, attempts):
        self.attempts = attempts
        self.reads = 0

    def received_attempts(self, run_id):
        self.reads += 1
        return self.attempts


class GlobalRecoveryPreparation(unittest.TestCase):
    @staticmethod
    def _payload(prompt):
        return json.loads(prompt.split("<context-data>\n", 1)[1].split("\n</context-data>", 1)[0])

    def test_global_prompt_reuses_snapshot_tasks_and_received_attempts(self):
        runner = seeded()
        review_all(runner)
        tasks = runner._tasks()
        a, b = "a", "b"
        attempts = [
            dict(attempt_id="attempt.one", task_id="atomic.batch", model="host/reviewer",
                 review=dict(layer="atomic", task_hashes={a: digest(tasks[a].to_dict()),
                                                          b: digest(tasks[b].to_dict())})),
            dict(attempt_id="attempt.two", task_id="atomic.batch", model="host/reviewer",
                 review=dict(layer="deep", task_hashes={b: digest(tasks[b].to_dict())})),
        ]
        journal = _AttemptJournal(attempts)
        runner.journal = journal
        runner.deferred_global_findings = [
            dict(finding_id="transport.a", issue="transport result for a", claim_ids=[a],
                 resolution_route="transport_recovery",
                 resolution_baseline=dict(trusted=True, transport_attempt_ids=["attempt.two"])),
            dict(finding_id="transport.b", issue="transport result for b", claim_ids=[b],
                 resolution_route="transport_recovery",
                 resolution_baseline=dict(trusted=True, transport_attempt_ids=[])),
        ]

        original_builder = runner_module.build_atomic_tasks
        builds = []

        def counted_builder(run):
            builds.append(None)
            return original_builder(run)

        original_snapshot = runner.orch.snapshot
        with patch("v4.runner.build_atomic_tasks", side_effect=counted_builder), \
                patch.object(runner.orch, "snapshot", wraps=original_snapshot) as snapshots:
            role, prompt = runner.prepare_phase6_global_sweep()

        self.assertEqual(role, "global_consistency_reviewer")
        payload = json.loads(prompt.split("<context-data>\n", 1)[1].split("\n</context-data>", 1)[0])
        self.assertEqual(len(builds), 1, builds)
        self.assertEqual(snapshots.call_count, 1, snapshots.call_count)
        self.assertEqual(journal.reads, 1, journal.reads)
        self.assertEqual(payload["transport_recovery_records"], [
            dict(attempt_id="attempt.one", task_id="atomic.batch", model="host/reviewer",
                 claim_ids=[b], layer="atomic"),
            dict(attempt_id="attempt.two", task_id="atomic.batch", model="host/reviewer",
                 claim_ids=[b], layer="deep"),
        ])
        self.assertEqual(payload["external_resolution_candidates"], [
            dict(finding_id="transport.a", resolution_route="transport_recovery",
                 transport_attempt_ids=["attempt.one"]),
            dict(finding_id="transport.b", resolution_route="transport_recovery",
                 transport_attempt_ids=["attempt.one", "attempt.two"]),
        ])

    def test_dependency_completion_reuses_snapshot_and_archive_index(self):
        runner = seeded()
        review_all(runner)
        tasks = runner._tasks()
        claim_id = "a"
        finding = dict(finding_id="transport.a", issue="transport result for a", claim_ids=[claim_id],
                       resolution_route="transport_recovery",
                       resolution_baseline=dict(trusted=True, transport_attempt_ids=[]))
        runner.deferred_global_findings = [finding]
        runner.prepare_phase6_global_sweep()
        response = dict(findings=[], dependency_reviews=[
            dict(claim_id=claim_id, required_dependencies=[], unresolved=False, reason="fixture")])
        result = runner.collect_phase6_global_sweep(response, model_identity="test/global")
        self.assertTrue(result["needs_dependency_completion"])

        attempts = [dict(attempt_id="attempt.new", task_id="atomic.batch", model="host/reviewer",
                         review=dict(layer="atomic", task_hashes={claim_id: digest(tasks[claim_id].to_dict())}))]
        journal = _AttemptJournal(attempts)
        runner.journal = journal
        original_builder = runner_module.build_atomic_tasks
        builds = []

        def counted_builder(run):
            builds.append(None)
            return original_builder(run)

        original_snapshot = runner.orch.snapshot
        with patch("v4.runner.build_atomic_tasks", side_effect=counted_builder), \
                patch.object(runner.orch, "snapshot", wraps=original_snapshot) as snapshots:
            role, prompt = runner.prepare_phase6_global_sweep()

        payload = self._payload(prompt)
        self.assertEqual(role, "global_dependency_completion")
        self.assertEqual(len(builds), 1, builds)
        self.assertEqual(snapshots.call_count, 1, snapshots.call_count)
        self.assertEqual(journal.reads, 1, journal.reads)
        self.assertEqual(payload["transport_recovery_records"], [
            dict(attempt_id="attempt.new", task_id="atomic.batch", model="host/reviewer",
                 claim_ids=[claim_id], layer="atomic")])
        self.assertEqual(payload["external_resolution_candidates"], [
            dict(finding_id="transport.a", resolution_route="transport_recovery",
                 transport_attempt_ids=["attempt.new"])])


if __name__ == "__main__":
    unittest.main()
