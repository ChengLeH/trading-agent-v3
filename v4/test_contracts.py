"""Contract regressions; no network or model required."""
import unittest
import ast
from pathlib import Path
from v4.test_atomic_phase6 import make_run


def claim(cid="c1", deps=None):
    return dict(claim_id=cid, text="任意論述", claim_type="interpretation",
                refs=[], fact_refs=[], dependencies=deps or [], author_role="bull",
                assertion={"kind": "interpretation"}, bindings=[])


class Contracts(unittest.TestCase):
    def test_v4_production_has_no_regex_or_legacy_prose_entrypoint_imports(self):
        for path in Path(__file__).parent.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(a.name in {"re", "regex", "v3.runner", "v3.reporting"} for a in node.names), path)
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, {"re", "regex", "v3.runner", "v3.reporting"}, path)

    def test_duplicate_and_missing_ids_rejected_before_task_dispatch(self):
        from v4.atomic_phase6 import build_atomic_tasks
        for claims in ([claim(), claim()], [dict(text="missing")]):
            with self.subTest(claims=claims), self.assertRaises(ValueError):
                build_atomic_tasks(make_run(claims))

    def test_dependencies_are_in_context_even_without_refs(self):
        from v4.atomic_phase6 import build_atomic_tasks
        tasks = build_atomic_tasks(make_run([claim(), claim("c2", ["c1"])]))
        self.assertEqual([c["claim_id"] for c in tasks[1].dependency_claims], ["c1"])

    def test_cycles_and_dangling_dependencies_rejected(self):
        from v4.atomic_phase6 import build_atomic_tasks
        for claims in ([claim("a", ["b"]), claim("b", ["a"])],
                       [claim("a", ["missing"]) ]):
            with self.subTest(claims=claims), self.assertRaises(ValueError):
                build_atomic_tasks(make_run(claims))

    def test_binding_requires_all_dimensions_and_declared_ref(self):
        from v4.bindings import validate_claim_bindings
        c = claim()
        c.update(claim_type="source_fact", refs=["fact.dram_supply.2029"],
                 fact_refs=["fact.dram_supply.2029"], bindings=[{"ref_id": "fact.dram_supply.2029"}])
        findings = validate_claim_bindings([c], make_run([c]).fact_store, require_bindings=True)
        self.assertTrue(any(f.status == "FAIL" for f in findings))

    def test_nonpass_requires_actionable_findings(self):
        from v4.atomic_phase6 import normalize_atomic_result
        with self.assertRaises(ValueError):
            normalize_atomic_result("c1", dict(claim_id="c1", verdict="FAIL", findings=[]))

    def test_extra_reviews_rejected(self):
        from v4.atomic_phase6 import summarize_atomic_results
        with self.assertRaises(ValueError):
            summarize_atomic_results(["c1"], {
                cid: dict(claim_id=cid, verdict="PASS", findings=[]) for cid in ("c1", "ghost")})


if __name__ == "__main__":
    unittest.main()
