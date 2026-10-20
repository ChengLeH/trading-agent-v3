"""Generic provider quota classification and durable fallback policy."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from .agy_protocol import classify_provider_failure
from .agy_session import AntigravityError, run_session
from .agent_dispatch import dispatch_secondary_reviews, restore_secondary_availability
from .test_cloud import cli_stub
from .test_agent_dispatch import FakeSecondary, SecondaryDispatch


class QuotaPolicy(unittest.TestCase):
    def test_structured_codes_precede_protocol_diagnostics_and_unknown_stays_unknown(self):
        self.assertEqual(classify_provider_failure({"error_code": "insufficient_quota",
            "error": "rate limit"})[0], "quota_exhausted")
        self.assertEqual(classify_provider_failure({"error_code": "rate_limit_exceeded"})[0], "rate_limited")
        self.assertEqual(classify_provider_failure({"error": "Individual quota reached"})[0], "quota_exhausted")
        self.assertIsNone(classify_provider_failure({"status": "SUCCESS", "error_code": "insufficient_quota"}))
        self.assertIsNone(classify_provider_failure({"error": "account is having a problem"}))
        self.assertIsNone(classify_provider_failure({"error": "unrelated prompt text"}))

    def test_terminal_quota_is_durable_and_never_continued_or_retried(self):
        body = '''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
emit(dict(event="result", result=dict(status="ERROR", error_code="insufficient_quota",
     error="Individual quota reached", event_id="evt-42", response="secret account detail")))
'''
        with tempfile.TemporaryDirectory() as root, cli_stub(body) as executable:
            with self.assertRaises(AntigravityError) as caught:
                run_session(executable, "model", "private prompt", 5, session_dir=root)
            self.assertEqual(caught.exception.code, "quota_exhausted")
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.details()["event_reference"], "evt-42")
            self.assertNotIn("secret", json.dumps(caught.exception.details()))
            state = json.loads((Path(root) / "session.json").read_text())
            self.assertEqual(state["last_error"], "quota_exhausted")
            event_archive = (Path(root) / "events.jsonl").read_text()
            self.assertNotIn("secret account detail", event_archive)
            self.assertIn("evt-42", event_archive)
            with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaises(AntigravityError) as blocked:
                run_session(executable, "model", "private prompt", 5, session_dir=root, resume=True)
            self.assertEqual(blocked.exception.code, "quota_exhausted")
            start.assert_not_called()

    def test_typed_quota_step_is_terminal_and_stream_interruption_stays_distinct(self):
        body = '''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
emit(dict(event="step_update", step_update=dict(step_type="error_message", text="Individual quota reached")))
'''
        with tempfile.TemporaryDirectory() as root, cli_stub(body) as executable:
            with self.assertRaises(AntigravityError) as caught:
                run_session(executable, "model", "prompt", 5, session_dir=root)
            self.assertEqual(caught.exception.code, "quota_exhausted")
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(json.loads((Path(root) / "session.json").read_text())["last_error"], "quota_exhausted")

    def test_client_stops_after_first_quota_event(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
             patch("v4.cloud.run_session", side_effect=AntigravityError("quota_exhausted")) as run:
            from .cloud import AntigravityCloudClient
            with self.assertRaises(AntigravityError) as caught:
                AntigravityCloudClient("model", request_attempts=5).complete("prompt")
            self.assertEqual(caught.exception.code, "quota_exhausted")
            self.assertEqual(run.call_count, 1)

    def test_rate_limit_gets_only_bounded_same_session_retry(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
             patch("v4.cloud.run_session", side_effect=[
                 AntigravityError("rate_limited", retryable=True, provider_error_code="rate_limit_exceeded"),
                 '{"ok":true}']) as run:
            from .cloud import AntigravityCloudClient
            self.assertEqual(AntigravityCloudClient("model", request_attempts=2).complete("prompt"), {"ok": True})
            self.assertEqual(run.call_count, 2)
            self.assertTrue(run.call_args.kwargs["resume"])

    def test_quota_stops_new_submissions_but_collects_inflight_valid_result(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = SecondaryDispatch().bootstrap(root)
            quota_seen = threading.Event()
            class InflightClient(FakeSecondary):
                def complete_structured(self, prompt, schema):
                    cid = schema["properties"]["claim_id"]["enum"][0]
                    self.calls.append(cid)
                    if cid == "a":
                        quota_seen.set()
                        raise AntigravityError("quota_exhausted", provider_error_code="insufficient_quota")
                    if cid == "b":
                        quota_seen.wait(2)
                        time.sleep(0.05)
                        return super().complete_structured(prompt, schema)
                    return super().complete_structured(prompt, schema)
            client = InflightClient()
            result = dispatch_secondary_reviews(workflow, client, max_workers=2)
            self.assertEqual(result["status"], "fallback_to_main")
            self.assertEqual(set(client.calls), {"a", "b"})
            self.assertIn("b", result["accepted_claim_ids"])
            self.assertNotIn("b", result["outstanding"]["items"])

    def test_availability_survives_invocation_and_restore_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = SecondaryDispatch().bootstrap(root)
            class QuotaClient(FakeSecondary):
                def complete_structured(self, prompt, schema):
                    cid = schema["properties"]["claim_id"]["enum"][0]
                    self.calls.append(cid)
                    if cid == "a":
                        raise AntigravityError("quota_exhausted", provider_error_code="insufficient_quota")
                    return super().complete_structured(prompt, schema)
            client = QuotaClient(fail=None)
            outcome = dispatch_secondary_reviews(workflow, client, max_workers=1)
            self.assertEqual(outcome["status"], "fallback_to_main")
            next_client = FakeSecondary()
            before = dispatch_secondary_reviews(workflow, next_client, max_workers=1)
            self.assertEqual(before["status"], "fallback_to_main")
            self.assertEqual(list(before["packet"]["items"]), list(outcome["outstanding"]["items"]))
            self.assertEqual(next_client.calls, [])
            self.assertTrue(restore_secondary_availability(workflow, client.identity, reason="operator verified access"))
            with workflow.queue.transaction() as state:
                self.assertEqual(state["secondary_availability"][client.identity]["status"], "available")
            restored = dispatch_secondary_reviews(workflow, FakeSecondary(), max_workers=1)
            self.assertIn(restored["status"], ("secondary_complete", "fallback_to_main"))
