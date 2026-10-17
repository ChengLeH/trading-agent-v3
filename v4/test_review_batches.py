"""Micro-batching preserves per-Claim semantic contracts and complete evidence."""
from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from .atomic_phase6 import build_atomic_tasks
from .review_batches import pack_review_requests, unpack_review_result
from .test_contracts import claim
from .test_integration import seeded, verdict, reference_ids, CloudDouble, global_verdict
from .checkpoint import load_checkpoint, save_checkpoint
from .journal import RunJournal
from .semantic_contracts import semantic_review_schema


class ReviewBatches(unittest.TestCase):
    def tasks(self, count):
        r = seeded()
        r.run.claims = [claim(f"claim.{i}") for i in range(count)]
        return r, build_atomic_tasks(r.run)

    def test_87_claims_require_22_requests_and_each_has_own_schema(self):
        _, tasks = self.tasks(87)
        requests = pack_review_requests(tasks, batch_size=4, max_bytes=65536, verification_epoch="epoch")
        self.assertEqual(len(requests), 22)
        self.assertEqual([cid for req in requests for cid in req["claim_ids"]], [t.claim_id for t in tasks])
        for req in requests:
            self.assertLessEqual(len(req["claim_ids"]), 4)
            self.assertEqual(req["response_schema"]["properties"]["results"]["minItems"], len(req["claim_ids"]))

    def test_shared_evidence_is_deduplicated_without_truncation(self):
        _, tasks = self.tasks(4)
        evidence = {"kind": "evidence", "value": {"content": "證據全文" * 2000}}
        for task in tasks:
            task.resolved_refs = {"source.shared": deepcopy(evidence)}
            task.claim["refs"] = ["source.shared"]
        request, = pack_review_requests(tasks, batch_size=4, max_bytes=65536, verification_epoch="epoch")
        data = json.loads(request["prompt"].split("```json\n")[1].split("\n```")[0])
        self.assertEqual(data["evidence_catalog"]["source.shared"], evidence)
        self.assertEqual(len(data["tasks"]), 4)
        for row, task in zip(data["tasks"], tasks):
            self.assertEqual(row["claim"], task.claim)
            self.assertEqual(row["resolved_ref_ids"], ["source.shared"])
            self.assertEqual(row["expected_reference_ids"], ["source.shared"])

    def test_byte_limit_splits_batches_but_never_truncates_large_single_claim(self):
        _, tasks = self.tasks(3)
        tasks[0].claim["text"] = "原始證據" * 10000
        requests = pack_review_requests(tasks, batch_size=4, max_bytes=4000, verification_epoch="epoch")
        self.assertEqual(requests[0]["claim_ids"], [tasks[0].claim_id])
        self.assertIn(tasks[0].claim["text"], requests[0]["prompt"])
        self.assertEqual([cid for req in requests for cid in req["claim_ids"]], [t.claim_id for t in tasks])

    def test_conflicting_same_id_evidence_or_dependency_objects_rejected(self):
        _, tasks = self.tasks(2)
        tasks[0].resolved_refs = {"same": {"value": 1}}
        tasks[1].resolved_refs = {"same": {"value": 2}}
        with self.assertRaises(ValueError):
            pack_review_requests(tasks, batch_size=4, max_bytes=65536, verification_epoch="epoch")

    def test_conflicting_dependency_objects_are_not_silently_merged(self):
        _, tasks = self.tasks(2)
        tasks[0].dependency_claims = [dict(claim_id="dep", text="before")]
        tasks[1].dependency_claims = [dict(claim_id="dep", text="after")]
        with self.assertRaisesRegex(ValueError, "conflicting"):
            pack_review_requests(tasks, batch_size=4, max_bytes=65536, verification_epoch="epoch")

    def test_batch_unpack_never_overwrites_duplicate_or_unknown_ids(self):
        for rows in ([verdict("a"), verdict("a")], [verdict("elsewhere")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                unpack_review_result({"results": rows}, ["a", "b"])
        self.assertEqual(unpack_review_result({"results": [verdict("a")]}, ["a", "b"]), {"a": verdict("a")})
        self.assertEqual(unpack_review_result({}, ["a", "b"]), {})

    def test_batch_settings_are_persistent_and_old_checkpoint_compatible(self):
        r = seeded()
        r.review_batch_size, r.review_batch_max_bytes = 3, 12345
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(r, path)
            restored = load_checkpoint(path)
            self.assertEqual((restored.review_batch_size, restored.review_batch_max_bytes), (3, 12345))
            old = json.loads(path.read_text())
            old.pop("review_batch_size"); old.pop("review_batch_max_bytes")
            path.write_text(json.dumps(old))
            self.assertEqual(load_checkpoint(path).review_batch_size, 1)

    def test_missing_claim_only_retried_and_successes_durable_before_next_collection(self):
        r = seeded()
        r.review_batch_size = 4
        prepared = r.prepare_phase6_atomic()
        tasks = r._tasks()
        class Client(CloudDouble):
            def complete_structured(self, prompt, schema):
                self.calls.append(prompt)
                data = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
                if "tasks" in data:
                    return {"results": [verdict(row["claim"]["claim_id"], ref_ids=row["expected_reference_ids"])
                                        for row in data["tasks"] if row["claim"]["claim_id"] != "b"]}
                return verdict(data["claim_id"], ref_ids=reference_ids(data["claim"]))
        client = Client()
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            result = r._execute_review_batch(client, prepared["atomic_prompts"], r.collect_phase6_atomic)
            self.assertFalse(result["blocked"])
            self.assertEqual(set(r.receipts), set(tasks))
            self.assertEqual(len(client.calls), 4)  # 12 claims -> 3 batches + only missing b
            self.assertIn('"claim_id": "b"', client.calls[-1])
            records = [json.loads(p.read_text()) for p in (Path(temp) / "responses/archive").glob("*.json")]
            self.assertEqual(len(records), 4)
            self.assertTrue(all(row["status"] == "received" for row in records))

    def test_failed_batch_still_persists_running_successful_sibling(self):
        from threading import Barrier
        from .agy_session import AntigravityError
        r = seeded()
        r.review_batch_size = 4
        r.max_workers = 3
        prepared = r.prepare_phase6_atomic()
        barrier = Barrier(3)
        class Client(CloudDouble):
            def complete_structured(self, prompt, schema):
                data = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
                barrier.wait(timeout=3)
                if data["tasks"][0]["claim"]["claim_id"] == "a":
                    raise AntigravityError("permission_denied")
                return {"results": [verdict(row["claim"]["claim_id"], ref_ids=row["expected_reference_ids"])
                                    for row in data["tasks"]]}
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            with self.assertRaises(AntigravityError):
                r._execute_review_batch(Client(), prepared["atomic_prompts"], r.collect_phase6_atomic)
            saved = json.loads((Path(temp) / "checkpoint.json").read_text())
            self.assertEqual(len(saved["audit"]["atomic"]), 8)
            self.assertEqual(len(r.receipts), 8)
            # Same-process retry schedules only the four failed Claims.
            self.assertEqual(r.prepare_phase6_atomic()["claim_count"], 4)
            # Reload deliberately starts a fresh verification epoch; audit
            # receipts cannot silently substitute for a new execution.
            self.assertEqual(load_checkpoint(Path(temp) / "checkpoint.json").receipts, {})

    def test_deep_batches_never_include_atomic_pass_or_skip_global(self):
        r = seeded(); r.review_batch_size = 4
        class Client(CloudDouble):
            def complete_structured(self, prompt, schema):
                self.calls.append(prompt)
                data = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
                layer = data["review_layer"]
                return {"results": [verdict(row["claim"]["claim_id"],
                    "SUSPICIOUS" if layer == "atomic" and row["claim"]["claim_id"] in {"a", "b"} else "PASS",
                    ref_ids=row["expected_reference_ids"]) for row in data["tasks"]]}
        first, deep = Client(), Client("test/deep")
        sweep = CloudDouble("test/global", lambda _: global_verdict(r))
        result = r.execute_phase6(first, deep, CloudDouble(), sweep)
        self.assertFalse(result["blocked"], result)
        self.assertEqual(len(first.calls), 3)
        self.assertEqual(len(deep.calls), 1)
        data = json.loads(deep.calls[0].split("```json\n")[1].split("\n```")[0])
        self.assertEqual({row["claim"]["claim_id"] for row in data["tasks"]}, {"a", "b"})
        self.assertTrue(all("first_review" in row for row in data["tasks"]))
        self.assertEqual(len(sweep.calls), 1)

    def test_batched_patch_revalidates_only_changed_claim_and_descendants(self):
        from collections import Counter
        r = seeded(); r.review_batch_size = 4
        seen = Counter()
        class Client(CloudDouble):
            def complete_structured(self, prompt, schema):
                data = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
                rows = []
                for row in data["tasks"]:
                    c = row["claim"]
                    if data["review_layer"] == "atomic":
                        seen[c["claim_id"]] += 1
                    rows.append(verdict(c["claim_id"], "FAIL" if c["claim_id"] == "b" and c["text"] != "corrected" else "PASS",
                                        ref_ids=row["expected_reference_ids"]))
                return dict(results=rows)
        patcher = CloudDouble("test/author", lambda _: [dict(claim_id="b", text="corrected")])
        sweep = CloudDouble("test/global", lambda _: global_verdict(r))
        result = r.execute_phase6(Client(), Client("test/deep"), patcher, sweep)
        self.assertFalse(result["blocked"], result)
        self.assertEqual((seen["a"], seen["b"], seen["c"], seen["independent"]), (1, 2, 2, 1))
        self.assertEqual(len(patcher.calls), 1)
        self.assertEqual(len(sweep.calls), 1)

    def test_single_claim_mode_persists_success_when_parallel_sibling_fails(self):
        from threading import Barrier
        from .agy_session import AntigravityError
        r = seeded(); r.run.claims = r.run.claims[:2]; r.max_workers = 2
        prepared = r.prepare_phase6_atomic()
        barrier = Barrier(2)
        class Client(CloudDouble):
            def complete_structured(self, prompt, schema):
                data = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
                barrier.wait(timeout=3)
                if data["claim_id"] == "a":
                    raise AntigravityError("permission_denied")
                return verdict(data["claim_id"], ref_ids=reference_ids(data["claim"]))
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            with self.assertRaises(AntigravityError):
                r._execute_review_batch(Client(), prepared["atomic_prompts"], r.collect_phase6_atomic)
            saved = json.loads((Path(temp) / "checkpoint.json").read_text())
            progress = json.loads((Path(temp) / "progress.json").read_text())
            self.assertEqual(set(saved["audit"]["atomic"]), {"b"})
            self.assertEqual((progress["atomic_verified"], progress["total_claims"]), (1, 2))
            archives = [json.loads(p.read_text()) for p in (Path(temp) / "responses/archive").glob("*.json")]
            self.assertTrue(all("started_at" in row and row["elapsed_seconds"] >= 0 for row in archives))

    def test_compact_atomic_payload_preserves_entire_task_without_duplicate_schema(self):
        from .atomic_phase6 import build_atomic_prompt
        _, tasks = self.tasks(2)
        task = tasks[0]
        text = build_atomic_prompt(task, verification_epoch="epoch")
        payload = json.loads(text.split("```json\n")[1].split("\n```")[0])
        self.assertNotIn("response_schema", payload)
        self.assertEqual(text.count("<response-schema>"), 1)
        self.assertEqual(json.loads(text.split("<response-schema>\n")[1].split("\n</response-schema>")[0]),
                         semantic_review_schema(task.claim))
        for key, value in task.review_dict().items():
            self.assertEqual(payload[key], value)

    def test_public_deep_batches_only_prepare_eligible_claims(self):
        r = seeded(); r.review_batch_size = 4
        r.prepare_phase6_atomic()
        r.collect_phase6_atomic({cid: verdict(cid, "SUSPICIOUS" if cid in {"a", "b"} else "PASS",
            ref_ids=reference_ids(task.claim)) for cid, task in r._tasks().items()}, model_identity="test/first")
        prepared = r.prepare_phase6_deep_review_batches()
        self.assertEqual(prepared["request_count"], 1)
        request, = prepared["review_batches"]
        self.assertEqual(set(request["claim_ids"]), {"a", "b"})
        result = r.collect_phase6_deep_review_batches({request["request_id"]: dict(results=[
            verdict(cid, ref_ids=reference_ids(r._tasks()[cid].claim)) for cid in request["claim_ids"]])},
            model_identity="test/deep")
        self.assertFalse(result["blocked"])
        self.assertEqual(set(r.deep_receipts), {"a", "b"})

    def test_cli_timeout_and_resume_batch_overrides_reach_actual_configuration(self):
        import contextlib
        import io
        from unittest.mock import patch
        from .__main__ import main
        r = seeded()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(r, path)
            seen = []
            def execute(runner, *clients):
                seen.append((runner.review_batch_size, runner.review_batch_max_bytes, runner.max_workers))
                return dict(blocked=True, status="test-stop")
            args = ["--resume", str(path), "--out", str(Path(temp)/"out"), "--author-model", "m",
                    "--verifier-model", "m", "--deep-model", "m", "--global-model", "m",
                    "--request-timeout", "1500", "--review-batch-size", "3", "--review-batch-max-bytes", "32000",
                    "--max-workers", "2"]
            with patch("v4.__main__.AntigravityCloudClient", return_value=CloudDouble()) as factory, \
                 patch("v4.runner.PipelineRunnerV4.resume", execute), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 2)
            self.assertEqual(seen, [(3, 32000, 2)])
            self.assertEqual(len(factory.call_args_list), 4)
            self.assertTrue(all(c.kwargs["timeout"] == 1500 for c in factory.call_args_list))

    def test_large_schema_counts_toward_batch_size_and_is_never_truncated(self):
        from unittest.mock import patch
        _, tasks = self.tasks(2)
        schema = {"description": "x" * 65536}
        with patch("v4.review_batches.batch_schema", return_value=schema):
            requests = pack_review_requests(tasks, batch_size=4, max_bytes=65536, verification_epoch="epoch")
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(r["response_schema"] == schema for r in requests))

    def test_public_batch_prepare_collect_still_requires_every_reference_check(self):
        r = seeded(); r.review_batch_size = 4
        prepared = r.prepare_phase6_atomic_batches()
        replies = {}
        for request in prepared["review_batches"]:
            replies[request["request_id"]] = {"results": [verdict(cid, ref_ids=reference_ids(r._tasks()[cid].claim))
                                                          for cid in request["claim_ids"]]}
        result = r.collect_phase6_atomic_batches(replies, model_identity="test/first")
        self.assertFalse(result["blocked"])
        self.assertEqual(len(r.receipts), len(r.run.claims))


if __name__ == "__main__":
    unittest.main()
