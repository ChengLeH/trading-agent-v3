"""Checkpoint review reuse must be reconstructed from durable raw responses."""
import json
import tempfile
import unittest
from pathlib import Path

from .checkpoint import load_checkpoint
from .journal import RunJournal
from .test_integration import CloudDouble, seeded, verdict, reference_ids, global_verdict


class DurableReviewReceipts(unittest.TestCase):
    def _review_and_save(self, directory):
        runner = seeded()
        runner.journal = RunJournal(directory)
        client = CloudDouble()
        prepared = runner.prepare_phase6_atomic()
        result = runner._execute_review_batch(
            client, prepared["atomic_prompts"], runner.collect_phase6_atomic)
        self.assertFalse(result["blocked"], result)
        self.assertEqual(set(runner.receipts), {c["claim_id"] for c in runner.run.claims})
        path = Path(directory) / "checkpoint.json"
        return runner, client, path

    def test_valid_raw_archives_restore_atomic_receipts_without_reissuing_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            runner, client, checkpoint = self._review_and_save(temp)
            original_call_count = len(client.calls)

            resumed = load_checkpoint(checkpoint)
            self.assertEqual(resumed.receipts, runner.receipts)
            self.assertEqual(resumed._latest_review_sources, runner._latest_review_sources)
            prepared = resumed.prepare_phase6_atomic()
            self.assertEqual(prepared["claim_count"], 0)
            self.assertEqual(len(client.calls), original_call_count)

            # Claim a has no references. Its accepted zero-ref judgment still
            # has durable provenance and can be restored safely.
            self.assertEqual(resumed.receipts["a"]["result"]["reference_checks"], [])

    def test_same_model_resume_reaches_global_without_atomic_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            runner, verifier, checkpoint = self._review_and_save(temp)
            before = len(verifier.calls)
            resumed = load_checkpoint(checkpoint)
            resumed.journal = RunJournal(temp)
            result = resumed.execute_phase6(
                verifier, CloudDouble("test/deep"), CloudDouble("test/author"),
                CloudDouble("test/global", lambda _: global_verdict(resumed)))

            self.assertFalse(result["blocked"], result)
            self.assertEqual(len(verifier.calls), before)
            self.assertTrue(resumed._restored_receipt_ids["atomic"])

    def test_changed_atomic_model_rechecks_restored_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            runner, _, checkpoint = self._review_and_save(temp)
            resumed = load_checkpoint(checkpoint)
            resumed.journal = RunJournal(temp)
            replacement = CloudDouble("test/new-atomic")
            result = resumed.execute_phase6(
                replacement, CloudDouble("test/deep"), CloudDouble("test/author"),
                CloudDouble("test/global", lambda _: global_verdict(resumed)))

            self.assertFalse(result["blocked"], result)
            self.assertEqual(len(replacement.calls), len(runner.run.claims))
            self.assertEqual({r["model"] for r in resumed.receipts.values()}, {"test/new-atomic"})

    def test_global_dependency_batch_size_survives_checkpoint_and_old_files_default(self):
        from .checkpoint import save_checkpoint

        runner = seeded()
        runner.global_dependency_batch_size = 9
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "checkpoint.json"
            save_checkpoint(runner, checkpoint)
            saved = json.loads(checkpoint.read_text())
            self.assertEqual(saved["global_dependency_batch_size"], 9)
            self.assertEqual(load_checkpoint(checkpoint).global_dependency_batch_size, 9)

            saved.pop("global_dependency_batch_size")
            checkpoint.write_text(json.dumps(saved))
            self.assertEqual(load_checkpoint(checkpoint).global_dependency_batch_size, 12)

    def test_archive_tamper_epoch_change_and_input_change_safely_drop_receipts(self):
        for change in ("response", "epoch", "input"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                _, _, checkpoint = self._review_and_save(temp)
                data = json.loads(checkpoint.read_text())
                cid = "a"
                source = data["review_sources"]["atomic"][cid]
                archive = Path(temp) / "responses" / "archive" / (source["attempt_id"] + ".json")
                if change == "response":
                    record = json.loads(archive.read_text())
                    record["response"] = json.dumps(verdict("a", "FAIL"))
                    archive.write_text(json.dumps(record))
                elif change == "epoch":
                    from uuid import uuid4
                    data["verification_epoch"] = uuid4().hex
                    checkpoint.write_text(json.dumps(data))
                else:
                    for claim in data["state"]["claims"]:
                        if claim["claim_id"] == cid:
                            claim["text"] += " changed"
                    checkpoint.write_text(json.dumps(data))

                restored = load_checkpoint(checkpoint)
                self.assertNotIn(cid, restored.receipts)

    def test_missing_archive_drops_only_its_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            runner, _, checkpoint = self._review_and_save(temp)
            source = runner._latest_review_sources["atomic"]["a"]
            archive = Path(temp) / "responses" / "archive" / (source["attempt_id"] + ".json")
            archive.unlink()

            restored = load_checkpoint(checkpoint)
            self.assertNotIn("a", restored.receipts)
            self.assertEqual(set(restored.receipts), set(runner.receipts) - {"a"})

    def test_checkpoint_without_source_manifest_does_not_reuse_audit_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, checkpoint = self._review_and_save(temp)
            data = json.loads(checkpoint.read_text())
            data.pop("review_sources")
            checkpoint.write_text(json.dumps(data))

            self.assertEqual(load_checkpoint(checkpoint).receipts, {})

    def test_deep_receipt_requires_matching_archived_first_layer_digest(self):
        from .schema import digest

        with tempfile.TemporaryDirectory() as temp:
            runner = seeded()
            runner.journal = RunJournal(temp)
            # Make Claim a eligible for deep review while keeping every other
            # atomic result an ordinary PASS.
            def atomic_reply(prompt):
                payload = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
                cid = payload["claim_id"]
                return verdict(cid, "FAIL" if cid == "a" else "PASS",
                               reference_ids(payload["claim"]))

            atomic = CloudDouble("test/atomic", atomic_reply)
            prepared = runner.prepare_phase6_atomic()
            runner._execute_review_batch(atomic, prepared["atomic_prompts"], runner.collect_phase6_atomic)
            deep_prepared = runner.prepare_phase6_deep_review(["a"])
            deep = CloudDouble("test/deep", lambda _: verdict("a"))
            runner._execute_review_batch(deep, deep_prepared["deep_prompts"],
                                         runner.collect_phase6_deep_review, layer="deep")
            checkpoint = Path(temp) / "checkpoint.json"
            from .checkpoint import save_checkpoint
            save_checkpoint(runner, checkpoint)

            data = json.loads(checkpoint.read_text())
            source = data["review_sources"]["deep"]["a"]
            archive = Path(temp) / "responses" / "archive" / (source["attempt_id"] + ".json")
            record = json.loads(archive.read_text())
            record["review"]["first_review_hashes"]["a"] = digest({"changed": True})
            archive.write_text(json.dumps(record))

            restored = load_checkpoint(checkpoint)
            self.assertIn("a", restored.receipts)
            self.assertNotIn("a", restored.deep_receipts)

    def test_valid_deep_archive_restores_only_with_current_atomic_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = seeded()
            runner.journal = RunJournal(temp)

            def atomic_reply(prompt):
                payload = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
                cid = payload["claim_id"]
                status = "FAIL" if cid == "a" else "PASS"
                return verdict(cid, status, reference_ids(payload["claim"]))

            atomic = CloudDouble("test/atomic", atomic_reply)
            prepared = runner.prepare_phase6_atomic()
            runner._execute_review_batch(atomic, prepared["atomic_prompts"], runner.collect_phase6_atomic)
            deep_prepared = runner.prepare_phase6_deep_review(["a"])
            deep = CloudDouble("test/deep", lambda _: verdict("a"))
            runner._execute_review_batch(deep, deep_prepared["deep_prompts"],
                                         runner.collect_phase6_deep_review, layer="deep")
            from .checkpoint import save_checkpoint
            checkpoint = Path(temp) / "checkpoint.json"
            save_checkpoint(runner, checkpoint)

            restored = load_checkpoint(checkpoint)
            self.assertIn("a", restored.receipts)
            self.assertIn("a", restored.deep_receipts)
            self.assertIn("a", restored._restored_receipt_ids["deep"])


if __name__ == "__main__":
    unittest.main()
