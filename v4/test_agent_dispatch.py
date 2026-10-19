"""Optional secondary review dispatch: deterministic, local, no paid model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .agent_dispatch import dispatch_secondary_reviews
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

    def test_agy_parallel_atomic_accepts_individual_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.bootstrap(root)
            client = FakeSecondary()
            result = dispatch_secondary_reviews(workflow, client, max_workers=3)
            self.assertEqual(result["status"], "secondary_complete")
            self.assertEqual(result["kind"], "atomic")
            self.assertEqual(len(result["accepted_claim_ids"]), 8)
            self.assertEqual(len(client.calls), 8)
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
            self.assertIn("a", result["failures"])
            self.assertEqual(len(result["accepted_claim_ids"]), 7)
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
            pending = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(pending["packet_id"], original["packet_id"])
            self.assertEqual(set(pending["items"]), set(original["items"]))
