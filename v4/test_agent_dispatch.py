"""Optional secondary review dispatch: deterministic, local, no paid model calls."""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .agent_dispatch import dispatch_secondary_reviews, dispatch_status
from .agent_reviews import NativeReviewWorkflow
from .test_agent_reviews import NativeReviews
from .test_integration import verdict
from .test_agent_cli import run


class FakeSecondary:
    identity = 'antigravity/fake-reviewer'

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def complete_structured(self, prompt, schema):
        cid = schema["properties"]["claim_id"]["enum"][0]
        self.calls.append(cid)
        if self.fail == cid:
            raise RuntimeError("AGY quota exhausted")
        refs = schema["properties"]["reference_checks"]["items"]["properties"]["ref_id"].get("enum", [])
        return json.dumps(verdict(cid, "PASS", refs))


class SecondaryDispatch(unittest.TestCase):
    def bootstrap(self, root):
        return NativeReviews().bootstrap(root)

    def test_reserved_reviews_are_not_reported_as_dispatched(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            while True:
                reserved = workflow.claim_reviews('another/executor', max_items=16, lease_seconds=3600)
                if reserved['status'] != 'reviews_claimed':
                    self.assertEqual(reserved['status'], 'reviews_in_flight')
                    break
            client = FakeSecondary()
            result = dispatch_secondary_reviews(workflow, client, continuous=True, max_items=16)
            self.assertEqual(result['status'], 'reviews_in_flight')
            self.assertEqual(result['dispatched_claim_count'], 0)
            self.assertEqual(client.calls, [])
            self.assertTrue(result['outstanding']['reservations'])
            self.assertTrue(all(row['execution_state'] == 'unknown'
                                for row in result['outstanding']['reservations']))
            self.assertTrue(all(isinstance(row['claimed_at'], (float, int))
                                for row in result['outstanding']['reservations']))
            status = dispatch_status(workflow.directory, attention_after_seconds=0.000001)
            self.assertTrue(status['state_available'])
            self.assertEqual(len(status['attention_lease_ids']), len(status['reservations']))
            self.assertEqual(status['attempts'], [])

    def test_dispatch_status_keeps_incomplete_call_and_ignores_truncated_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'secondary-dispatch.jsonl'
            path.write_text(json.dumps(dict(request_id='request-1', status='calling', started_at=1))
                            + '\n' + '{"request_id":', encoding='utf-8')
            result = dispatch_status(Path(root), attention_after_seconds=60)
            self.assertEqual(result['attempts'][0]['status'], 'calling')
            self.assertEqual(result['damaged_records'], 1)
            self.assertEqual(result['attention_request_ids'], ['request-1'])
            self.assertGreater(result['attempts'][0]['seconds_since_call_start'], 0)
            self.assertIn('liveness are unknown', result['interpretation'])

    def test_running_dispatch_writes_heartbeat_and_stopped_status(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        started, release = threading.Event(), threading.Event()
        class WaitingClient(FakeSecondary):
            def complete_structured(self, prompt, schema):
                started.set()
                if not release.wait(5):
                    raise RuntimeError('test timed out')
                return super().complete_structured(prompt, schema)
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(dispatch_secondary_reviews, workflow, WaitingClient(),
                                     max_workers=1, max_items=1, progress_interval_seconds=0.03)
                try:
                    self.assertTrue(started.wait(5))
                    deadline = time.monotonic() + 4
                    while True:
                        status = dispatch_status(workflow.directory)
                        rows = status['dispatchers']
                        if rows and rows[0]['status'] == 'running' and rows[0]['active_request_count'] == 1:
                            break
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                finally:
                    release.set()
                self.assertFalse(future.result(timeout=5)['failures'])
            self.assertEqual(dispatch_status(workflow.directory)['dispatchers'][0]['status'], 'stopped')

    def test_observation_write_failure_does_not_discard_review(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with patch('v4.agent_dispatch._append_dispatch_observation', side_effect=OSError('unavailable')):
                result = dispatch_secondary_reviews(workflow, FakeSecondary(), max_workers=1, max_items=1)
            self.assertFalse(result['failures'])
            self.assertEqual(len(result['accepted_claim_ids']), 1)
            self.assertTrue(any(row['code'] == 'dispatch_observability_unavailable'
                                for row in result['warnings']))
            with workflow.queue.transaction() as state:
                self.assertIn(result['accepted_claim_ids'][0], workflow._load(state).receipts)

    def test_concurrent_dispatchers_keep_separate_heartbeats_and_claims(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        release = threading.Event()
        both_started = threading.Barrier(2)
        class WaitingClient(FakeSecondary):
            def complete_structured(self, prompt, schema):
                both_started.wait(timeout=5)
                if not release.wait(5):
                    raise RuntimeError('test timed out')
                return super().complete_structured(prompt, schema)
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(dispatch_secondary_reviews,
                    NativeReviewWorkflow(workflow.directory), WaitingClient(),
                    max_workers=1, max_items=1, progress_interval_seconds=0.03)
                    for _ in range(2)]
                try:
                    deadline = time.monotonic() + 4
                    while True:
                        rows = dispatch_status(workflow.directory)['dispatchers']
                        if len(rows) == 2 and all(row['active_request_count'] == 1 for row in rows):
                            break
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                finally:
                    release.set()
                results = [future.result(timeout=5) for future in futures]
            self.assertTrue(all(not row['failures'] for row in results))
            self.assertFalse(set(results[0]['accepted_claim_ids']) & set(results[1]['accepted_claim_ids']))
            self.assertEqual(len(dispatch_status(workflow.directory)['dispatchers']), 2)

    def test_expired_reservation_cannot_start_model(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            client = FakeSecondary()
            with patch('v4.agent_dispatch.renew_items_in_state', return_value=dict(item_ids=[], expires_at=None)):
                result = dispatch_secondary_reviews(workflow, client, max_workers=1, max_items=1)
            self.assertEqual(result['status'], 'fallback_to_main')
            self.assertEqual(client.calls, [])
            self.assertEqual(result['dispatched_claim_count'], 0)
            self.assertIn('expired before provider dispatch', next(iter(result['failures'].values())))

    def test_existing_legacy_session_is_not_silently_restarted(self):
        from .cloud import structured_prompt
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            client = FakeSecondary()
            client.legacy_session_root = Path(root) / 'legacy'
            client.session_root = workflow.directory / 'secondary-sessions'
            packet = workflow.next()
            item = next(iter(packet['items'].values()))
            schema = json.dumps(item['schema'], ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            wire = structured_prompt(item['prompt'], schema)
            key = hashlib.sha256((client.identity + '\0' + wire).encode()).hexdigest()
            (client.legacy_session_root / key).mkdir(parents=True)
            result = dispatch_secondary_reviews(workflow, client, max_workers=1, max_items=1, batch_size=1)
            self.assertEqual(result['status'], 'fallback_to_main')
            self.assertEqual(client.calls, [])
            self.assertTrue((client.legacy_session_root / key).exists())
            self.assertFalse((client.session_root / key).exists())

    def test_agy_parallel_atomic_accepts_individual_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            client = FakeSecondary()
            result = dispatch_secondary_reviews(workflow, client, max_workers=3)
            self.assertEqual(result["status"], "secondary_complete")
            self.assertEqual(result["kind"], "atomic")
            self.assertEqual(len(result["accepted_claim_ids"]), 8)
            self.assertEqual(len(client.calls), 8)
            observations = [json.loads(line) for line in
                            (workflow.directory / 'secondary-dispatch.jsonl').read_text().splitlines()]
            starts = {row['request_id'] for row in observations if row['status'] == 'calling'}
            ends = {row['request_id'] for row in observations if row['status'] == 'response_received'}
            scheduled = {row['request_id'] for row in observations if row['status'] == 'scheduled'}
            accepted = {row['request_id'] for row in observations if row['status'] == 'accepted'}
            self.assertEqual(len(starts), 8)
            self.assertEqual(starts, ends)
            self.assertEqual(scheduled, accepted)
            self.assertEqual(starts, accepted)
            self.assertTrue(all(row['elapsed_seconds'] >= 0 for row in observations
                                if row['status'] == 'response_received'))
            self.assertTrue(all('prompt' not in row and 'response' not in row for row in observations))
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertEqual({runner.receipts[cid]["model"]
                                  for cid in result["accepted_claim_ids"]},
                                 {client.identity})
            self.assertEqual(result["outstanding"]["kind"], "atomic")
            self.assertNotEqual(result["outstanding"]["packet_id"], result["packet_id"])

    def test_quota_exhaustion_leaves_failed_claim_for_main_agent(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            client = FakeSecondary(fail="a")
            result = dispatch_secondary_reviews(workflow, client, max_workers=4)
            self.assertEqual(result["status"], "fallback_to_main")
            self.assertEqual(result['warnings'][0]['code'], 'secondary_review_degraded')
            disclosure = NativeReviewWorkflow(workflow.directory).execution_disclosure()
            self.assertEqual(disclosure['secondary_events'][-1]['failed_claim_ids'], ['a'])
            self.assertFalse(disclosure['independent_review_guaranteed'])
            self.assertIn("a", result["failures"])
            self.assertEqual(len(result["accepted_claim_ids"]), 7)
            events = [json.loads(line) for line in
                      (workflow.directory / 'secondary-dispatch.jsonl').read_text().splitlines()]
            failed = [row for row in events if row['status'] == 'call_failed']
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]['error_type'], 'RuntimeError')
            self.assertNotIn('prompt', failed[0])
            remaining = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(list(remaining["items"]), ["a"])
            # The host uses the same original packet, not a second generated
            # review or a new canonical graph revision.
            schema = remaining["items"]["a"]["schema"]
            refs = schema["properties"]["reference_checks"]["items"]["properties"]["ref_id"].get("enum", [])
            valid = verdict("a", "PASS", refs)
            accepted = NativeReviewWorkflow(workflow.directory).submit(
                remaining["packet_id"], {"a": valid}, "host/main",
                remaining["input_hash"])
            self.assertFalse(accepted.get("blocked"), accepted)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertEqual(len(runner.receipts), 8)
                self.assertEqual(runner.receipts["a"]["model"], "host/main")

    def test_no_client_for_host_owned_role_stage(self):
        from .test_agent_cli import AgentCli
        with tempfile.TemporaryDirectory() as root:
            checkpoint = AgentCli().source_checkpoint(root)
            # Initial checkpoint is already past author roles; a fresh
            # manifest is necessary to inspect the host-only role stage.
            source = Path(root) / "news.json"
            source.write_text(json.dumps({"items": [
                dict(title="Fixture", url="https://example.test/source", published="2026-10-15")
            ]}))
            manifest = Path(root) / "manifest.json"
            manifest.write_text(json.dumps(dict(subject=dict(ticker="TEST"),
                as_of="2026-10-16", sources=[
                    dict(id="news", type="news_json", path=str(source))])))
            workflow = NativeReviewWorkflow.from_manifest(manifest, Path(root) / "native")
            secondary = FakeSecondary()
            outcome = dispatch_secondary_reviews(workflow, secondary)
            self.assertEqual(outcome["status"], "host_owned")
            self.assertEqual(outcome["kind"], "roles")
            self.assertEqual(secondary.calls, [])

    def test_cli_unavailable_agy_returns_host_packet_without_state_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            original = workflow.next()
            with patch("v4.cloud.AntigravityCloudClient", side_effect=RuntimeError("agy unavailable")):
                rc, output = run(["dispatch-secondary", "--work-dir", str(workflow.directory),
                                  "--agy-model", "fake"])
            self.assertEqual(rc, 0)
            self.assertEqual(output["status"], "fallback_to_main")
            self.assertEqual(output['warnings'][0]['next_action'], 'main_agent_complete_outstanding_reviews')
            disclosure = NativeReviewWorkflow(workflow.directory).execution_disclosure()
            self.assertEqual(disclosure['secondary_events'][-1]['code'], 'secondary_unavailable')
            pending = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(pending["packet_id"], original["packet_id"])
            self.assertEqual(set(pending["items"]), set(original["items"]))

    def test_cli_places_new_sessions_under_work_directory(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            client = FakeSecondary()
            with patch('v4.cloud.AntigravityCloudClient', return_value=client) as factory:
                rc, output = run(['dispatch-secondary', '--work-dir', str(workflow.directory),
                                  '--agy-model', 'fake', '--max-items', '1'])
            self.assertEqual(rc, 0)
            self.assertEqual(output['dispatched_claim_count'], 1)
            self.assertEqual(factory.call_args.kwargs['session_root'], workflow.directory / 'secondary-sessions')
