"""Native main-Agent V4 lifecycle without Antigravity or cloud calls."""
import json
import tempfile
import unittest
from pathlib import Path

from .agent_reviews import NativeReviewWorkflow
from .test_agent_reviews import response
from .test_integration import claim, comparable_wait


class NativeManifestLifecycle(unittest.TestCase):
    def test_fresh_manifest_role_reviews_global_and_report_survive_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "news.json").write_text(json.dumps(dict(items=[
                dict(title="Fixture", url="https://example.test/source", published="2026-10-15")
            ])), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(dict(subject=dict(ticker="TEST"),
                as_of="2026-10-16", sources=[
                    dict(id="news", type="news_json", path="news.json")
                ])), encoding="utf-8")
            workflow = NativeReviewWorkflow.from_manifest(manifest, root / "native")
            workflow.record_secondary_event({}, code='secondary_unavailable', requested_identity='antigravity/test')
            kinds = []
            for _ in range(32):
                packet = workflow.next()
                kind = packet.get("kind")
                if packet.get("status") == "complete":
                    self.assertTrue(packet["report_markdown"])
                    self.assertIn("global_review", packet["sidecar"])
                    self.assertEqual(packet['execution_disclosure']['secondary_events'][0]['code'], 'secondary_unavailable')
                    self.assertEqual(packet['sidecar']['execution_disclosure'], packet['execution_disclosure'])
                    break
                output = {}
                if kind == "roles":
                    for role, item in packet["items"].items():
                        task = json.loads(item["prompt"].split("<context-data>\n")[1]
                                          .split("\n</context-data>")[0])
                        candidate = claim(role + ".1", [c["claim_id"] for c in task["claims"]])
                        candidate["author_role"] = role
                        entry = dict(claims=[candidate])
                        if role == "trader":
                            entry["trade"] = comparable_wait()
                            candidate["assertion"] = dict(kind="trade_plan", trade=entry["trade"])
                        output[role] = entry
                elif kind in {"atomic", "deep"}:
                    output = {cid: response(cid, item) for cid, item in packet["items"].items()}
                elif kind == "global":
                    role = next(iter(packet["items"]))
                    with workflow.queue.transaction() as state:
                        runner = workflow._load(state)
                        claims = {c["claim_id"]: c for c in runner.run.claims}
                        target = runner.pending_global["target_claim_ids"]
                    output = {role: dict(findings=[], resolved_external_findings=[],
                        dependency_reviews=[dict(claim_id=cid,
                            required_dependencies=claims[cid]["dependencies"],
                            unresolved=False, reason="deterministic simulated reviewer output")
                            for cid in target])}
                else:
                    self.fail(f"unexpected stage: {packet}")
                outcome = workflow.submit(packet["packet_id"], output,
                                          "host/synthetic-test", packet["input_hash"])
                self.assertFalse(outcome.get("blocked") and not outcome.get("needs_retry"), outcome)
                kinds.append(kind)
                workflow = NativeReviewWorkflow(workflow.directory)
            else:
                self.fail("native Manifest pipeline never finalized")
            self.assertEqual(kinds[:5], ["roles"] * 5)
            self.assertIn("atomic", kinds)
            self.assertEqual(kinds[-1], "global")
            repeated = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(repeated["report_markdown"], packet["report_markdown"])
            self.assertEqual(repeated["sidecar"]["report_hash"], packet["sidecar"]["report_hash"])
            self.assertEqual(repeated['execution_disclosure'], packet['execution_disclosure'])
