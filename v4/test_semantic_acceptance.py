"""Negative semantic verdict fixtures test gates, not model accuracy."""
import copy
import json
import unittest
from .test_integration import seeded, review_all, verdict, CloudDouble, reference_ids as claim_reference_ids
from v3.facts import Fact
from v3.evidence import EvidencePacket
from collections import Counter
from pathlib import Path
import tempfile
from .atomic_phase6 import build_atomic_tasks, normalize_atomic_result


def reference_ids(task):
    c = task["claim"] if isinstance(task, dict) else task.claim
    return claim_reference_ids(c)


def source_checks(ids, relation="SUPPORTS"):
    return [dict(ref_id=ref, relation=relation, attributed_assertion="fixture attribution",
                 source_evidence="fixture source content", reason="scripted semantic verdict") for ref in ids]


def global_review(r, findings=None, required=None, unresolved=()):
    required = required or {}
    return dict(findings=findings or [], dependency_reviews=[
        dict(claim_id=c["claim_id"], required_dependencies=required.get(c["claim_id"], c["dependencies"]),
             unresolved=c["claim_id"] in unresolved, reason="scripted dependency reasoning")
        for c in r.run.claims])


class SemanticAcceptance(unittest.TestCase):
    def test_claim_labels_do_not_block_notes_before_semantic_review(self):
        from .test_contracts import claim
        from .schema import CLAIM_TYPES
        for label in sorted(CLAIM_TYPES):
            with self.subTest(label=label):
                r = seeded()
                c = claim("reviewer.note", ["a"])
                c.update(author_role="reviewer", claim_type=label, refs=["a"],
                         text="本次提供的資料沒有資本支出欄位，不能據此推定資本支出為零。")
                r.orch.submit_batch(r.run_id, {"reviewer": [c]})
                findings, blocked = r.orch.validate_stage(r.run_id, "phase6")
                self.assertFalse(blocked, findings)
                prepared = r.prepare_phase6_atomic()
                self.assertIn(c["claim_id"], dict(prepared["atomic_prompts"]))
                self.assertEqual(next(t for t in build_atomic_tasks(r.run) if t.claim_id == c["claim_id"])
                                 .review_dict()["dependency_claims"][0]["claim_id"], "a")
                with self.assertRaisesRegex(ValueError, "semantic verification"):
                    r.prepare_phase6_global_sweep()

    def test_risk_conclusion_wording_variants_reach_the_same_semantic_review_layer(self):
        statements = (
            "在財報前採取 0% 部位是合乎我目前風險偏好的選擇。",
            "僅憑 ATR 就斷定任何非零部位都不可能控制風險。",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                r = seeded()
                risk_claim = next(c for c in r.run.claims if c["author_role"] == "risk_manager")
                risk_claim["text"] = statement

                prompt = dict(r.prepare_phase6_atomic()["atomic_prompts"])[risk_claim["claim_id"]]

                self.assertIn(statement, prompt)

    def test_binding_coverage_uses_actual_sources_even_for_interpretations(self):
        from .bindings import validate_claim_bindings
        from .test_atomic_phase6 import make_run
        from .test_contracts import claim
        c = claim()
        c["refs"] = ["fact.dram_supply.2029"]
        run = make_run([c])
        findings = validate_claim_bindings([c], run.fact_store, require_bindings=True)
        self.assertTrue(any(f.status == "FAIL" and f.ref_id == c["refs"][0] for f in findings))

    def test_unsupported_content_stays_blocked_regardless_of_claim_label(self):
        from .test_contracts import claim
        cases = [("semantic_fact", "本次資料沒有資本支出，所以公司資本支出為零。"),
                 ("interpretation", "上游資料遭截斷，因此我未收到完整論述。"),
                 ("forecast", "公司已公布本季營收一千億元。")]
        for label, text in cases:
            with self.subTest(label=label):
                r = seeded()
                c = claim("reviewer.unsupported")
                c.update(author_role="reviewer", claim_type=label, text=text)
                r.orch.submit_batch(r.run_id, {"reviewer": [c]})
                prepared = r.prepare_phase6_atomic()
                index = {c["claim_id"]: c for c in r.run.claims}
                results = {cid: verdict(cid, ref_ids=claim_reference_ids(index[cid]))
                           for cid, _ in prepared["atomic_prompts"]}
                # Scripted LLM judgments exercise routing, not model accuracy.
                results[c["claim_id"]] = verdict(c["claim_id"], "SUSPICIOUS")
                summary = r.collect_phase6_atomic(results, model_identity="test/first")
                self.assertEqual(summary["deep_review_claim_ids"], [c["claim_id"]])
                r.prepare_phase6_deep_review()
                r.collect_phase6_deep_review({c["claim_id"]: verdict(c["claim_id"], "FAIL")},
                                            model_identity="test/deep")
                with self.assertRaisesRegex(ValueError, "semantic findings"):
                    r.prepare_phase6_global_sweep()
                r.orch.patch(r.run_id, [{"claim_id": c["claim_id"], "claim_type": "trade_assumption"}],
                             allowed_ids={c["claim_id"]})
                self.assertIn(c["claim_id"], dict(r.prepare_phase6_atomic()["atomic_prompts"]))
                self.assertNotIn(c["claim_id"], r.deep_receipts)

    def test_review_context_describes_catalog_scope_without_fabricating_transport_evidence(self):
        from .test_atomic_phase6 import make_run
        from .test_contracts import claim
        run = make_run([claim()])
        task = build_atomic_tasks(run)[0]
        scope = task.context["evidence_scope"]
        self.assertEqual(scope["fact_concepts"], ["DRAMWaferSupply"])
        self.assertEqual(scope["counts"]["facts"], 1)
        self.assertFalse(scope["includes_source_contents"])
        self.assertNotIn("transport_verified", scope)
        self.assertEqual(task.review_dict()["context"]["evidence_scope"], scope)

    def test_generated_trade_resources_do_not_invalidate_unrelated_catalog_scope(self):
        from .schema import digest
        r = seeded()
        before = {t.claim_id: t for t in build_atomic_tasks(r.run)}
        r.orch.register_trade(r.run_id, dict(direction="SHORT", entry=173.16, stop=184,
                                            target=148, position_pct=5))
        after = {t.claim_id: t for t in build_atomic_tasks(r.run)}
        self.assertTrue(r.run.trade_generated[0])
        self.assertEqual(digest(before["a"].to_dict()), digest(after["a"].to_dict()))
        self.assertNotEqual(digest(before["role.trader"].to_dict()), digest(after["role.trader"].to_dict()))

    def test_large_catalog_keeps_review_bounded_without_truncating_source_evidence(self):
        from v3.sources import SourceRecord
        r = seeded()
        for i in range(1000):
            r.run.fact_store.add(Fact(fact_id=f"metric.{i}", value=i, unit="USD", period="2026-Q2",
                                      concept=f"DistinctStructuredMetric_{i}", source_id=f"source.{i}"))
            r.run.records.append(SourceRecord(id=f"source.{i}", type="generic_text", subject_ticker="TEST",
                                               subject_cik=None, payload={"original": i}, source_hash=f"hash.{i}"))
        prepared = r.prepare_phase6_atomic()
        task = next(t for t in build_atomic_tasks(r.run) if t.claim_id == "a")
        scope = task.context["evidence_scope"]
        self.assertIsNone(scope["fact_concepts"])
        self.assertEqual(scope["fact_concept_count"], 1000)
        self.assertEqual(scope["source_record_count"], 1000)
        self.assertLess(len(json.dumps(scope).encode()), 1024)
        self.assertEqual(len(r.run.fact_store.to_dict()), 1000)
        self.assertEqual(len(r.run.records), 1000)
        self.assertEqual(prepared["claim_count"], len(r.run.claims))

    @staticmethod
    def add_evidence_reference(runner, claim_id, evidence_id):
        runner.run.evidence_store.add(EvidencePacket(
            evidence_id=evidence_id, claim_candidate="new source passage", source_id="source.new",
            source_type="generic_text", source_span="p.1"))
        claim = next(c for c in runner.run.claims if c["claim_id"] == claim_id)
        claim["refs"].append(evidence_id)
        claim["bindings"].append(dict(ref_id=evidence_id, entity="unknown", concept="unknown",
                                      period="unknown", scope="unknown", unit="unknown", basis="unknown"))

    def test_new_execution_uses_fresh_atomic_and_deep_requests_with_exact_schema(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        original = seeded()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "same-input.json"
            save_checkpoint(original, path)
            runners = [original, load_checkpoint(path, continue_verification=False)]
        plans = [r.prepare_phase6_atomic() for r in runners]
        self.assertEqual(runners[0]._hash(), runners[1]._hash())
        self.assertNotEqual(plans[0]["atomic_prompts"], plans[1]["atomic_prompts"])
        self.assertEqual(plans[0]["response_schemas"]["b"]["properties"]["reference_checks"]["minItems"], 1)
        self.assertEqual(plans[0]["response_schemas"]["a"]["properties"]["reference_checks"]["maxItems"], 0)
        deep = []
        for r in runners:
            results = self.atomic_results(r)
            results["a"] = verdict("a", "FAIL")
            r.collect_phase6_atomic(results, model_identity="test/first")
            deep.append(r.prepare_phase6_deep_review()["deep_prompts"])
        self.assertNotEqual(deep[0], deep[1])

    def test_missing_reference_array_is_only_optional_when_claim_has_no_references(self):
        from .semantic_contracts import semantic_review_schema
        r = seeded()
        claim = next(c for c in r.run.claims if c["claim_id"] == "a")
        response = verdict("a")
        response.pop("reference_checks")

        normalized = normalize_atomic_result("a", response, expected_ref_ids=[])
        schema = semantic_review_schema(claim)

        self.assertEqual(normalized["reference_checks"], [])
        self.assertNotIn("reference_checks", schema["required"])

    def test_missing_single_reference_is_supplemented_without_rechecking_valid_refs(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r = seeded()
        claim_b = next(c for c in r.run.claims if c["claim_id"] == "b")
        claim_b["dependencies"].append("independent")
        prepared = r.prepare_phase6_atomic()
        tasks = {t.claim_id: t for t in build_atomic_tasks(r.run)}
        results = {cid: verdict(cid, ref_ids=reference_ids(tasks[cid]))
                   for cid, _ in prepared["atomic_prompts"]}
        results["b"]["reference_checks"] = source_checks(["a"])

        first = r.collect_phase6_atomic(results, model_identity="test/first")

        self.assertTrue(first["blocked"])
        self.assertEqual(first["retry_claim_ids"], ["b"])
        self.assertEqual(first["missing_reference_ids"], {"b": ["independent"]})
        self.assertEqual(first["response_schemas"]["b"]["properties"]["reference_checks"]["items"]["properties"]["ref_id"]["enum"],
                         ["independent"])
        self.assertEqual([row["ref_id"] for row in r.reference_supplements["atomic"]["b"]["base_result"]["reference_checks"]],
                         ["a"])

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(r, path)
            resumed = load_checkpoint(path)
            restored = resumed.prepare_phase6_atomic()
            b_prompt = dict(restored["atomic_prompts"])["b"]
            self.assertNotIn("b", resumed.reference_supplements["atomic"])
            self.assertEqual(restored["response_schemas"]["b"]["properties"]["reference_checks"]["items"]["properties"]["ref_id"]["enum"],
                             ["a", "independent"])
            self.assertIn('"expected_reference_ids":["a","independent"]', b_prompt)

        completed = r.collect_phase6_atomic(
            {"b": verdict("b", ref_ids=["independent"])}, model_identity="test/first")
        self.assertFalse(completed["blocked"])
        self.assertEqual([row["ref_id"] for row in r.receipts["b"]["result"]["reference_checks"]],
                         ["a", "independent"])

    def test_missing_supplement_response_preserves_accepted_reference_checks(self):
        r = seeded()
        next(c for c in r.run.claims if c["claim_id"] == "b")["dependencies"].append("independent")
        prepared = r.prepare_phase6_atomic()
        tasks = {task.claim_id: task for task in build_atomic_tasks(r.run)}
        results = {cid: verdict(cid, ref_ids=reference_ids(tasks[cid]))
                   for cid, _ in prepared["atomic_prompts"]}
        results["b"]["reference_checks"] = source_checks(["a"])
        r.collect_phase6_atomic(results, model_identity="test/first")
        accepted = copy.deepcopy(r.reference_supplements["atomic"]["b"]["base_result"]["reference_checks"])

        retry = r.collect_phase6_atomic({}, model_identity="test/first")

        self.assertEqual([row["ref_id"] for row in r.reference_supplements["atomic"]["b"]["base_result"]["reference_checks"]],
                         [row["ref_id"] for row in accepted])
        self.assertEqual(retry["response_schemas"]["b"]["properties"]["reference_checks"]["items"]["properties"]["ref_id"]["enum"],
                         ["independent"])

    def test_invalid_supplement_verdict_retries_without_rechecking_accepted_references(self):
        from .checkpoint import load_checkpoint, save_checkpoint
        r = seeded()
        next(c for c in r.run.claims if c["claim_id"] == "b")["dependencies"].append("independent")
        prepared = r.prepare_phase6_atomic()
        tasks = {task.claim_id: task for task in build_atomic_tasks(r.run)}
        initial = {cid: verdict(cid, ref_ids=reference_ids(tasks[cid]))
                   for cid, _ in prepared["atomic_prompts"]}
        initial["b"]["reference_checks"] = source_checks(["a"])
        first = r.collect_phase6_atomic(initial, model_identity="test/original")
        self.assertEqual(first["missing_reference_ids"], {"b": ["independent"]})

        malformed = verdict("b", "NOT_A_VERDICT", ref_ids=["independent"])
        r.collect_phase6_atomic({"b": malformed}, model_identity="test/original")
        repair = r.reference_supplements["atomic"]["b"]
        self.assertTrue(repair["contract_repair"])
        self.assertEqual(repair["missing_ref_ids"], [])
        self.assertEqual([row["ref_id"] for row in repair["accepted_reference_checks"]],
                         ["a", "independent"])

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "supplement-repair.json"
            save_checkpoint(r, path)
            resumed = load_checkpoint(path)
            prepared = resumed.prepare_phase6_atomic()
            self.assertNotIn("b", resumed.reference_supplements["atomic"])
            self.assertEqual(prepared["response_schemas"]["b"]["properties"]["reference_checks"]["maxItems"], 2)

        result = r.collect_phase6_atomic(
            {"b": dict(claim_id="b", verdict="PASS", findings=[])},
            model_identity="test/original")
        self.assertFalse(result["blocked"], result)
        self.assertEqual([row["ref_id"] for row in r.receipts["b"]["result"]["reference_checks"]],
                         ["a", "independent"])

    def test_checkpoint_reuses_only_reference_checks_found_in_durable_response_archive(self):
        from .checkpoint import load_checkpoint, save_checkpoint
        from .journal import RunJournal
        from .semantic_contracts import SEMANTIC_REVIEW_CONTRACT
        from .schema import digest

        r = seeded()
        next(c for c in r.run.claims if c["claim_id"] == "b")["dependencies"].append("independent")
        prepared = r.prepare_phase6_atomic()
        tasks = {task.claim_id: task for task in build_atomic_tasks(r.run)}
        prompt = dict(prepared["atomic_prompts"])["b"]
        response = verdict("b", ref_ids=["a"])
        client = CloudDouble("test/archive-bound", lambda _prompt: response)
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            context = dict(layer="atomic", verification_epoch=r._verification_epoch,
                           contract=SEMANTIC_REVIEW_CONTRACT,
                           task_hashes={"b": digest(tasks["b"].to_dict())})
            received, provenance = r._complete(
                client, "b", prompt, response_schema=prepared["response_schemas"]["b"],
                review_context=context, with_provenance=True)
            self.assertIsNotNone(provenance)
            source = {**provenance, "claim_id": "b", "layer": "atomic"}
            r._record_review_progress({"b": received}, client, "atomic", {"b": source})
            partial = r.collect_phase6_atomic({"b": received}, model_identity=client.identity)
            self.assertTrue(partial["blocked"])
            checkpoint = Path(temp) / "checkpoint.json"
            saved = json.loads(checkpoint.read_text())
            supplement = saved["reference_supplements"]["atomic"]["b"]
            self.assertEqual(len(supplement["reference_sources"]), 1)
            self.assertEqual(supplement["base_result_source"]["attempt_id"], provenance["attempt_id"])

            restored = load_checkpoint(checkpoint)
            self.assertIn("b", restored.reference_supplements["atomic"])
            self.assertEqual(restored.prepare_phase6_atomic()["response_schemas"]["b"][
                "properties"]["reference_checks"]["items"]["properties"]["ref_id"]["enum"], ["independent"])

            supplement["base_result"]["reference_checks"][0]["source_evidence"] = "checkpoint-only fabricated support"
            checkpoint.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            tampered = load_checkpoint(checkpoint)
            self.assertNotIn("b", tampered.reference_supplements["atomic"])
            self.assertEqual(tampered.prepare_phase6_atomic()["response_schemas"]["b"][
                "properties"]["reference_checks"]["items"]["properties"]["ref_id"]["enum"], ["a", "independent"])

            second = r.prepare_phase6_atomic()
            second_prompt = dict(second["atomic_prompts"])["b"]
            malformed = verdict("b", "NOT_A_VERDICT", ref_ids=["independent"])
            client.responder = lambda _prompt: malformed
            second_context = dict(layer="atomic", verification_epoch=r._verification_epoch,
                                  contract=SEMANTIC_REVIEW_CONTRACT,
                                  task_hashes={"b": digest(tasks["b"].to_dict())})
            received, provenance = r._complete(
                client, "b", second_prompt, response_schema=second["response_schemas"]["b"],
                review_context=second_context, with_provenance=True)
            r._record_review_progress({"b": received}, client, "atomic",
                                      {"b": {**provenance, "claim_id": "b", "layer": "atomic"}})
            r.collect_phase6_atomic({"b": received}, model_identity=client.identity)
            repair = r.reference_supplements["atomic"]["b"]
            self.assertTrue(repair["contract_repair"])
            self.assertEqual(len(repair["reference_sources"]), 2)

            saved = json.loads(checkpoint.read_text())
            accepted = saved["reference_supplements"]["atomic"]["b"]["accepted_reference_checks"]
            self.assertEqual([row["ref_id"] for row in accepted], ["a", "independent"])
            restored_repair = load_checkpoint(checkpoint)
            self.assertTrue(restored_repair.reference_supplements["atomic"]["b"]["contract_repair"])
            self.assertEqual(restored_repair.prepare_phase6_atomic()["response_schemas"]["b"][
                "properties"]["reference_checks"]["maxItems"], 0)

            accepted[0]["source_evidence"] = "checkpoint-only fabricated accepted reference"
            checkpoint.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            tampered_repair = load_checkpoint(checkpoint)
            self.assertNotIn("b", tampered_repair.reference_supplements["atomic"])

    def test_model_change_is_rejected_before_supplement_request_is_sent(self):
        from .checkpoint import load_checkpoint, save_checkpoint
        from .journal import RunJournal
        from .semantic_contracts import SEMANTIC_REVIEW_CONTRACT
        from .schema import digest
        r = seeded()
        next(c for c in r.run.claims if c["claim_id"] == "b")["dependencies"].append("independent")
        prepared = r.prepare_phase6_atomic()
        tasks = {task.claim_id: task for task in build_atomic_tasks(r.run)}

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model-bound-supplement.json"
            r.journal = RunJournal(temp)
            prompt = dict(prepared["atomic_prompts"])["b"]
            response = verdict("b", ref_ids=["a"])
            response_schema = prepared["response_schemas"]["b"]
            context = dict(layer="atomic", verification_epoch=r._verification_epoch,
                           contract=SEMANTIC_REVIEW_CONTRACT,
                           task_hashes={"b": digest(tasks["b"].to_dict())})
            client = CloudDouble("test/original", lambda _prompt: response)
            received, provenance = r._complete(
                client, "b", prompt, response_schema=response_schema,
                review_context=context, with_provenance=True)
            self.assertIsNotNone(provenance)
            r._record_review_progress(
                {"b": received}, client, "atomic",
                {"b": {**provenance, "claim_id": "b", "layer": "atomic"}})
            r.collect_phase6_atomic({"b": received}, model_identity=client.identity)
            save_checkpoint(r, path)
            r = load_checkpoint(path)

        replacement = CloudDouble("test/replacement")
        blocked = r.execute_phase6(replacement, CloudDouble("test/deep"), CloudDouble("test/patch"),
                                   CloudDouble("test/global"))

        self.assertTrue(blocked["blocked"])
        self.assertIn("reference supplement model differs", blocked["reason"])
        self.assertEqual(replacement.calls, [])

    def test_minor_clarity_note_does_not_trigger_deep_review_or_patch(self):
        r = seeded()
        prepared = r.prepare_phase6_atomic()
        tasks = {task.claim_id: task for task in build_atomic_tasks(r.run)}
        results = {cid: verdict(cid, ref_ids=reference_ids(tasks[cid]))
                   for cid, _ in prepared["atomic_prompts"]}
        results["a"]["clarity_notes"] = [dict(issue="描述略籠統", reason="不影響主體、事實或決策")]

        summary = r.collect_phase6_atomic(results, model_identity="test/first")

        self.assertFalse(summary["needs_deep_review"])
        self.assertEqual(summary["pass_claim_ids"], list(results))
        self.assertEqual(r.receipts["a"]["result"]["clarity_notes"], results["a"]["clarity_notes"])

    def test_global_clarity_only_finding_is_recorded_without_blocking_delivery(self):
        r = seeded(); review_all(r)
        r.prepare_phase6_global_sweep()
        note = dict(claim_ids=["b"], issue="措辭略籠統", evidence="Claim text", suggested_fix="可選擇改寫",
                    materiality="clarity_only", materiality_reason="合理解讀都保留相同事實與決策")

        result = r.collect_phase6_global_sweep(global_review(r, [note]), model_identity="test/global")
        report = r.finalize()

        self.assertFalse(result["blocked"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["clarity_notes"], [note | {"finding_id": result["clarity_notes"][0]["finding_id"]}])
        self.assertFalse(report["blocked"])
        self.assertEqual(report["sidecar"]["global_review"]["clarity_notes"], result["clarity_notes"])

    def test_global_prompt_distinguishes_role_views_from_same_scope_conflicts(self):
        r = seeded(); review_all(r)
        _, prompt = r.prepare_phase6_global_sweep()

        self.assertIn("不同角色可呈現不同觀點", prompt)
        self.assertIn("同一標的、期間、條件與決策層級", prompt)
        self.assertIn("resolution_route", prompt)

    def test_external_resolution_route_blocks_without_spending_revision_round(self):
        r = seeded()
        r.max_revisions = 0
        finding = dict(claim_ids=["b"], issue="缺少九月的來源數據", evidence="當前 source store 無該期間資料",
                       materiality="material", resolution_route="evidence_needed",
                       resolution_reason="Claim 修補器不能建立不存在的行情資料",
                       evidence_request=dict(proposition="取得九月行情資料", refs=[], entity="TEST",
                           metric="closing price", period="2026-09", unit="USD", basis="",
                           scope="", known_data_range=dict(start="2026-08", end="2026-10")))
        verifier, deep = CloudDouble(), CloudDouble("test/deep")
        patcher = CloudDouble("test/patcher")
        global_client = CloudDouble("test/global", lambda _: global_review(r, [finding]))

        blocked = r.execute_phase6(verifier, deep, patcher, global_client)

        self.assertFalse(blocked["blocked"])
        self.assertEqual(blocked["status"], "evidence_recovery")
        self.assertFalse(blocked["delivery_ready"])
        self.assertEqual(len(blocked["evidence_requests"]), 1)
        self.assertEqual(r.revision_round, 0)
        self.assertEqual(patcher.calls, [])
        self.assertEqual(r.deferred_global_findings[0]["resolution_route"], "evidence_needed")
        self.assertEqual(r.deferred_global_findings[0]["resolution_state_hash"],
                         r._resolution_state_hash(r.deferred_global_findings[0]))
        from .checkpoint import load_checkpoint, save_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "external-route.json"
            save_checkpoint(r, path)
            restored = load_checkpoint(path)
            self.assertEqual(restored.deferred_global_findings[0]["resolution_state_hash"],
                             r._resolution_state_hash(restored.deferred_global_findings[0]))

        global_client.responder = lambda _: global_review(r)
        next(c for c in r.run.claims if c["claim_id"] == "role.bear")["text"] = "另一個角色的新觀察。"
        unchanged = r.execute_phase6(verifier, deep, patcher, global_client)
        self.assertFalse(unchanged["blocked"])
        self.assertEqual(unchanged["status"], "evidence_recovery")
        self.assertFalse(unchanged["delivery_ready"])
        self.assertEqual(r.revision_round, 0)

        saved_id = r.deferred_global_findings[0]["finding_id"]
        global_client.responder = lambda _: global_review(r) | {
            "resolved_external_findings": [dict(finding_id=saved_id,
                                                 evidence="已有來源",
                                                 reason="此自由文字不構成 typed proof")]
        }
        rejected = r.execute_phase6(verifier, deep, patcher, global_client)
        self.assertFalse(rejected["blocked"])
        self.assertEqual(rejected["status"], "evidence_recovery")
        self.assertTrue(rejected["rejected_external_resolutions"])

        self.add_evidence_reference(r, "b", "ev.new_data")
        global_client.responder = lambda _: global_review(r) | {
            "resolved_external_findings": [dict(finding_id=saved_id,
                                                 evidence="引用新的 evidence object",
                                                 reason="新來源已由該 Claim 直接引用",
                                                 claim_ids=["b"], resolution_refs=["ev.new_data"])]
        }
        completed = r.execute_phase6(verifier, deep, patcher, global_client)
        self.assertFalse(completed["blocked"], completed)
        self.assertEqual(r.deferred_global_findings, [])
        self.assertEqual(r.revision_round, 0)

    def test_evidence_route_requires_typed_request_and_valid_route(self):
        from .semantic_contracts import normalize_semantic_finding_fields, semantic_review_schema
        base = dict(issue="Need the original filing", resolution_route="evidence_needed",
                    resolution_reason="The provided evidence does not establish the reported item.")
        with self.assertRaisesRegex(ValueError, "typed evidence_request"):
            normalize_semantic_finding_fields(base)
        with self.assertRaisesRegex(ValueError, "invalid semantic finding"):
            normalize_semantic_finding_fields(base | dict(resolution_route=[]))
        with self.assertRaisesRegex(ValueError, "requires resolution_route"):
            normalize_semantic_finding_fields(base | dict(resolution_route="claim_patch",
                evidence_request=dict(proposition="Find the filing")))
        with self.assertRaisesRegex(ValueError, "evidence_request"):
            normalize_semantic_finding_fields(base | dict(evidence_request=None))
        with self.assertRaisesRegex(ValueError, "unknown IDs"):
            normalize_semantic_finding_fields(base | dict(evidence_request={
                "proposition": "Find the filing", "refs": ["invented.source"]}),
                allowed_evidence_refs=[])
        normalized = normalize_semantic_finding_fields(base | dict(evidence_request={
            "proposition": "Find the filing"}))
        self.assertEqual(normalized["evidence_request"], dict(
            proposition="Find the filing", refs=[], entity="", metric="", period="", unit="",
            basis="", scope="", known_data_range=dict(start="", end="")))

        claim = next(c for c in seeded().run.claims if c["claim_id"] == "a")
        schema = semantic_review_schema(claim)
        request_refs = schema["properties"]["findings"]["items"]["properties"][
            "evidence_request"]["properties"]["refs"]
        if claim_reference_ids(claim):
            self.assertEqual(request_refs["items"]["enum"], claim_reference_ids(claim))
        else:
            self.assertEqual(request_refs["maxItems"], 0)

    def test_only_accepted_deep_evidence_finding_becomes_durable_work(self):
        from .checkpoint import load_checkpoint, save_checkpoint

        def reviewed_runner(deep_status):
            runner = seeded()
            prepared = runner.prepare_phase6_atomic()
            tasks = {task.claim_id: task for task in build_atomic_tasks(runner.run)}
            results = {cid: verdict(cid, ref_ids=reference_ids(task))
                       for cid, task in tasks.items()}
            results["a"] = verdict("a", "SUSPICIOUS", ref_ids=reference_ids(tasks["a"]))
            results["a"]["findings"] = [dict(issue="Source does not establish the period.",
                resolution_route="evidence_needed", resolution_reason="The period is absent.",
                evidence_request=dict(proposition="Find a source for the period."))]
            runner.collect_phase6_atomic(results, model_identity="test/atomic")
            deep = runner.prepare_phase6_deep_review()
            deep_result = verdict("a", deep_status, ref_ids=reference_ids(tasks["a"]))
            if deep_status != "PASS":
                deep_result["findings"] = [dict(issue="The period still lacks source support.",
                    resolution_route="evidence_needed", resolution_reason="The reviewed evidence is insufficient.",
                    evidence_request=dict(proposition="Find a source for the period."))]
            runner.collect_phase6_deep_review({"a": deep_result}, model_identity="test/deep")
            return runner

        passed = reviewed_runner("PASS")
        self.assertIsNone(passed.prepare_evidence_work())
        self.assertEqual(passed.evidence_requests, [])

        accepted = reviewed_runner("FAIL")
        work = accepted.prepare_evidence_work()
        self.assertEqual(work["status"], "evidence_recovery")
        self.assertEqual(len(work["evidence_requests"]), 1)
        record = work["evidence_requests"][0]
        finding_id = record["finding_id"]
        self.assertTrue(finding_id.startswith("deep."))
        self.assertEqual(record["claim_ids"], ["a"])
        self.assertEqual(accepted._claim_review_findings()[0]["finding_id"], finding_id)
        self.assertEqual(accepted.prepare_evidence_work()["evidence_requests"][0]["request_id"],
                         record["request_id"])
        baseline = accepted.deep_finding_baselines[finding_id]

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(accepted, path)
            restored = load_checkpoint(path, continue_verification=False)
        self.assertEqual(restored.evidence_requests, accepted.evidence_requests)
        self.assertEqual(restored.deep_finding_baselines[finding_id], baseline)

    def test_unmapped_external_finding_needs_explicit_global_resolution(self):
        r = seeded(); review_all(r)
        finding = dict(claim_ids=[], unresolved=True, issue="缺少來源證據",
                       evidence="來源資料尚未提供", materiality="material",
                       resolution_route="evidence_needed", resolution_reason="需要補上來源資料",
                       evidence_request=dict(proposition="取得可支持原始主張的來源"))
        global_calls = []
        def global_response(_prompt):
            global_calls.append(True)
            return global_review(r, [finding] if len(global_calls) == 1 else [])
        global_client = CloudDouble("test/global", global_response)
        clients = (CloudDouble(), CloudDouble("test/deep"), CloudDouble("test/patch"), global_client)

        first = r.execute_phase6(*clients)
        self.assertTrue(first["blocked"])
        saved_id = r.deferred_global_findings[0]["finding_id"]

        next(c for c in r.run.claims if c["claim_id"] == "role.bear")["text"] = "無關的角色觀察更新。"
        second = r.execute_phase6(*clients)

        self.assertTrue(second["blocked"])
        self.assertEqual(second["status"], "blocked:resolution_required")
        self.assertEqual([f["finding_id"] for f in r.deferred_global_findings], [saved_id])
        with self.assertRaises(ValueError):
            r.finalize()

        global_client.responder = lambda _prompt: global_review(r) | {
            "resolved_external_findings": [dict(finding_id=saved_id,
                                                 evidence="已新增可核對的來源資料",
                                                 reason="新來源直接補足原先缺漏",
                                                 claim_ids=["b"], resolution_refs=["ev.followup"])]
        }
        free_prose = r.execute_phase6(*clients)
        self.assertTrue(free_prose["blocked"])
        self.assertTrue(free_prose["rejected_external_resolutions"])
        self.add_evidence_reference(r, "b", "ev.followup")
        resolved = r.execute_phase6(*clients)
        self.assertFalse(resolved["blocked"], resolved)
        self.assertEqual(r.deferred_global_findings, [])

    def test_model_cannot_clear_human_decision_route(self):
        r = seeded(); review_all(r)
        finding = dict(claim_ids=["b"], issue="需要使用者決定風險偏好", evidence="現有資訊無法決定可承受損失",
                       materiality="material", resolution_route="human_decision",
                       resolution_reason="需要使用者選擇最大風險")
        r.prepare_phase6_global_sweep()
        first = r.collect_phase6_global_sweep(global_review(r, [finding]), model_identity="test/global")
        finding_id = r.deferred_global_findings[0]["finding_id"]

        r.prepare_phase6_global_sweep()
        second = r.collect_phase6_global_sweep(
            global_review(r) | {"resolved_external_findings": [dict(
                finding_id=finding_id, evidence="我已替使用者決定", reason="模型認為偏好已明確",
                claim_ids=["b"], resolution_refs=["b"])]}, model_identity="test/global")

        self.assertTrue(first["blocked"])
        self.assertTrue(second["blocked"])
        self.assertEqual(second["rejected_external_resolutions"][0]["finding_id"], finding_id)
        self.assertEqual(r.deferred_global_findings[0]["finding_id"], finding_id)

    def test_role_redeliberation_requires_received_original_role_output(self):
        from .journal import RunJournal
        from .schema import Claim

        r = seeded(); review_all(r)
        original = copy.deepcopy(next(c for c in r.run.claims if c["claim_id"] == "b"))
        finding = dict(claim_ids=["b"], issue="原角色結論需要重新裁決", evidence="原主張仍未解決",
                       materiality="material", resolution_route="role_redeliberation",
                       resolution_reason="需要原作者重新確認結論")
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            r.prepare_phase6_global_sweep()
            r.collect_phase6_global_sweep(global_review(r, [finding]), model_identity="test/global")
            finding_id = r.deferred_global_findings[0]["finding_id"]

            # A manual edit is not evidence that the author reconsidered the claim.
            next(c for c in r.run.claims if c["claim_id"] == "b")["text"] = "手動修改，沒有原角色重新回覆。"
            review_all(r)
            r.prepare_phase6_global_sweep()
            manual_edit = r.collect_phase6_global_sweep(global_review(r) | {
                "resolved_external_findings": [dict(finding_id=finding_id, evidence="文字已改",
                                                     reason="只有手動編輯", claim_ids=["b"],
                                                     resolution_refs=["b"])]}, model_identity="test/global")
            self.assertTrue(manual_edit["blocked"])

            # A response from the wrong role cannot satisfy the route either.
            wrong_role = copy.deepcopy(original)
            wrong_role["author_role"] = "bear"
            rejected_response = r.journal.complete(
                r, CloudDouble("test/role", lambda _: {"claims": [wrong_role]}),
                "bull", "重新裁決 b")
            self.assertEqual(rejected_response["claims"][0]["author_role"], "bear")
            r.prepare_phase6_global_sweep()
            wrong_attempt = r.collect_phase6_global_sweep(global_review(r) | {
                "resolved_external_findings": [dict(finding_id=finding_id, evidence="有新回覆",
                                                     reason="回覆作者與原角色不符", claim_ids=["b"],
                                                     resolution_refs=[row["attempt_id"] for row in
                                                         r.journal.received_attempts(r.run_id)
                                                         if row["task_id"] == "bull"])]}, model_identity="test/global")
            self.assertTrue(wrong_attempt["blocked"])

            # Only a durable, post-finding response from the original role that
            # exactly matches the current canonical Claim can resolve it.
            revised = copy.deepcopy(original)
            revised["text"] = "原作者重新分析後改變了結論。"
            r.journal.complete(r, CloudDouble("test/role", lambda _: {"claims": [revised]}),
                               "bull", "重新裁決 b")
            attempt = next(row for row in r.journal.received_attempts(r.run_id)
                           if row["task_id"] == "bull"
                           and row["response"].get("claims", [{}])[0].get("text") == revised["text"])
            r.run.claims = [Claim.from_dict(revised).to_dict() if c["claim_id"] == "b" else c
                            for c in r.run.claims]
            r.orch._sync_views(r.run)
            review_all(r)
            _, prompt = r.prepare_phase6_global_sweep()
            payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
            records = payload["external_resolution_candidates"][0]["role_redeliberation_records"]
            self.assertEqual(records, [dict(attempt_id=attempt["attempt_id"], task_id="bull", claim_ids=["b"])])
            resolved = r.collect_phase6_global_sweep(global_review(r) | {
                "resolved_external_findings": [dict(finding_id=finding_id, evidence="原角色新回覆已存檔",
                                                     reason="原角色重新裁決並重新驗證", claim_ids=["b"],
                                                     resolution_refs=[attempt["attempt_id"]])]},
                model_identity="test/global")
            self.assertFalse(resolved["blocked"], resolved)

    def test_schema_retries_do_not_preserve_legacy_pass_or_skip_deep_review(self):
        from .semantic_contracts import semantic_review_schema
        class StructuredDouble(CloudDouble):
            def complete(self, _):
                raise AssertionError("semantic requests must use schema capability")

            def complete_structured(self, prompt, schema):
                cid = schema["properties"]["claim_id"]["enum"][0]
                self.assert_schema(cid, schema)
                return super().complete(prompt)
        r, counts, deep_calls = seeded(), Counter(), []
        def atomic(prompt):
            payload = json.loads(prompt.split("```json\n")[1].split("\n```\n")[0])
            cid = payload["claim_id"]
            counts[cid] += 1
            result = verdict(cid, ref_ids=reference_ids(payload))
            if cid == "b" and counts[cid] == 1:
                result.pop("reference_checks")
            elif cid == "b" and counts[cid] == 2:
                result = verdict(cid, "FAIL", ref_ids=["a"])
                result["reference_checks"] = source_checks(["a"], "CONTRADICTS")
            return result
        def deep(_):
            deep_calls.append(True)
            result = verdict("b", "FAIL", ref_ids=["a"])
            result["reference_checks"] = source_checks(["a"], "CONTRADICTS")
            if len(deep_calls) == 1:
                result.pop("reference_checks")
            return result
        first, second = StructuredDouble("test/shared", atomic), StructuredDouble("test/shared", deep)
        first.assert_schema = second.assert_schema = lambda cid, s: self.assertEqual(
            s, semantic_review_schema(next(c for c in r.run.claims if c["claim_id"] == cid)))
        patcher = CloudDouble("test/author", lambda _: [dict(claim_id="b", text="corrected semantic attribution")])
        result = r.execute_phase6(first, second, patcher, CloudDouble("test/global", lambda _: global_review(r)))
        self.assertFalse(result["blocked"], result)
        self.assertEqual((counts["b"], counts["c"], len(deep_calls), len(patcher.calls)), (3, 2, 2, 1))
        self.assertIn("corrected semantic attribution", result["report_markdown"])

    def test_contract_retry_rejects_source_state_changed_in_flight(self):
        r = seeded()
        results = self.atomic_results(r)
        results["b"].pop("reference_checks")
        result = r.collect_phase6_atomic(results, model_identity="test/first")
        self.assertTrue(result["needs_retry"])
        r.run.claims[0]["text"] = "new upstream state"
        with self.assertRaisesRegex(ValueError, "state changed"):
            r.collect_phase6_atomic({"b": verdict("b", ref_ids=["a"])}, model_identity="test/first")
        self.assertNotIn("b", r.receipts)
        with self.assertRaises(ValueError):
            r.finalize()

    def test_invalid_atomic_contract_returns_retry_plan_and_keeps_valid_receipts(self):
        from .journal import RunJournal
        r = seeded()
        results = self.atomic_results(r)
        results["b"].pop("reference_checks")
        with tempfile.TemporaryDirectory() as temp:
            r.journal = RunJournal(temp)
            result = r.collect_phase6_atomic(results, model_identity="test/first")
            self.assertTrue(result["blocked"])
            self.assertTrue(result["needs_retry"])
            self.assertEqual(result["retry_claim_ids"], ["b"])
            self.assertEqual(set(r.receipts), set(results) - {"b"})
            self.assertNotIn("b", json.loads((Path(temp) / "checkpoint.json").read_text())["audit"]["atomic"])
            self.assertEqual([cid for cid, _ in result["retry_prompts"]], ["b"])
            with self.assertRaises(ValueError):
                r.prepare_phase6_global_sweep()
            repaired = r.collect_phase6_atomic({"b": verdict("b", ref_ids=["a"])}, model_identity="test/first")
            self.assertFalse(repaired["blocked"])

    def test_87_cached_legacy_results_are_semantically_regenerated(self):
        from .test_contracts import claim
        r = seeded()
        r.run.claims.extend(claim("bulk." + str(i)) for i in range(87 - len(r.run.claims)))
        r.orch._sync_views(r.run)
        prepared = r.prepare_phase6_atomic()
        from .cloud import structured_prompt
        cache = {structured_prompt(prompt, json.dumps(prepared["response_schemas"][cid], ensure_ascii=False,
                                                        separators=(",", ":"))):
                 dict(claim_id=cid, verdict="PASS", findings=[])
                 for cid, prompt in prepared["atomic_prompts"]}
        generated = []
        def respond(prompt):
            if prompt in cache:
                return copy.deepcopy(cache[prompt])
            payload = json.loads(prompt.split("```json\n")[1].split("\n```\n")[0])
            generated.append(payload["claim_id"])
            return verdict(payload["claim_id"], ref_ids=reference_ids(payload))
        verifier, deep = CloudDouble("test/first", respond), CloudDouble("test/deep")
        result = r.execute_phase6(verifier, deep, CloudDouble("test/author"),
                                 CloudDouble("test/global", lambda _: global_review(r)))
        self.assertFalse(result["blocked"], result)
        refs_required = {c["claim_id"] for c in r.run.claims if claim_reference_ids(c)}
        self.assertEqual(len(verifier.calls), len(r.run.claims) + len(refs_required))
        self.assertEqual(set(generated), refs_required)
        self.assertEqual(len(generated), len(refs_required))
        self.assertEqual(deep.calls, [])
        self.assertTrue(all("reference_checks" in x["result"] for x in r.receipts.values()))

    def test_contract_retry_limit_blocks_without_repeating_valid_claims(self):
        r = seeded()
        counts = Counter()
        def respond(prompt):
            payload = json.loads(prompt.split("```json\n")[1].split("\n```\n")[0])
            cid = payload["claim_id"]
            counts[cid] += 1
            result = verdict(cid, ref_ids=reference_ids(payload))
            if cid == "b":
                result.pop("reference_checks")
            return result
        deep, sweep = CloudDouble("test/deep"), CloudDouble("test/global")
        result = r.execute_phase6(CloudDouble("test/first", respond), deep, CloudDouble("test/author"), sweep)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["status"], "blocked:atomic_contract")
        self.assertFalse(result["needs_retry"])
        self.assertEqual(result["invalid_claim_ids"], ["b"])
        self.assertEqual(counts["b"], 3)
        self.assertTrue(all(n == 1 for cid, n in counts.items() if cid != "b"))
        self.assertEqual((deep.calls, sweep.calls), ([], []))
        self.assertNotIn("report_markdown", result)

    def test_identical_findings_keep_independent_targets_across_reload(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        content = dict(issue="same wording, different claims", claim_ids=[], unresolved=True)
        result = r.collect_phase6_global_sweep(global_review(
            r, findings=[content, copy.deepcopy(content)], required={"independent": ["a"]}), model_identity="test/global")
        self.assertEqual(len(r.deferred_global_findings), 2)
        r.prepare_phase6_revision_v4(result["dependency_findings"])
        r.collect_phase6_revision(result["dependency_patches"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "identical.json"
            save_checkpoint(r, path)
            saved = json.loads(path.read_text())
            for malformed in ("missing", "duplicate"):
                data = copy.deepcopy(saved)
                if malformed == "missing":
                    data["deferred_global_findings"][0].pop("finding_id")
                else:
                    data["deferred_global_findings"][1]["finding_id"] = data["deferred_global_findings"][0]["finding_id"]
                path.write_text(json.dumps(data))
                with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                    load_checkpoint(path)
            path.write_text(json.dumps(saved))
            r = load_checkpoint(path)
        review_all(r)
        role, prompt = r.prepare_phase6_global_sweep()
        payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
        self.assertEqual(payload["deferred_global_findings"], r.deferred_global_findings)
        self.assertEqual(len({f["finding_id"] for f in payload["deferred_global_findings"]}), 2)
        rechecked = r.collect_phase6_global_sweep(global_review(r, findings=[
            dict(issue="same wording, different claims", claim_ids=["b"]),
            dict(issue="same wording, different claims", claim_ids=["c"])]), model_identity="test/global")
        self.assertEqual([f["claim_ids"] for f in rechecked["findings"]], [["b"], ["c"]])
        self.assertEqual(len({f["finding_id"] for f in rechecked["findings"]}), 2)
        revision = r.prepare_phase6_revision_v4(rechecked["findings"])
        self.assertEqual(revision["allowed_claim_ids"], ["b", "c"])

    def test_deferred_content_finding_survives_graph_repair_and_reload(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        content = dict(issue="stale decision", claim_ids=[], unresolved=True)
        result = r.collect_phase6_global_sweep(global_review(r, findings=[content], required={"independent": ["a"]}), model_identity="test/global")
        content = result["findings"][0]
        r.prepare_phase6_revision_v4(result["dependency_findings"])
        r.collect_phase6_revision(result["dependency_patches"])
        self.assertEqual(r.deferred_global_findings, [content])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "deferred.json"
            save_checkpoint(r, path)
            restored = load_checkpoint(path)
            self.assertEqual(restored.deferred_global_findings, [content])
            linker = CloudDouble("test/linker", lambda _: [dict(finding_index=0, claim_ids=["b"], unresolved=False)])
            patcher = CloudDouble("test/author", lambda _: [dict(claim_id="b", text="new decision")])
            prompts = []
            def clean_global(prompt):
                payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
                prompts.append(payload)
                return global_review(restored)
            global_model = CloudDouble("test/global", clean_global)
            delivered = restored.execute_phase6(CloudDouble(), CloudDouble("test/deep"), patcher, global_model, linker)
            self.assertFalse(delivered["blocked"], delivered)
            self.assertEqual(restored.deferred_global_findings, [])
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["deferred_global_findings"], [content])
            self.assertEqual(linker.calls, [])
            self.assertEqual(patcher.calls, [])

    def test_dependencies_only_patch_invalidates_target_finding_and_revalidates_descendants(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r = seeded(); review_all(r)
        target = dict(finding_id="fixture.dependency",issue="declared causal premise missing",claim_ids=["b"])
        untouched = dict(finding_id="fixture.other",issue="separate issue",claim_ids=["independent"])
        unmapped = dict(finding_id="fixture.unmapped",issue="needs semantic linking",claim_ids=[],unresolved=True)
        r.deferred_global_findings = [target,untouched,unmapped]
        original = copy.deepcopy(next(c for c in r.run.claims if c["claim_id"]=="b"))
        retained = copy.deepcopy(r.receipts["independent"])
        r.prepare_phase6_revision_v4([target])
        result = r.collect_phase6_revision([dict(claim_id="b",dependencies=["a","independent"])])
        after = next(c for c in r.run.claims if c["claim_id"]=="b")
        self.assertEqual({k for k in original if original[k]!=after[k]}, {"dependencies"})
        self.assertEqual({f["finding_id"] for f in r.deferred_global_findings},
                         {untouched["finding_id"], unmapped["finding_id"]})
        self.assertEqual(set(result["revalidate_claim_ids"]),{"b","c"})
        self.assertFalse({"b","c"}.intersection(r.receipts))
        self.assertEqual(r.receipts["independent"],retained)
        self.assertIsNone(r.global_receipt)
        self.assertIn(target,r.history[-1]["findings"])
        self.assertEqual(r.history[-1]["invalidated_global_findings"],[target])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"patched.json"; save_checkpoint(r,path)
            restored=load_checkpoint(path)
            self.assertEqual({f["finding_id"] for f in restored.deferred_global_findings},
                             {untouched["finding_id"], unmapped["finding_id"]})
            self.assertIn(target,restored.history[-1]["findings"])
            self.assertEqual(restored.history[-1]["invalidated_global_findings"],[target])
            with self.assertRaises(ValueError): restored.finalize()

    def test_noop_dependency_patch_does_not_invalidate_unresolved_finding(self):
        r=seeded(); review_all(r)
        finding=dict(finding_id="fixture.noop",issue="still unresolved",claim_ids=["b"])
        r.deferred_global_findings=[finding]
        r.prepare_phase6_revision_v4([finding])
        r.collect_phase6_revision([dict(claim_id="b",dependencies=["a"])])
        self.assertEqual(r.deferred_global_findings,[finding])

    def test_dependency_patch_requires_new_global_verdict_and_can_still_fail(self):
        r=seeded(); review_all(r)
        finding=dict(finding_id="fixture.graph",issue="unresolved causal inconsistency",claim_ids=["b"])
        r.deferred_global_findings=[finding]
        r.prepare_phase6_revision_v4([finding])
        r.collect_phase6_revision([dict(claim_id="b",dependencies=["a","independent"])])
        self.assertEqual(r.deferred_global_findings,[])
        with self.assertRaises(ValueError): r.finalize()
        review_all(r)
        with self.assertRaises(ValueError): r.finalize()
        _,prompt=r.prepare_phase6_global_sweep()
        self.assertIn("findings_to_recheck",prompt)
        self.assertIn(finding["finding_id"],prompt)
        result=r.collect_phase6_global_sweep(global_review(r,findings=[dict(issue="still inconsistent",claim_ids=["b"])]),model_identity="test/global")
        self.assertTrue(result["needs_revision"])
        self.assertEqual(result["findings"][0]["issue"],"still inconsistent")
        with self.assertRaises(ValueError): r.finalize()

    def test_graph_only_last_revision_can_finish_after_revalidation_without_another_patch(self):
        r=seeded(); r.max_revisions=1; review_all(r)
        finding=dict(finding_id="fixture.last",issue="missing causal edge",claim_ids=["b"])
        r.deferred_global_findings=[finding]
        r.prepare_phase6_revision_v4([finding])
        r.collect_phase6_revision([dict(claim_id="b",dependencies=["a","independent"])])
        author=CloudDouble("test/author",lambda _: self.fail("must not spend another patch round"))
        first=CloudDouble()
        sweep=CloudDouble("test/global",lambda _: global_review(r))
        result=r.execute_phase6(first,CloudDouble("test/deep"),author,sweep)
        self.assertFalse(result["blocked"],result)
        self.assertEqual(result["status"],"complete")
        self.assertEqual(r.revision_round,1)
        self.assertEqual(author.calls,[])
        self.assertEqual(len(first.calls),2)  # only b and its descendant c
        self.assertEqual(len(sweep.calls),1)

    def test_new_finding_invalidation_audit_cannot_disappear_or_change_shape_on_reload(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r=seeded(); review_all(r)
        finding=dict(finding_id="fixture.audit",issue="missing causal edge",claim_ids=["b"])
        r.deferred_global_findings=[finding]
        r.prepare_phase6_revision_v4([finding])
        r.collect_phase6_revision([dict(claim_id="b",dependencies=["a","independent"])])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"audit.json"; save_checkpoint(r,path)
            original=json.loads(path.read_text())
            for bad in ('missing',None,{},[finding,finding],[dict(finding_id="bad",issue="issue",claim_ids=["unknown"])],
                        [dict(finding_id="bad",issue="issue",claim_ids=["independent"])],
                        [dict(finding_id="bad",issue="",claim_ids=["b"]) ]):
                damaged=copy.deepcopy(original)
                row=damaged["history"][-1]
                if bad == 'missing': row.pop("invalidated_global_findings")
                else: row["invalidated_global_findings"]=bad
                path.write_text(json.dumps(damaged))
                with self.subTest(bad=bad),self.assertRaises(ValueError): load_checkpoint(path)
            for bad_format in (None,0,False,{},[],"unknown"):
                damaged=copy.deepcopy(original)
                damaged["history"][-1]["format"]=bad_format
                damaged["history"][-1].pop("invalidated_global_findings")
                path.write_text(json.dumps(damaged))
                with self.subTest(format=bad_format),self.assertRaises(ValueError): load_checkpoint(path)
            path.write_text(json.dumps(original))
            restored=load_checkpoint(path)
            self.assertEqual(restored.history[-1]["invalidated_global_findings"],[finding])
            # Legacy histories genuinely lack the new audit contract; preserve compatibility.
            legacy=copy.deepcopy(original)
            legacy["history"][-1].pop("format")
            legacy["history"][-1].pop("invalidated_global_findings")
            path.write_text(json.dumps(legacy))
            self.assertEqual(len(load_checkpoint(path).history),1)

    def test_returned_dependency_rows_cannot_mutate_saved_audit(self):
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        result = r.collect_phase6_global_sweep(global_review(r), model_identity="test/global")
        result["dependency_reviews"].clear()
        self.assertEqual(len(r.global_receipt["dependency_reviews"]), len(r.run.claims))
        self.assertFalse(r.finalize()["blocked"])

    def test_full_causal_repair_from_phantom_binding_and_missing_edge(self):
        r = seeded()
        source_id, decision_id = "role.fundamentals_analyst", "role.research_manager"
        fact = Fact("fact.lease", 329000000000, "USD", "2026-Q2", entity="FixtureIssuer",
                    concept="LeaseCommitments", scope="consolidated", basis="reported",
                    source_id="fixture", source_pointer="lease note", source_hash="fixture")
        r.run.fact_store.add(fact)
        for c in r.run.claims:
            if c["claim_id"] == source_id:
                c.update(text="3,290 億租約是營收能見度。", claim_type="source_fact", refs=[fact.fact_id],
                         fact_refs=[fact.fact_id], assertion=dict(kind="value", ref_id=fact.fact_id, value=fact.value),
                         bindings=[dict(ref_id=fact.fact_id, **{k: getattr(fact, k) for k in
                                       ("entity", "concept", "period", "scope", "unit", "basis")})])
            elif c["claim_id"] == decision_id:
                c.update(text="因為有 3,290 億營收背書，裁決偏多。", dependencies=[])
        r.orch._sync_views(r.run)
        self.assertFalse(r.orch.validate_stage(r.run_id, "phase6")[1])
        calls, patches, sweeps = Counter(), [], []

        def atomic(prompt):
            task = json.loads(prompt.split("```json\n")[1].split("\n```")[0])
            cid = task["claim_id"]
            calls[cid] += 1
            # Scripted semantic outcomes, not a Python inference implementation.
            fail = (cid == source_id and calls[cid] == 1) or (cid == decision_id and calls[cid] == 2)
            result = verdict(cid, "FAIL" if fail else "PASS")
            result["reference_checks"] = source_checks(reference_ids(task), "CONTRADICTS" if fail else "SUPPORTS")
            return result

        def deep(prompt):
            task = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])["task"]
            return {**verdict(task["claim_id"], "FAIL"), "reference_checks": source_checks(reference_ids(task), "CONTRADICTS")}

        def patcher(prompt):
            payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
            ids = [t["claim_id"] for t in payload["tasks"]]
            patches.append(ids)
            if ids == [source_id]:
                return [dict(claim_id=source_id, text="3,290 億是未來租金負債。")]
            self.assertEqual(ids, [decision_id])
            return [dict(claim_id=decision_id, text="撤回營收背書理由，重新評估裁決。")]

        def sweep(prompt):
            payload = json.loads(prompt.split("<context-data>\n")[1].split("\n</context-data>")[0])
            sweeps.append(payload)
            if len(sweeps) == 1:
                changes = payload["history"][0]["claim_changes"]
                self.assertIn("營收能見度", changes[0]["before"]["text"])
                self.assertIn("租金負債", changes[0]["after"]["text"])
            return global_review(r, required={decision_id: [source_id]})

        result = r.execute_phase6(CloudDouble("test/atomic", atomic), CloudDouble("test/deep", deep),
                                 CloudDouble("test/author", patcher), CloudDouble("test/global", sweep))
        self.assertFalse(result["blocked"], result)
        self.assertEqual(patches, [[source_id], [decision_id]])
        self.assertEqual(calls[decision_id], 3)
        self.assertEqual(len(sweeps), 2)
        self.assertEqual(r.revision_round, 3)  # source patch, edge repair, decision patch
        self.assertNotIn("因為有 3,290 億營收背書", result["report_markdown"])
        self.assertEqual(next(c for c in r.run.claims if c["claim_id"] == decision_id)["dependencies"], [source_id])
        self.assertEqual(result["sidecar"]["atomic_reviews"][source_id]["result"]["reference_checks"][0]["relation"], "SUPPORTS")
        # Persistence must retain before-images and discard old review receipts.
        from .checkpoint import save_checkpoint, load_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "causal.json"
            save_checkpoint(r, path)
            restored = load_checkpoint(path)
            self.assertEqual(restored.history, r.history)
            self.assertEqual(restored.receipts, {})

    def test_mixed_missing_edge_and_unlinked_content_finding_both_get_resolved(self):
        r = seeded()
        sweeps, links, authored = [], [], []
        def sweep(_):
            sweeps.append(True)
            if len(sweeps) == 1:
                return global_review(r, findings=[dict(issue="outdated interpretation", claim_ids=[], unresolved=True)],
                                     required={"independent": ["a"]})
            if len(sweeps) == 2:
                return global_review(r, findings=[dict(issue="outdated interpretation", claim_ids=[], unresolved=True)])
            return global_review(r)
        def linker(_):
            links.append(True)
            return [dict(finding_index=0, claim_ids=["b"], unresolved=False)]
        def patcher(_):
            authored.append(True)
            return [dict(claim_id="b", text="updated interpretation")]
        result = r.execute_phase6(CloudDouble(), CloudDouble("test/deep"), CloudDouble("test/author", patcher),
                                 CloudDouble("test/global", sweep), CloudDouble("test/linker", linker))
        self.assertFalse(result["blocked"], result)
        self.assertEqual((len(sweeps), len(links), len(authored)), (3, 1, 1))
        self.assertEqual(r.deferred_global_findings, [])

    def test_unresolved_or_stale_dependency_audit_blocks_delivery(self):
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        result = r.collect_phase6_global_sweep(global_review(r, unresolved=["a"]), model_identity="test/global")
        self.assertTrue(result["blocked"])
        with self.assertRaises(ValueError):
            r.finalize()
        r.run.claims[0]["text"] = "changed while global review in flight"
        with self.assertRaises(ValueError):
            r.collect_phase6_global_sweep(global_review(r), model_identity="test/global")
        self.assertIsNone(r.global_receipt)

    def test_missing_original_history_objects_are_rejected_on_reload(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r = seeded()
        review_all(r)
        r.prepare_phase6_revision_v4([dict(claim_id="a", issue="change")])
        r.collect_phase6_revision([dict(claim_id="a", text="corrected")])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.json"
            save_checkpoint(r, path)
            good = json.loads(path.read_text())
            for malformed in (None, [], [None], [dict(claim_id="a")]):
                data = copy.deepcopy(good)
                data["history"][0]["claim_changes"] = malformed
                path.write_text(json.dumps(data))
                with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                    load_checkpoint(path)

    def test_deep_pass_cannot_bypass_missing_reference_coverage(self):
        r = seeded()
        results = self.atomic_results(r)
        results["b"] = {**verdict("b", "FAIL"), "reference_checks": source_checks(["a"], "CONTRADICTS")}
        r.collect_phase6_atomic(results, model_identity="test/first")
        r.prepare_phase6_deep_review()
        for rows in ([], source_checks(["a", "a"]), source_checks(["unknown"]), source_checks(["a"], "INSUFFICIENT")):
            with self.subTest(rows=rows):
                raw = {**verdict("b"), "reference_checks": rows}
                with self.assertRaises(ValueError):
                    normalize_atomic_result("b", raw, expected_ref_ids=["a"])
                result = r.collect_phase6_deep_review({"b": raw}, model_identity="test/second")
                self.assertTrue(result["blocked"])
                self.assertEqual(result["invalid_claim_ids"], ["b"])
                self.assertEqual(r.deep_receipts, {})
                with self.assertRaises(ValueError):
                    r.prepare_phase6_global_sweep()
        r.collect_phase6_deep_review({"b": {**verdict("b"), "reference_checks": source_checks(["a"])}}, model_identity="test/second")

    def atomic_results(self, r):
        prepared = r.prepare_phase6_atomic()
        tasks = {t.claim_id: t for t in build_atomic_tasks(r.run)}
        return {cid: {**verdict(cid), "reference_checks": source_checks(reference_ids(tasks[cid]))}
                for cid, _ in prepared["atomic_prompts"]}

    def test_pass_cannot_omit_duplicate_or_invent_reference_checks(self):
        for mutation in ("omit", "duplicate", "unknown"):
            r = seeded()
            results = self.atomic_results(r)
            if mutation == "omit":
                results["b"]["reference_checks"] = []
            elif mutation == "duplicate":
                results["b"]["reference_checks"] *= 2
            else:
                results["b"]["reference_checks"] = source_checks(["not-a-ref"])
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    normalize_atomic_result("b", results["b"], expected_ref_ids=["a"])
                result = r.collect_phase6_atomic(results, model_identity="test/first")
                self.assertTrue(result["blocked"])
                self.assertEqual(result["invalid_claim_ids"], ["b"])
                self.assertNotIn("b", r.receipts)
                with self.assertRaises(ValueError):
                    r.prepare_phase6_global_sweep()

    def test_pass_cannot_override_semantic_contradiction_or_insufficient_evidence(self):
        for relation in ("CONTRADICTS", "INSUFFICIENT"):
            r = seeded()
            results = self.atomic_results(r)
            results["b"]["reference_checks"] = source_checks(["a"], relation)
            with self.subTest(relation=relation):
                with self.assertRaises(ValueError):
                    normalize_atomic_result("b", results["b"], expected_ref_ids=["a"])
                result = r.collect_phase6_atomic(results, model_identity="test/first")
                self.assertTrue(result["blocked"])
                self.assertEqual(result["invalid_claim_ids"], ["b"])
                self.assertNotIn("b", r.receipts)
                with self.assertRaises(ValueError):
                    r.prepare_phase6_global_sweep()

    def test_empty_global_array_is_no_longer_dependency_acceptance(self):
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        with self.assertRaises(ValueError):
            r.collect_phase6_global_sweep([], model_identity="test/global")
        self.assertIsNone(r.global_receipt)

    def test_global_requires_every_claim_once_and_valid_dependency_ids(self):
        for mutation in ("omit", "duplicate", "unknown", "self", "cycle"):
            r = seeded()
            review_all(r)
            r.prepare_phase6_global_sweep()
            result = global_review(r)
            if mutation == "omit":
                result["dependency_reviews"].pop()
            elif mutation == "duplicate":
                result["dependency_reviews"][-1] = copy.deepcopy(result["dependency_reviews"][0])
            elif mutation == "unknown":
                result["dependency_reviews"][0]["required_dependencies"] = ["missing"]
            elif mutation == "self":
                result["dependency_reviews"][0]["required_dependencies"] = ["a"]
            else:
                result["dependency_reviews"][0]["required_dependencies"] = ["c"]
            before = copy.deepcopy(r.run.claims)
            if mutation == "omit":
                pending = r.collect_phase6_global_sweep(result, model_identity="test/global")
                self.assertTrue(pending["needs_dependency_completion"])
                self.assertEqual(pending["missing_dependency_claim_ids"], [r.run.claims[-1]["claim_id"]])
                with self.assertRaises(ValueError): r.finalize()
            else:
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    r.collect_phase6_global_sweep(result, model_identity="test/global")
            self.assertEqual(r.run.claims, before)
            self.assertIsNone(r.global_receipt)

    def test_unresolved_dependency_retains_reason_and_enters_surgical_revision(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
        response = global_review(r, unresolved=("b",))
        row = next(row for row in response["dependency_reviews"] if row["claim_id"] == "b")
        row["reason"] = "Cannot establish whether the conclusion relies on the prior premise"
        result = r.collect_phase6_global_sweep(response, model_identity="test/global")
        self.assertTrue(result["needs_revision"])
        self.assertEqual(result["status"], "revision")
        finding = next(f for f in result["findings"] if f["category"] == "dependency_unresolved")
        self.assertEqual(finding["evidence"], row["reason"])
        self.assertEqual(finding["dependency_review"], row)
        self.assertEqual(finding["declared_dependencies"], ["a"])
        self.assertEqual(result["dependency_patches"], [])
        response["dependency_reviews"].clear()
        self.assertEqual(len(r.global_receipt["dependency_reviews"]), len(r.run.claims))
        with self.assertRaises(ValueError): r.finalize()
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "checkpoint.json"; save_checkpoint(r, checkpoint)
            resumed = load_checkpoint(checkpoint)
            self.assertEqual(resumed.deferred_global_findings, r.deferred_global_findings)
            review_all(resumed)
            revision = resumed.prepare_phase6_revision_v4(resumed._review_findings())
            self.assertEqual(revision["allowed_claim_ids"], ["b", "c"])
            self.assertIn(row["reason"], revision["revision_prompts"][0][1])

    def test_unresolved_dependency_survives_another_claim_graph_repair(self):
        r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
        response = global_review(r, required={"independent": ["a"]}, unresolved=("b",))
        result = r.collect_phase6_global_sweep(response, model_identity="test/global")
        r.prepare_phase6_revision_v4(result["dependency_findings"])
        r.collect_phase6_revision(result["dependency_patches"])
        self.assertEqual([f["claim_id"] for f in r.deferred_global_findings], ["b"])
        self.assertEqual(r.deferred_global_findings[0]["dependency_review"]["reason"], "scripted dependency reasoning")
        with self.assertRaises(ValueError): r.finalize()

    def test_execution_repairs_uncertain_claim_then_revalidates_and_sweeps_again(self):
        r = seeded(); sweeps = []
        def sweep(_):
            sweeps.append(True)
            return global_review(r, unresolved=["b"] if len(sweeps) == 1 else [])
        patcher = CloudDouble("test/author", lambda _: [dict(claim_id="b", text="Clarified supported conclusion")])
        result = r.execute_phase6(CloudDouble(), CloudDouble("test/deep"), patcher,
                                 CloudDouble("test/global", sweep))
        self.assertFalse(result["blocked"], result)
        self.assertEqual((len(sweeps), len(patcher.calls)), (2, 1))
        self.assertEqual(r.deferred_global_findings, [])
        self.assertEqual(r.history[0]["findings"][0]["evidence"], "scripted dependency reasoning")
        self.assertEqual(set(r.history[0]["affected_claim_ids"]), {"b", "c"})

    def test_uncertainty_reason_is_visible_when_patcher_fails_or_budget_exhausts(self):
        for limit in (0, 1):
            with self.subTest(limit=limit):
                r = seeded(); r.max_revisions = limit
                def fail(_): raise RuntimeError("author unavailable")
                result = r.execute_phase6(CloudDouble(), CloudDouble("test/deep"),
                                         CloudDouble("test/author", fail),
                                         CloudDouble("test/global", lambda _: global_review(r, unresolved=["b"])))
                self.assertTrue(result["blocked"])
                self.assertEqual(result["status"], "blocked:revision_limit" if limit == 0 else "blocked:execution")
                self.assertEqual(result["findings"][0]["claim_id"], "b")
                self.assertEqual(result["findings"][0]["dependency_review"]["reason"], "scripted dependency reasoning")
                self.assertNotIn("report_markdown", result)

    def test_mixed_dependency_block_keeps_all_reasons_on_graph_failure_or_limit(self):
        from unittest.mock import patch
        for limit in (0, 1):
            with self.subTest(limit=limit):
                r = seeded(); r.max_revisions = limit
                reviewer = CloudDouble("test/global", lambda _: global_review(
                    r, required={"independent": ["a"]}, unresolved=["b"]))
                with patch.object(r.orch, "patch", side_effect=RuntimeError("graph repair failed")):
                    result = r.execute_phase6(CloudDouble(), CloudDouble("test/deep"),
                                             CloudDouble("test/author"), reviewer)
                self.assertTrue(result["blocked"])
                self.assertEqual({f["claim_id"] for f in result["findings"]}, {"independent", "b"})
                uncertain = next(f for f in result["findings"] if f["claim_id"] == "b")
                self.assertEqual(uncertain["dependency_review"]["reason"], "scripted dependency reasoning")
                self.assertNotIn("report_markdown", result)

    def test_missing_dependency_is_exact_metadata_patch_and_invalidates_descendants(self):
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        result = r.collect_phase6_global_sweep(global_review(r, required={"independent": ["a"]}), model_identity="test/global")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["dependency_patches"], [dict(claim_id="independent", dependencies=["a"])])
        with self.assertRaises(ValueError):
            r.finalize()
        r.prepare_phase6_revision_v4(result["dependency_findings"])
        pending = r.collect_phase6_revision(result["dependency_patches"])
        self.assertEqual(pending["revalidate_claim_ids"], ["independent"])
        self.assertNotIn("independent", r.receipts)
        r.collect_phase6_atomic({"independent": {**verdict("independent"), "reference_checks": source_checks(["a"])}}, model_identity="test/first")
        with self.assertRaises(ValueError):
            r.finalize()
        # Once the recovered edge is canonical, changing its upstream reaches
        # the formerly disconnected claim through normal descendant traversal.
        r.prepare_phase6_revision_v4([dict(claim_id="a", issue="change premise")])
        pending = r.collect_phase6_revision([dict(claim_id="a", text="corrected premise")])
        self.assertEqual(set(pending["revalidate_claim_ids"]), {"a", "b", "c", "independent"})


if __name__ == "__main__":
    unittest.main()
