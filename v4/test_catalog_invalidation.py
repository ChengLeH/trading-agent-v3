"""Catalog-sensitive receipt fingerprints follow explicit typed claim scope."""
import unittest

from v3.facts import Fact
from v3.sources import SourceRecord
from .atomic_phase6 import build_atomic_prompt, build_atomic_tasks
from .schema import digest
from .test_atomic_phase6 import make_run


_OMIT = object()


def _claim(cid="claim", *, scope=_OMIT, refs=None, dependencies=None, text="任意論述"):
    assertion = {"kind": "interpretation"}
    if scope is not _OMIT:
        assertion["catalog_scope"] = scope
    return dict(claim_id=cid, claim_type="interpretation", text=text,
                refs=list(refs or []), fact_refs=[], dependencies=list(dependencies or []),
                author_role="bull", assertion=assertion, bindings=[])


def _task(run, claim_id):
    return next(task for task in build_atomic_tasks(run) if task.claim_id == claim_id)


def _source(source_id, source_hash, declarations=None):
    return SourceRecord(id=source_id, type="generic_text", subject_ticker="TEST",
                        subject_cik=None, payload={"version": source_hash}, source_hash=source_hash,
                        meta=({"financial_statement_declarations": declarations}
                              if declarations is not None else {}))


class CatalogInvalidation(unittest.TestCase):
    def test_referenced_scope_ignores_unreferenced_fact_and_source_declaration(self):
        run = make_run([_claim(scope="referenced_sources", refs=["fact.dram_supply.2029"])])
        run.records = [_source("fixture", "used-v1", {"origin": "used"})]
        before = _task(run, "claim")
        before_hash = digest(before.to_dict())

        run.fact_store.add(Fact(fact_id="unrelated.fact", value=1, unit="USD", period="FY2030",
                                concept="UnrelatedMetric", source_id="unrelated.source"))
        run.records.append(_source("unrelated.source", "unrelated-v1", {"origin": "unrelated"}))

        after = _task(run, "claim")
        self.assertEqual(digest(after.to_dict()), before_hash)
        self.assertEqual(after.context["source_declarations"], {"fixture": {"origin": "used"}})
        self.assertEqual(after.context["evidence_scope"]["scope"], "referenced_sources")
        self.assertNotIn("counts", after.context["evidence_scope"])
        prompt = build_atomic_prompt(after, verification_epoch="fixed")
        self.assertIn("catalog_scope=referenced_sources", prompt)
        self.assertIn("若命題實際依賴完整目錄、來源不存在或未提供", prompt)

    def test_referenced_source_content_changes_invalidate_receipts(self):
        run = make_run([_claim(scope="referenced_sources", refs=["fact.dram_supply.2029"])])
        run.records = [_source("fixture", "used-v1", {"origin": "used"})]
        original = digest(_task(run, "claim").to_dict())

        run.fact_store.get("fact.dram_supply.2029").value = 2
        changed_fact = digest(_task(run, "claim").to_dict())
        self.assertNotEqual(changed_fact, original)

        run.records[0].source_hash = "used-v2"
        changed_source = digest(_task(run, "claim").to_dict())
        self.assertNotEqual(changed_source, changed_fact)

    def test_referenced_dependent_invalidates_when_ancestor_claim_changes(self):
        parent = _claim("parent", scope="referenced_sources", refs=["fact.dram_supply.2029"])
        child = _claim("child", scope="referenced_sources", dependencies=["parent"])
        run = make_run([parent, child])
        before = digest(_task(run, "child").to_dict())

        parent["text"] = "上游主張已修訂"

        self.assertNotEqual(digest(_task(run, "child").to_dict()), before)

    def test_full_and_legacy_scopes_remain_catalog_sensitive(self):
        for scope in ("full_catalog", _OMIT):
            with self.subTest(scope="legacy omitted" if scope is _OMIT else scope):
                run = make_run([_claim(scope=scope)])
                before = digest(_task(run, "claim").to_dict())

                run.fact_store.add(Fact(fact_id="new.catalog.fact", value=7, unit="USD",
                                        period="FY2030", concept="NewMetric", source_id="new.source"))
                run.records = [_source("new.source", "new-source-v1")]

                self.assertNotEqual(digest(_task(run, "claim").to_dict()), before)

    def test_only_explicit_full_scope_hashes_unreferenced_catalog_content(self):
        run = make_run([_claim("explicit", scope="full_catalog"), _claim("legacy")])
        explicit_before = digest(_task(run, "explicit").to_dict())
        legacy_before = digest(_task(run, "legacy").to_dict())

        run.fact_store.get("fact.dram_supply.2029").value = 900001

        self.assertNotEqual(digest(_task(run, "explicit").to_dict()), explicit_before)
        self.assertEqual(digest(_task(run, "legacy").to_dict()), legacy_before)

    def test_explicit_full_ancestor_content_change_invalidates_referenced_descendant(self):
        parent = _claim("parent", scope="full_catalog", refs=["fact.dram_supply.2029"])
        child = _claim("child", scope="referenced_sources", dependencies=["parent"])
        run = make_run([parent, child])
        before = digest(_task(run, "child").to_dict())

        run.fact_store.get("fact.dram_supply.2029").value = 900001

        self.assertNotEqual(digest(_task(run, "child").to_dict()), before)

    def test_referenced_scope_inherits_full_catalog_sensitivity_from_legacy_ancestor(self):
        parent = _claim("parent", refs=["fact.dram_supply.2029"])
        child = _claim("child", scope="referenced_sources", dependencies=["parent"])
        run = make_run([parent, child])
        before = _task(run, "child")
        before_hash = digest(before.to_dict())
        prompt = build_atomic_prompt(before, verification_epoch="fixed")
        self.assertIn("繼承 full_catalog 敏感性", prompt)

        run.fact_store.add(Fact(fact_id="new.catalog.fact", value=7, unit="USD",
                                period="FY2030", concept="NewMetric", source_id="new.source"))

        self.assertNotEqual(digest(_task(run, "child").to_dict()), before_hash)

    def test_unrelated_claim_scope_declaration_does_not_change_scoped_receipt(self):
        scoped = _claim("scoped", scope="referenced_sources", refs=["fact.dram_supply.2029"])
        other = _claim("other")
        run = make_run([scoped, other])
        before = digest(_task(run, "scoped").to_dict())

        other["assertion"]["catalog_scope"] = "full_catalog"

        self.assertEqual(digest(_task(run, "scoped").to_dict()), before)

    def test_malformed_catalog_scope_is_rejected_before_task_creation(self):
        for value in ("all_sources", None, True):
            with self.subTest(value=value):
                run = make_run([_claim(scope=value)])
                with self.assertRaisesRegex(ValueError, "catalog_scope"):
                    build_atomic_tasks(run)


if __name__ == "__main__":
    unittest.main()
