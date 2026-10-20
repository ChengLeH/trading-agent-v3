"""CLI exchange shape and error contracts with mocked workflow constructors."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .agent_cli import main
from .checkpoint import save_checkpoint
from .test_integration import seeded


def run(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(args)
    return code, json.loads(out.getvalue())


class AgentCli(unittest.TestCase):
    def test_prompt_files_deliver_full_role_context_without_large_inline_response(self):
        import hashlib
        from .test_agent_roles import NativeRoles
        with tempfile.TemporaryDirectory() as root:
            _, workflow = NativeRoles().bootstrap(root)
            packet = workflow.next()
            code, compact = run(['next', '--work-dir', str(workflow.directory), '--prompt-files'])
            self.assertEqual(code, 0)
            self.assertEqual(compact['packet_id'], packet['packet_id'])
            for role, item in compact['items'].items():
                self.assertNotIn('prompt', item)
                wire = Path(item['delivery']['prompt_file']).read_bytes()
                self.assertEqual(hashlib.sha256(wire).hexdigest(), item['delivery']['artifact_sha256'])
                self.assertEqual(json.loads(wire)['prompt'], packet['items'][role]['prompt'])
            self.assertLess(len(json.dumps(compact)), len(json.dumps(packet)))

    def source_checkpoint(self, root):
        runner = seeded()
        source = Path(root) / "original.json"
        save_checkpoint(runner, source)
        return source

    def test_init_imports_checkpoint_and_emits_json(self):
        with tempfile.TemporaryDirectory() as root:
            source = self.source_checkpoint(root)
            work = Path(root) / "native"
            with patch("v4.agent_reviews.NativeReviewWorkflow") as workflow:
                code, payload = run(["init", "--checkpoint", str(source), "--work-dir", str(work)])
            self.assertEqual(code, 0)
            self.assertEqual(payload, dict(ok=True, status="initialized", work_dir=str(work),
                                           source_checkpoint=str(source)))
            workflow.from_checkpoint.assert_called_once()
            self.assertEqual(str(workflow.from_checkpoint.call_args.args[0]), str(source))
            self.assertFalse(source.parent.joinpath("agent-state.json").exists())

    def test_init_missing_checkpoint_fails_strict_json_exit2(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("v4.agent_reviews.NativeReviewWorkflow") as workflow:
                code, payload = run(["init", "--checkpoint", str(Path(root) / "absent.json"),
                                     "--work-dir", str(Path(root) / "native")])
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["type"], "ValueError")
            self.assertIn("checkpoint not found", payload["error"]["reason"])
            workflow.from_checkpoint.assert_not_called()

    def test_next_blocked_packet_exits1_pending_exits0(self):
        pending = dict(status="reviews", packet_id="p1", kind="atomic",
                       input_hash="h1", items={"c1": dict(prompt="review one", schema={"type": "object"})})
        blocked = dict(blocked=True, status="blocked:reviews", packet_id="p1", review_errors={})
        with tempfile.TemporaryDirectory() as root:
            for expected, result in ((0, pending), (1, blocked)):
                with patch("v4.agent_reviews.NativeReviewWorkflow") as workflow:
                    workflow.return_value.next.return_value = result
                    code, payload = run(["next", "--work-dir", root])
                self.assertEqual(code, expected)
                self.assertEqual(payload, result)
                workflow.assert_called_once_with(root)

    def test_submit_reads_object_responses_and_fences_shape_errors(self):
        with tempfile.TemporaryDirectory() as root:
            responses = Path(root) / "responses.json"
            responses.write_text(json.dumps({"c1": {"claim_id": "c1", "verdict": "PASS"}}), encoding="utf-8")
            staged = dict(status="reviews_staged", packet_id="p1", missing_item_ids=["c2"])
            with patch("v4.agent_reviews.NativeReviewWorkflow") as workflow:
                workflow.return_value.submit.return_value = staged
                code, payload = run(["submit", "--work-dir", root, "--packet-id", "p1",
                                     "--input-hash", "h1", "--executor", "host/one",
                                     "--responses", str(responses)])
            self.assertEqual(code, 0)
            self.assertEqual(payload, staged)
            self.assertEqual(workflow.return_value.submit.call_args.args,
                             ("p1", {"c1": {"claim_id": "c1", "verdict": "PASS"}}, "host/one", "h1"))
            for broken, reason in ((None, "not found"), ("[]", "nonempty JSON object"),
                                   ('{"c1": "scalar"}', "object or array")):
                target = Path(root) / "broken.json"
                if broken is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_text(broken, encoding="utf-8")
                code, payload = run(["submit", "--work-dir", root, "--packet-id", "p1",
                                     "--input-hash", "h1", "--executor", "host/one",
                                     "--responses", str(target)])
                self.assertEqual(code, 2)
                self.assertIn(reason, payload["error"]["reason"])

    def test_submit_blocked_exit1_and_workflow_exception_exit2(self):
        with tempfile.TemporaryDirectory() as root:
            responses = Path(root) / "responses.json"
            responses.write_text(json.dumps({"c1": {"claim_id": "c1"}}), encoding="utf-8")
            base = ["submit", "--work-dir", root, "--packet-id", "p1",
                    "--input-hash", "h1", "--executor", "host/one", "--responses", str(responses)]
            blocked = dict(blocked=True, status="blocked:review_contract", blocked_claims={"c1": {}})
            with patch("v4.agent_reviews.NativeReviewWorkflow") as workflow:
                workflow.return_value.submit.return_value = blocked
                code, payload = run(base)
            self.assertEqual((code, payload["status"]), (1, "blocked:review_contract"))
            with patch("v4.agent_reviews.NativeReviewWorkflow") as workflow:
                workflow.return_value.submit.side_effect = ValueError("stale packet input version")
                code, payload = run(base)
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], dict(type="ValueError", reason="stale packet input version"))

    def test_patch_command_reads_array_and_fences_shape_errors(self):
        with tempfile.TemporaryDirectory() as root:
            patches_file = Path(root) / "patches.json"
            patches_file.write_text(json.dumps([dict(claim_id="c1", text="fixed")]), encoding="utf-8")
            accepted = dict(status="reviews", revalidate_claim_ids=["c1"], blocked=False)
            with patch("v4.agent_reviews.NativeReviewWorkflow") as workflow:
                workflow.return_value.submit_patch.return_value = accepted
                code, payload = run(["patch", "--work-dir", str(root), "--packet-id", "p9",
                                     "--input-hash", "h1", "--executor", "host/x",
                                     "--patches", str(patches_file)])
            self.assertEqual(code, 0)
            self.assertEqual(payload, accepted)
            workflow.return_value.submit_patch.assert_called_once_with(
                "p9", [dict(claim_id="c1", text="fixed")], "host/x", "h1")
            for bad, message in (([], "nonempty JSON array"), (dict(claim_id="c1"), "nonempty JSON array")):
                patches_file.write_text(json.dumps(bad), encoding="utf-8")
                with patch("v4.agent_reviews.NativeReviewWorkflow"):
                    code, payload = run(["patch", "--work-dir", str(root), "--packet-id", "p9",
                                         "--input-hash", "h1", "--executor", "host/x",
                                         "--patches", str(patches_file)])
                self.assertEqual(code, 2)
                self.assertIn(message, payload["error"]["reason"])
            missing = Path(root) / "absent.json"
            with patch("v4.agent_reviews.NativeReviewWorkflow"):
                code, payload = run(["patch", "--work-dir", str(root), "--packet-id", "p9",
                                     "--input-hash", "h1", "--executor", "host/x",
                                     "--patches", str(missing)])
            self.assertEqual(code, 2)
            self.assertIn("patches file not found", payload["error"]["reason"])

    def test_retry_patch_command_replays_operator_reason(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("v4.agent_reviews.NativeReviewWorkflow") as workflow:
                workflow.return_value.retry_patch.return_value = dict(status="retry_patch",
                                                                       retired_packet_id="p1", blocked=False)
                code, payload = run(["retry-patch", "--work-dir", str(root),
                                     "--reason", "corrected claim id in the next patch"])
            self.assertEqual(code, 0)
            workflow.return_value.retry_patch.assert_called_once_with(
                "corrected claim id in the next patch")

    def test_cli_module_starts_no_provider_or_subprocess(self):
        import ast
        tree = ast.parse(Path(__file__).with_name("agent_cli.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & {"subprocess", "socket", "urllib", "http", "requests", "threading"}, set())


    def test_real_manifest_native_bootstrap_without_cloud(self):
        from .agent_reviews import NativeReviewWorkflow
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "news.json").write_text(json.dumps({"items": [
                dict(title="Fixture", url="https://example.test/source", published="2026-10-15")
            ]}), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(dict(subject={"ticker": "TEST"},
                as_of="2026-10-16", sources=[dict(id="news", type="news_json",
                                                   path="news.json")])), encoding="utf-8")
            work = root / "native"
            with patch("v4.cloud.AntigravityCloudClient") as cloud:
                rc, created = run(["init", "--manifest", str(manifest), "--work-dir", str(work)])
                self.assertEqual(rc, 0, created)
                rc, first = run(["next", "--work-dir", str(work)])
                self.assertEqual(rc, 0, first)
                self.assertEqual(first["kind"], "roles")
                self.assertIn("market_analyst", first["items"])
                self.assertEqual(NativeReviewWorkflow(work).next()["packet_id"], first["packet_id"])
                cloud.assert_not_called()
            current = (work / "agent-state.json").read_bytes()
            rc, duplicate = run(["init", "--manifest", str(manifest), "--work-dir", str(work)])
            self.assertEqual(rc, 2)
            self.assertIn("already initialised", duplicate["error"]["reason"])
            self.assertEqual((work / "agent-state.json").read_bytes(), current)
            wrong = root / "wrong"
            rc, mismatch = run(["init", "--manifest", str(manifest), "--work-dir", str(wrong),
                                "--ticker", "NVDA"])
            self.assertEqual(rc, 2, mismatch)
            self.assertIn("ticker differs", mismatch["error"]["reason"])
            self.assertFalse((wrong / "agent-state.json").exists())
            missing = root / "absent"
            rc, bad = run(["init", "--manifest", str(root / "missing.json"),
                           "--work-dir", str(missing)])
            self.assertEqual(rc, 2, bad)
            self.assertIn("manifest not found", bad["error"]["reason"])
            self.assertFalse((missing / "agent-state.json").exists())

    def test_invalid_manifest_cannot_publish_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(dict(as_of="2026-10-16", sources=[])), encoding="utf-8")
            work = root / "native"
            rc, invalid = run(["init", "--manifest", str(manifest), "--work-dir", str(work)])
            self.assertEqual(rc, 2, invalid)
            self.assertIn("subject.ticker", invalid["error"]["reason"])
            self.assertFalse((work / "agent-state.json").exists())


if __name__ == "__main__":
    unittest.main()
