"""Compact completion receipts without replacing canonical evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .completion_summary import summarize_completion
from .token_metrics import audit_native_work


class CompletionSummaryTests(unittest.TestCase):
    def test_result_artifact_summary_is_consistent_and_does_not_copy_findings_or_prompts(self):
        result = {
            "status": "complete",
            "task_id": "task-123",
            "packet_id": "packet-456",
            "completed_count": 3,
            "failed_count": 1,
            "interventions": [{"kind": "retry"}, {"kind": "repair"}],
            "findings": ["private finding text"],
            "prompt": "private prompt evidence",
        }
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "result.json"
            raw = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
            artifact.write_bytes(raw)

            summary = summarize_completion(artifact)

            self.assertEqual(summary["identity"], {
                "task_id": "task-123", "packet_id": "packet-456", "run_id": None,
            })
            self.assertEqual(summary["completed_count"], 3)
            self.assertEqual(summary["failed_count"], 1)
            self.assertEqual(summary["intervention_count"], 2)
            self.assertEqual(summary["completion_basis"], "unverified_result_fields")
            self.assertEqual(summary["result_artifact"], {
                "path": str(artifact.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                "sha256_basis": "artifact_bytes",
            })
            rendered = json.dumps(summary, ensure_ascii=False)
            self.assertNotIn("private finding text", rendered)
            self.assertNotIn("private prompt evidence", rendered)
            self.assertEqual(artifact.read_bytes(), raw)

    def test_only_validated_receipts_override_unverified_reported_counts(self):
        result = {"status": "complete", "completed_count": 99, "failed_count": 8,
                  "revision_history": [{"round": 1}]}
        accepted = {
            "atomic": {"claim.a": {"input_hash": "a" * 64, "model": "test/model",
                                    "result": {"verdict": "PASS"}}},
            "deep": {"claim.b": {"input_hash": "b" * 64, "model": "test/model",
                                  "result": {"verdict": "FAIL"}}},
            "global": {"input_hash": "c" * 64, "report_hash": "d" * 64,
                       "model": "test/model", "coverage_rounds": 1, "findings": []},
        }

        summary = summarize_completion(result, accepted_receipts=accepted)

        self.assertEqual(summary["completed_count"], 3)
        self.assertEqual(summary["failed_count"], 8)
        self.assertEqual(summary["intervention_count"], 1)
        self.assertEqual(summary["completion_basis"], "accepted_receipts")
        self.assertEqual(summary["receipt_status"], "accepted")
        self.assertEqual(summary["accepted_receipt_count"], 3)

    def test_accepted_receipts_do_not_certify_an_unrelated_supplied_verdict(self):
        result = {"verdict": "PASS"}
        accepted = {"claim.a": {"input_hash": "a" * 64, "model": "test/model",
                                "result": {"verdict": "FAIL"}}}

        summary = summarize_completion(result, accepted_receipts=accepted)

        self.assertEqual(summary["receipt_status"], "accepted")
        self.assertEqual(summary["accepted_receipt_count"], 1)
        self.assertEqual(summary["verdict_status"], "unverified_supplied_verdict")

    def test_arbitrary_success_verdict_is_unverified_and_cannot_count_as_completed(self):
        summary = summarize_completion({
            "status": "complete", "verdict": "PASS", "response": {"verdict": "PASS"},
        })

        self.assertIsNone(summary["completed_count"])
        self.assertIsNone(summary["failed_count"])
        self.assertEqual(summary["receipt_status"], "unverified")
        self.assertEqual(summary["verdict_status"], "unverified_supplied_verdict")
        self.assertIsNone(summary["result_artifact"]["path"])
        self.assertEqual(len(summary["result_artifact"]["sha256"]), 64)

    def test_corrupt_artifact_is_rejected_without_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "result.json"
            artifact.write_bytes(b"{broken")
            before = artifact.read_bytes()

            with self.assertRaisesRegex(ValueError, "valid JSON"):
                summarize_completion(artifact)

            self.assertEqual(artifact.read_bytes(), before)

    def test_arbitrary_status_text_is_not_copied_into_the_short_summary(self):
        summary = summarize_completion({"status": "private finding text"})

        self.assertEqual(summary["reported_status"], "unrecognized")
        self.assertNotIn("private finding text", json.dumps(summary))

    def test_metrics_and_summary_do_not_call_a_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "agent-state.json").write_text(json.dumps({
                "format": "v4-native-work/1", "input_hash": "x", "packets": {},
            }), encoding="utf-8")

            with patch("v4.cloud.request_completion", side_effect=AssertionError("model call")):
                audit_native_work(root)
                summarize_completion({"status": "complete"})


if __name__ == "__main__":
    unittest.main()
