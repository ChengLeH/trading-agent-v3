"""Deterministic root-patch -> cone -> global-finding -> patch -> reload regression.

Run as a normal unittest so Hypothesis sampling cannot leave this chain uncovered.
"""
from pathlib import Path
import tempfile
import unittest

from .agent_reviews import NativeReviewWorkflow
from .checkpoint import save_checkpoint
from .test_agent_reviews import response
from .test_integration import seeded


class DeterministicDeepChain(unittest.TestCase):
    def until_global(self, directory):
        reviewed = []
        for _ in range(75):
            workflow = NativeReviewWorkflow(directory)
            packet = workflow.next()
            if packet.get("kind") == "global":
                return packet, reviewed
            self.assertIn(packet.get("kind"), ("atomic", "deep"), packet)
            reviewed.extend(packet["items"])
            outcome = workflow.submit(packet["packet_id"],
                {cid: response(cid, item) for cid, item in packet["items"].items()},
                "host/deterministic", packet["input_hash"])
            self.assertFalse(outcome.get("blocked"), outcome)
        self.fail("global review never reached")

    def until_root_patch(self, directory):
        """Force a root failure in atomic review while siblings pass."""
        root_failed = False
        for _ in range(75):
            workflow = NativeReviewWorkflow(directory)
            packet = workflow.next()
            if packet.get("kind") == "patch":
                self.assertTrue(root_failed, "patch preceded root review failure")
                self.assertIn("a", packet["allowed_claim_ids"])
                return packet
            self.assertIn(packet.get("kind"), ("atomic", "deep"), packet)
            fail_this = "a" in packet["items"] and packet["kind"] in ("atomic", "deep")
            rows = {cid: response(cid, item, "FAIL" if fail_this and cid == "a" else "PASS")
                    for cid, item in packet["items"].items()}
            outcome = workflow.submit(packet["packet_id"], rows, "host/deterministic", packet["input_hash"])
            if fail_this and packet["kind"] == "deep":
                root_failed = True
        self.fail("root review failure did not generate a patch")

    def global_answer(self, directory):
        workflow = NativeReviewWorkflow(directory)
        with workflow.queue.transaction() as state:
            runner = workflow._load(state)
            claims = {c["claim_id"]: c for c in runner.run.claims}
            target_ids = runner.pending_global["target_claim_ids"]
        return dict(findings=[], resolved_external_findings=[], dependency_reviews=[
            dict(claim_id=cid, required_dependencies=claims[cid]["dependencies"],
                 unresolved=False, reason="Fixed-fixture dependency audit")
            for cid in target_ids])

    def test_root_cone_then_post_patch_global_finding_survives_reload(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "initial.json"
            save_checkpoint(seeded(), source)
            directory = Path(root) / "native"
            NativeReviewWorkflow.from_checkpoint(source, directory)

            first_patch = self.until_root_patch(directory)
            with NativeReviewWorkflow(directory).queue.transaction() as state:
                before = NativeReviewWorkflow(directory)._load(state)
                self.assertTrue({"b", "c"} <= set(before.receipts))
                epoch = before._verification_epoch
            patched = NativeReviewWorkflow(directory).submit_patch(
                first_patch["packet_id"],
                [dict(claim_id="a", text="First root-Claim repair")],
                "host/deterministic", first_patch["input_hash"])
            self.assertFalse(patched.get("blocked"), patched)
            self.assertTrue({"a", "b", "c"} <= set(patched["revalidate_claim_ids"]))
            with NativeReviewWorkflow(directory).queue.transaction() as state:
                after = NativeReviewWorkflow(directory)._load(state)
                self.assertEqual(after._verification_epoch, epoch)
                self.assertFalse({"a", "b", "c"} & set(after.receipts))
                self.assertIsNone(after.global_receipt)

            global_packet, cone_reviewed = self.until_global(directory)
            self.assertTrue({"a", "b", "c"} <= set(cone_reviewed))
            answer = self.global_answer(directory)
            answer["findings"] = [dict(
                claim_id="a", category="semantic_conflict",
                issue="Global conflict found after root and cone re-verification",
                evidence=dict(trigger="deterministic post-patch global review"),
                suggested_fix="Repair root Claim again")]
            role = next(iter(global_packet["items"]))
            result = NativeReviewWorkflow(directory).submit(
                global_packet["packet_id"], {role: answer},
                "host/deterministic-global", global_packet["input_hash"])
            self.assertTrue(result.get("needs_revision"), result)

            second_patch = NativeReviewWorkflow(directory).next()
            self.assertEqual(second_patch.get("kind"), "patch", second_patch)
            self.assertIn("a", second_patch["allowed_claim_ids"])
            outcome = NativeReviewWorkflow(directory).submit_patch(
                second_patch["packet_id"], [dict(claim_id="a", text="Global repair of root a")],
                "host/deterministic", second_patch["input_hash"])
            self.assertFalse(outcome.get("blocked"), outcome)

            reopened = NativeReviewWorkflow(directory)
            with reopened.queue.transaction() as state:
                runner = reopened._load(state)
                self.assertEqual(next(c for c in runner.run.claims
                    if c["claim_id"] == "a")["text"], "Global repair of root a")
                self.assertFalse({"a", "b", "c"} & set(runner.receipts))
                self.assertIsNone(runner.global_receipt)
                self.assertIsNone(runner.pending_patch)
            _, reviewed_after_reload = self.until_global(directory)
            self.assertTrue({"a", "b", "c"} <= set(reviewed_after_reload))
