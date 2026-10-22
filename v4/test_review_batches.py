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
from .schema import digest
from v3.evidence import EvidencePacket
from .cloud import structured_prompt


class ReviewBatches(unittest.TestCase):
    def test_prepared_single_fallback_results_follow_the_issued_response_schema(self):
        from unittest.mock import patch
        from . import runner as runner_module
        pack = runner_module.pack_review_requests
        def padded(*args, **kwargs):
            requests = pack(*args, **kwargs)
            for request in requests:
                request['prompt'] += ' ' * (kwargs['max_bytes'] + 1)
            return requests
        for layer in ('atomic', 'deep'):
            with self.subTest(layer=layer):
                r = seeded()
                if layer == 'deep':
                    r.prepare_phase6_atomic()
                    r.collect_phase6_atomic({cid: verdict(cid, 'SUSPICIOUS',
                        ref_ids=reference_ids(task.claim)) for cid, task in r._tasks().items()},
                        model_identity='test/first')
                prepare = r.prepare_phase6_atomic_batches if layer == 'atomic' else r.prepare_phase6_deep_review_batches
                with patch('v4.runner.pack_review_requests', side_effect=padded):
                    requests = prepare()['review_batches']
                self.assertTrue(all('claim_id' in req['response_schema']['properties'] for req in requests))
                replies = {req['request_id']: verdict(req['claim_ids'][0],
                    ref_ids=reference_ids(r._tasks()[req['claim_ids'][0]].claim)) for req in requests}
                collect = r.collect_phase6_atomic_batches if layer == 'atomic' else r.collect_phase6_deep_review_batches
                result = collect(replies, model_identity='test/reviewer')
                self.assertFalse(result['blocked'], result)
                receipts = r.receipts if layer == 'atomic' else r.deep_receipts
                self.assertEqual(set(receipts), set(r._tasks()))

    def tasks(self, count):
        r = seeded()
        r.run.claims = [claim(f"claim.{i}") for i in range(count)]
        return r, build_atomic_tasks(r.run)

    def runner_with_large_evidence(self):
        r = seeded()
        claim_a = next(c for c in r.run.claims if c["claim_id"] == "independent")
        evidence_id = "evidence.large-review-input"
        r.run.evidence_store.add(EvidencePacket(
            evidence_id=evidence_id,
            claim_candidate="x" * (70 * 1024),
            source_id="fixture.large",
            source_type="news_json",
            source_span="fixture span",
        ))
        claim_a["refs"].append(evidence_id)
        claim_a["bindings"].append(dict(
            ref_id=evidence_id,
            **{name: "unknown" for name in ("entity", "concept", "period", "scope", "unit", "basis")},
        ))
        return r

    def test_atomic_and_deep_use_independent_budgets_with_exact_evidence_accounting(self):
        r = self.runner_with_large_evidence()
        self.assertEqual(r.review_batch_max_bytes, 64 * 1024)
        self.assertEqual(r.review_deep_batch_max_bytes, 128 * 1024)

        with self.assertRaisesRegex(ValueError, "own_evidence_bytes"):
            r.prepare_phase6_atomic_batches()

        atomic = r._prepare_phase6_atomic(r._tasks(), enforce_budget=False)
        client = CloudDouble()
        blocked = r._execute_review_batch(client, atomic["atomic_prompts"], r.collect_phase6_atomic,
                                          layer="atomic")
        self.assertTrue(blocked["blocked"], blocked)
        row = next(row for row in blocked["oversized_items"] if row["claim_id"] == "independent")
        prompt = dict(atomic["atomic_prompts"])["independent"]
        schema = atomic["response_schemas"]["independent"]
        wire = structured_prompt(prompt, json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(row["wire_bytes"], len(wire.encode("utf-8")))
        self.assertEqual(row["schema_bytes"], len(json.dumps(schema, ensure_ascii=False,
                                                             separators=(",", ":")).encode("utf-8")))
        self.assertGreater(row["own_evidence_bytes"], 70 * 1024)
        self.assertEqual(row["wire_bytes"], row["own_evidence_bytes"] +
                         row["shared_context_bytes"] + row["schema_bytes"])
        self.assertEqual(row["budget_bytes"], 64 * 1024)
        sent_ids = []
        for sent in client.calls:
            payload = json.loads(sent.split("```json\n")[1].split("\n```", 1)[0])
            if "tasks" in payload:
                sent_ids.extend(row["claim"]["claim_id"] for row in payload["tasks"])
            if "claim_id" in payload:
                sent_ids.append(payload["claim_id"])
        self.assertNotIn("independent", sent_ids)

        tasks = r._tasks()
        atomic_results = {
            cid: verdict(cid, "SUSPICIOUS" if cid == "independent" else "PASS",
                         ref_ids=reference_ids(task.claim))
            for cid, task in tasks.items()
        }
        r.collect_phase6_atomic(atomic_results, model_identity="test/first")
        deep = r.prepare_phase6_deep_review_batches(["independent"])
        self.assertEqual(deep["request_count"], 1)
        class DeepClient(CloudDouble):
            def complete(self, prompt):
                self.calls.append(prompt)
                payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
                task = payload["task"]
                return verdict(task["claim_id"], ref_ids=reference_ids(task["claim"]))

        deep_result = r._execute_review_batch(DeepClient(), deep["deep_prompts"],
                                              r.collect_phase6_deep_review, layer="deep")
        self.assertFalse(deep_result["blocked"], deep_result)
        self.assertIn("independent", r.deep_receipts)

    def test_deep_budget_checkpoint_is_persistent_and_old_checkpoints_default_to_128_kib(self):
        r = seeded()
        self.assertEqual(r.review_deep_batch_max_bytes, 128 * 1024)
        r.review_batch_max_bytes = 12345
        r.review_deep_batch_max_bytes = 196608
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(r, path)
            restored = load_checkpoint(path)
            self.assertEqual(restored.review_batch_max_bytes, 12345)
            self.assertEqual(restored.review_deep_batch_max_bytes, 196608)

            old = json.loads(path.read_text())
            old.pop("review_deep_batch_max_bytes")
            path.write_text(json.dumps(old))
            legacy = load_checkpoint(path)
            self.assertEqual(legacy.review_batch_max_bytes, 12345)
            self.assertEqual(legacy.review_deep_batch_max_bytes, 128 * 1024)

    def test_long_claim_and_premise_are_counted_as_item_evidence(self):
        r = seeded()
        child = next(row for row in r.run.claims if row["claim_id"] == "b")
        premise = next(row for row in r.run.claims if row["claim_id"] == "a")
        child["dependencies"] = ["a"]
        child["text"] += " child-premise " + "c" * (20 * 1024)
        premise["text"] += " parent-premise " + "p" * (45 * 1024)
        tasks = r._tasks()
        prompt, schema = r._reference_review_request(tasks["b"], "atomic")
        from .review_budget import review_wire_breakdown
        row = review_wire_breakdown(prompt, schema)
        self.assertGreater(row["own_evidence_bytes"], 65 * 1024)
        self.assertEqual(row["wire_bytes"], row["own_evidence_bytes"] +
                         row["shared_context_bytes"] + row["schema_bytes"])

    def test_review_context_format_is_part_of_global_state_hash(self):
        from unittest.mock import patch
        r = seeded()
        before = r._hash()
        with patch("v4.runner.REVIEW_CONTEXT_FORMAT", "v4-review-context/test-next"):
            after = r._hash()
        self.assertNotEqual(before, after)

    def test_prepared_batches_fall_back_to_layer_capped_per_claim_requests(self):
        from unittest.mock import patch
        r = seeded()
        r.review_batch_size = 4
        oversized_batch_schema = {"type": "object", "description": "x" * (70 * 1024)}
        with patch("v4.review_batches.batch_schema", return_value=oversized_batch_schema):
            prepared = r.prepare_phase6_atomic_batches()
        self.assertEqual(prepared["request_count"], len(r._tasks()))
        for request in prepared["review_batches"]:
            wire = structured_prompt(request["prompt"], json.dumps(
                request["response_schema"], ensure_ascii=False, separators=(",", ":")))
            self.assertLessEqual(len(wire.encode("utf-8")), r.review_batch_max_bytes)

    def test_incomplete_related_diagnostics_blocks_sync_and_native_dispatch_under_budget(self):
        from .agent_reviews import NativeReviewWorkflow
        r = seeded()
        tasks = r._tasks()
        diagnostics = tasks["independent"].context["evidence_scope"]["ingestion_diagnostics"]
        diagnostics["truncation"] = dict(truncated=True, omitted_count=2, omitted_check_ids=["check.one"])
        diagnostics["requires_scoped_review"] = True
        prepared = r._prepare_phase6_atomic(tasks, enforce_budget=False)
        prompts = dict(prepared["atomic_prompts"])
        schemas = prepared["response_schemas"]
        prompt = prompts["independent"]
        schema = schemas["independent"]
        client = CloudDouble()
        sync = r._execute_review_batch(client, list(prompts.items()),
                                       r.collect_phase6_atomic, layer="atomic")
        self.assertTrue(sync["blocked"], sync)
        self.assertIn("related_diagnostics_incomplete",
                      {row["reason"] for row in sync["oversized_items"]})
        row = next(row for row in sync["oversized_items"]
                   if row["claim_id"] == "independent")
        self.assertLess(row["wire_bytes"], 64 * 1024)
        sent_ids = []
        for sent in client.calls:
            payload = json.loads(sent.split("```json\n")[1].split("\n```", 1)[0])
            sent_ids.append(payload["claim"]["claim_id"])
        self.assertNotIn("independent", sent_ids)
        self.assertTrue(sent_ids, "unrelated siblings should remain reviewable")
        self.assertIn("adjust the related diagnostics cap", sync["reason"])
        self.assertNotIn("increase the general item budget", sync["reason"].split("cannot restore")[0])

        with tempfile.TemporaryDirectory() as root:
            workflow = NativeReviewWorkflow(Path(root) / "native")
            state = dict(packets={}, active_packet_id=None, input_hash="fixture")
            native = workflow._issue_review_packet(
                state, r, "atomic", prompts, schemas,
                size=16, cap=262144, tasks=tasks)
        self.assertEqual(native["status"], "reviews", native)
        self.assertNotIn("independent", native["items"])
        self.assertEqual(native["oversized_items"][0]["reason"], "related_diagnostics_incomplete")
        self.assertEqual(native["next_action"], "reduce_structured_refs_or_adjust_related_diagnostics_cap")

    def test_partial_collector_keeps_all_unreceived_siblings_until_they_finish(self):
        for layer in ('atomic', 'deep'):
            with self.subTest(layer=layer):
                runner = seeded()
                if layer == 'deep':
                    runner.prepare_phase6_atomic()
                    runner.collect_phase6_atomic({cid: verdict(cid, 'SUSPICIOUS',
                        ref_ids=reference_ids(task.claim)) for cid, task in runner._tasks().items()},
                        model_identity='test/first')
                tasks = runner._tasks()
                tasks['independent'].context['evidence_scope']['ingestion_diagnostics']['requires_scoped_review'] = True
                runner._tasks = lambda: tasks
                prepare = runner._prepare_phase6_atomic if layer == 'atomic' else runner._prepare_phase6_deep_review
                prepare(tasks, enforce_budget=False)
                collect = runner.collect_phase6_atomic if layer == 'atomic' else runner.collect_phase6_deep_review
                receipts = runner.receipts if layer == 'atomic' else runner.deep_receipts
                expected = [cid for cid in tasks if cid != 'independent']
                for index, cid in enumerate(expected):
                    outcome = collect({cid: verdict(cid, ref_ids=reference_ids(tasks[cid].claim))},
                                      model_identity='test/reviewer', allow_partial=True)
                    self.assertIn(cid, receipts)
                    if index < len(expected) - 1:
                        self.assertFalse(outcome['blocked'], outcome)
                        self.assertIn(expected[index + 1], getattr(runner, 'pending_' + layer))
                self.assertEqual(set(receipts), set(expected))
                self.assertEqual(outcome['status'], 'blocked:review_context_budget')
                self.assertEqual(set(getattr(runner, 'pending_' + layer)), {'independent'})

    def test_collectors_never_accept_a_diagnostics_incomplete_pass_but_keep_siblings(self):
        r = seeded()
        tasks = r._tasks()
        diagnostics = tasks["independent"].context["evidence_scope"]["ingestion_diagnostics"]
        diagnostics["requires_scoped_review"] = True
        r._tasks = lambda: tasks
        r._prepare_phase6_atomic(tasks, enforce_budget=False)
        results = {cid: verdict(cid, ref_ids=reference_ids(task.claim))
                   for cid, task in tasks.items()}
        summary = r.collect_phase6_atomic(results, model_identity="test/first")
        self.assertEqual(r.receipts.keys(), tasks.keys() - {"independent"})
        self.assertTrue(summary["blocked"], summary)
        self.assertEqual(summary["status"], "blocked:review_context_budget")
        self.assertIn("independent", summary["blocked_claim_ids"])

        deep_runner = seeded()
        deep_tasks = deep_runner._tasks()
        deep_tasks["independent"].context["evidence_scope"]["ingestion_diagnostics"][
            "requires_scoped_review"] = True
        deep_runner._tasks = lambda: deep_tasks
        deep_runner.receipts = {
            cid: dict(input_hash=digest(task.to_dict()),
                      result=verdict(cid, "SUSPICIOUS" if cid == "independent" else "PASS",
                                     ref_ids=reference_ids(task.claim)),
                      model="test/first")
            for cid, task in deep_tasks.items()
        }
        deep_runner.prepare_phase6_deep_review(["independent"], enforce_budget=False)
        deep_result = deep_runner.collect_phase6_deep_review(
            {"independent": verdict("independent", ref_ids=reference_ids(deep_tasks["independent"].claim))},
            model_identity="test/deep")
        self.assertNotIn("independent", deep_runner.deep_receipts)
        self.assertTrue(deep_result["blocked"], deep_result)
        self.assertEqual(deep_result["status"], "blocked:review_context_budget")

    def test_87_claims_require_22_requests_and_each_has_own_schema(self):
        from .financial_review import FINANCIAL_REVIEW_CONTRACT
        _, tasks = self.tasks(87)
        requests = pack_review_requests(tasks, batch_size=4, max_bytes=65536, verification_epoch="epoch")
        self.assertEqual(len(requests), 22)
        self.assertEqual([cid for req in requests for cid in req["claim_ids"]], [t.claim_id for t in tasks])
        for req in requests:
            self.assertIn(FINANCIAL_REVIEW_CONTRACT, req['prompt'])
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
        from .review_budget import review_wire_breakdown
        own = review_wire_breakdown(request['prompt'], request['response_schema'])['own_evidence_bytes']
        size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        self.assertEqual(own, size(evidence) + sum(size(task.claim) for task in tasks))
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

    def test_single_reference_supplement_does_not_disable_batches_for_other_claims(self):
        r = seeded()
        r.review_batch_size = 16
        next(c for c in r.run.claims if c["claim_id"] == "b")["dependencies"].append("independent")
        task = {row.claim_id: row for row in build_atomic_tasks(r.run)}["b"]
        r.reference_supplements["atomic"]["b"] = dict(
            input_hash=digest(task.to_dict()),
            model="test/atomic", base_result=verdict("b", ref_ids=["a"]),
            missing_ref_ids=["independent"])
        prepared = r.prepare_phase6_atomic()

        class Client(CloudDouble):
            def complete(self, prompt):
                self.calls.append(prompt)
                payload = json.loads(prompt.split("```json\n")[1].split("\n```\n")[0])
                if "tasks" in payload:
                    return {"results": [verdict(row["claim"]["claim_id"], ref_ids=row["expected_reference_ids"])
                                        for row in payload["tasks"]]}
                if "accepted_reference_checks" in payload:
                    return verdict("b", ref_ids=payload["expected_reference_ids"])
                raise AssertionError("unexpected review request format")

        client = Client("test/atomic")
        summary = r._execute_review_batch(client, prepared["atomic_prompts"], r.collect_phase6_atomic)

        self.assertFalse(summary["blocked"], summary)
        self.assertEqual(len(client.calls), 2)  # one normal Claim batch plus b's targeted supplement
        self.assertEqual(set(r.receipts), {claim_id for claim_id, _ in prepared["atomic_prompts"]})
        self.assertEqual([row["ref_id"] for row in r.receipts["b"]["result"]["reference_checks"]],
                         ["a", "independent"])

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
            # Same-epoch restart keeps only archive-verified successful rows.
            self.assertEqual(load_checkpoint(Path(temp) / "checkpoint.json").receipts, r.receipts)
            self.assertEqual(load_checkpoint(Path(temp) / "checkpoint.json", continue_verification=False).receipts, {})

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
                seen.append((runner.review_batch_size, runner.review_batch_max_bytes,
                             runner.review_deep_batch_max_bytes, runner.max_workers))
                return dict(blocked=True, status="test-stop")
            args = ["--resume", str(path), "--out", str(Path(temp)/"out"), "--author-model", "m",
                    "--verifier-model", "m", "--deep-model", "m", "--global-model", "m",
                    "--request-timeout", "1500", "--review-batch-size", "3", "--review-batch-max-bytes", "32000",
                    "--max-workers", "2"]
            with patch("v4.__main__.AntigravityCloudClient", return_value=CloudDouble()) as factory, \
                 patch("v4.runner.PipelineRunnerV4.resume", execute), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 2)
            self.assertEqual(seen, [(3, 32000, 32000, 2)])
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
