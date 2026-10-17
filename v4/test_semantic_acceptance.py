"""Negative semantic verdict fixtures test gates, not model accuracy."""
import copy
import json
import unittest
from .test_integration import seeded, review_all, verdict, CloudDouble, reference_ids as claim_reference_ids
from v3.facts import Fact
from collections import Counter
from pathlib import Path
import tempfile
from .atomic_phase6 import build_atomic_tasks


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
        findings = r._review_findings()
        self.assertEqual(len({f["finding_id"] for f in findings}), 2)
        r.prepare_finding_linker(findings)
        linked = r.apply_finding_links(findings, [
            dict(finding_index=0, claim_ids=["b"], unresolved=False),
            dict(finding_index=1, claim_ids=["c"], unresolved=False)])
        self.assertEqual([f["claim_ids"] for f in r.deferred_global_findings], [["b"], ["c"]])
        r.prepare_phase6_revision_v4([linked[0]])
        r.collect_phase6_revision([dict(claim_id="b", text="first issue repaired")])
        self.assertEqual(r.deferred_global_findings, [linked[1]])
        review_all(r)
        with self.assertRaisesRegex(ValueError, "outstanding semantic findings"):
            r.prepare_phase6_global_sweep()

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
            global_model = CloudDouble("test/global", lambda _: global_review(restored))
            delivered = restored.execute_phase6(CloudDouble(), CloudDouble("test/deep"), patcher, global_model, linker)
            self.assertFalse(delivered["blocked"], delivered)
            self.assertEqual(restored.deferred_global_findings, [])
            self.assertEqual(len(linker.calls), 1)

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
        self.assertEqual((len(sweeps), len(links), len(authored)), (2, 1, 1))
        self.assertEqual(r.deferred_global_findings, [])

    def test_unresolved_or_stale_dependency_audit_blocks_delivery(self):
        r = seeded()
        review_all(r)
        r.prepare_phase6_global_sweep()
        with self.assertRaises(ValueError):
            r.collect_phase6_global_sweep(global_review(r, unresolved=["a"]), model_identity="test/global")
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
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                r.collect_phase6_deep_review({"b": {**verdict("b"), "reference_checks": rows}}, model_identity="test/second")
            self.assertEqual(r.deep_receipts, {})
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
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                r.collect_phase6_atomic(results, model_identity="test/first")
            self.assertEqual(r.receipts, {})

    def test_pass_cannot_override_semantic_contradiction_or_insufficient_evidence(self):
        for relation in ("CONTRADICTS", "INSUFFICIENT"):
            r = seeded()
            results = self.atomic_results(r)
            results["b"]["reference_checks"] = source_checks(["a"], relation)
            with self.subTest(relation=relation), self.assertRaises(ValueError):
                r.collect_phase6_atomic(results, model_identity="test/first")
            self.assertEqual(r.receipts, {})

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
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                r.collect_phase6_global_sweep(result, model_identity="test/global")
            self.assertEqual(r.run.claims, before)
            self.assertIsNone(r.global_receipt)

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
