"""V4 source lineage is declared provenance, never an independence verdict."""
import json
from pathlib import Path
import tempfile
import unittest

from v3.sources import AdapterRegistry, SourceRecord
from v3.evidence import EvidencePacket
from v3.facts import Fact
from .atomic_phase6 import build_atomic_tasks
from .checkpoint import load_checkpoint, save_checkpoint
from .pipeline import OrchestratorV4
from .reporting import render_report
from .test_integration import seeded


class SourceLineage(unittest.TestCase):
    def test_atomic_context_includes_only_lineage_reachable_from_claim_refs(self):
        runner = seeded()
        runner.run.fact_store.add(Fact(fact_id="fact.revenue", value=12, unit="USD",
            period="FY2025", concept="Revenue", source_id="source.finance", source_pointer="income.revenue"))
        claim = next(row for row in runner.run.claims if row["claim_id"] == "a")
        claim["refs"] = ["fact.revenue"]
        runner.run.source_lineage = {
            "source.finance": {"origin_id": "filing-v1", "evidence_refs": ["evidence.origin"]},
            "evidence.origin": {"origin_id": "original-release-v1"},
            "source.unrelated": {"origin_id": "unrelated-v1"},
        }

        task = next(row for row in build_atomic_tasks(runner.run) if row.claim_id == "a")

        self.assertEqual(task.context["source_lineage"], {
            "source.finance": {"origin_id": "filing-v1", "evidence_refs": ["evidence.origin"]},
            "evidence.origin": {"origin_id": "original-release-v1"},
        })
        self.assertNotIn("source.unrelated", task.review_dict()["context"]["source_lineage"])

    def test_global_review_gets_full_lineage_and_lineage_never_certifies_independence(self):
        runner = seeded()
        runner.run.source_lineage = {
            "source.one": {"origin_id": "same-document"},
            "source.two": {"origin_id": "same-document"},
        }
        global_state = runner._global_review_state()

        self.assertEqual(global_state["source_lineage"], runner.run.source_lineage)
        self.assertNotIn("independent", json.dumps(global_state["source_lineage"]).lower())

    def test_report_discloses_declared_and_unknown_lineage_safely(self):
        runner = seeded()
        runner.run.evidence_store.add(EvidencePacket(evidence_id="evidence.news",
            claim_candidate="A reported fact.", source_id="source.news", source_type="news_json",
            source_span="https://news.test/story"))
        next(row for row in runner.run.claims if row["claim_id"] == "a")["refs"] = ["evidence.news"]
        runner.run.source_lineage = {
            "source.news": {"origin_id": "origin-v1", "reason": "Declared <script> [text]."},
        }

        rendered = render_report(runner.orch.snapshot(runner.run_id))

        self.assertEqual(rendered["sidecar"]["source_lineage"], runner.run.source_lineage)
        citation = rendered["sidecar"]["citations"][0]
        statuses = {row["ref_id"]: row["status"] for row in citation["source_lineage"]}
        self.assertEqual(statuses, {"source.news": "declared", "evidence.news": "unknown"})
        self.assertIn("來源沿革為資料註記、未獨立驗證", rendered["report_markdown"])
        self.assertIn("來源沿革未知", rendered["report_markdown"])
        self.assertNotIn("<script>", rendered["report_markdown"])

    def test_reposts_share_declared_origin_and_bundle_evidence_can_differ(self):
        from .source_lineage import normalize_source_lineage, validate_source_lineage

        lineage = normalize_source_lineage({
            "repost.one": {"origin_id": "statement-2025-v1", "publisher": "Outlet A",
                           "evidence_refs": ["bundle.news0"]},
            "repost.two": {"origin_id": "statement-2025-v1", "publisher": "Outlet B"},
            "repost.three": {"origin_id": "statement-2025-v1", "publisher": "Outlet C"},
            "bundle.news0": {"origin_id": "article-alpha-v1"},
            "bundle.news1": {"origin_id": "article-beta-v2"},
        })
        owners = {"bundle.news0": "bundle", "bundle.news1": "bundle"}
        validated = validate_source_lineage(
            lineage,
            source_ids={"repost.one", "repost.two", "repost.three", "bundle"},
            evidence_owners=owners,
        )

        self.assertEqual({validated[key]["origin_id"] for key in
                          ("repost.one", "repost.two", "repost.three")},
                         {"statement-2025-v1"})
        self.assertEqual(validated["bundle.news0"]["origin_id"], "article-alpha-v1")
        self.assertEqual(validated["bundle.news1"]["origin_id"], "article-beta-v2")
        self.assertNotIn("independent", json.dumps(validated).lower())

    def test_same_url_versions_stay_distinct_and_unknown_values_are_not_filled(self):
        from .source_lineage import normalize_source_lineage, validate_source_lineage

        lineage = normalize_source_lineage({
            "filing.v1": {"origin_id": "filing-document-v1", "reason": "first version",
                          "content_fingerprint": "version-one-hash"},
            "filing.v2": {"origin_id": "filing-document-v2", "reason": "amended version",
                          "content_fingerprint": "version-two-hash"},
        })
        versions = validate_source_lineage(lineage, source_ids={"filing.v1", "filing.v2"},
            evidence_owners={}, fingerprints={"filing.v1": "version-one-hash",
                                               "filing.v2": "version-two-hash"})

        source_rows = {
            "filing.v1": {"url": "https://filings.test/company/report", "version": "accepted"},
            "filing.v2": {"url": "https://filings.test/company/report", "version": "amended"},
        }
        self.assertEqual(source_rows["filing.v1"]["url"], source_rows["filing.v2"]["url"])
        self.assertNotEqual(source_rows["filing.v1"]["version"], source_rows["filing.v2"]["version"])
        self.assertNotEqual(versions["filing.v1"]["origin_id"],
                            versions["filing.v2"]["origin_id"])
        self.assertNotEqual(versions["filing.v1"]["content_fingerprint"],
                            versions["filing.v2"]["content_fingerprint"])
        self.assertNotIn("publisher", versions["filing.v1"])
        self.assertNotIn("origin_id", normalize_source_lineage({
            "source.unknown": {"publisher": "Declared Publisher"}
        })["source.unknown"])

    def test_dangling_refs_and_cycles_are_rejected(self):
        from .source_lineage import normalize_source_lineage, validate_source_lineage

        dangling = normalize_source_lineage({
            "source.a": {"derived_from": ["missing.parent"]},
        })
        with self.assertRaisesRegex(ValueError, "derived_from.*missing.parent"):
            validate_source_lineage(dangling, source_ids={"source.a"}, evidence_owners={})

        with self.assertRaisesRegex(ValueError, "cycle"):
            normalize_source_lineage({
                "source.a": {"derived_from": ["source.b"]},
                "source.b": {"derived_from": ["source.a"]},
            })

        fingerprint = normalize_source_lineage({
            "source.a": {"content_fingerprint": "wrong-fingerprint"},
        })
        with self.assertRaisesRegex(ValueError, "content_fingerprint"):
            validate_source_lineage(fingerprint, source_ids={"source.a"},
                                    evidence_owners={},
                                    fingerprints={"source.a": "canonical-fingerprint"})

        with self.assertRaisesRegex(ValueError, "source owner"):
            validate_source_lineage({"bundle.news0": {"origin_id": "article-v1"}},
                                    source_ids={"source.a"},
                                    evidence_owners={"bundle.news0": "missing.source"})

    def test_manifest_lineage_is_validated_and_committed_after_ingestion(self):
        manifest = {
            "subject": {"ticker": "TEST"}, "as_of": "2026-10-21",
            "sources": [{"id": "bundle", "type": "news_json", "path": "unused"}],
            "source_lineage": {
                "bundle.news0": {"origin_id": "article-v1", "publisher": "Outlet A"},
                "bundle.news1": {"origin_id": "article-v2"},
            },
        }
        orch = OrchestratorV4()
        rid = orch.start_run({"ticker": "TEST"}, manifest)
        run = orch.runs[rid]
        self.assertEqual(run.source_lineage, {})

        registry = AdapterRegistry()
        registry.register("news_json", lambda source_id, _path, subject: SourceRecord(
            id=source_id, type="news_json", subject_ticker=subject["ticker"],
            subject_cik=None,
            payload={"items": [
                {"title": "A", "url": "https://same.test/story"},
                {"title": "B", "url": "https://same.test/story"},
            ]}, source_hash="a1b2c3d4e5f60708"))
        orch.ingest(rid, registry=registry)
        orch.build_facts(rid)
        findings, blocked = orch.validate_stage(rid, "ingest")

        self.assertFalse(blocked, findings)
        self.assertEqual(run.source_lineage["bundle.news0"]["origin_id"], "article-v1")
        self.assertEqual(run.source_lineage["bundle.news1"]["origin_id"], "article-v2")
        self.assertNotIn("publisher", run.source_lineage["bundle.news1"])

    def test_dangling_manifest_lineage_never_enters_canonical_snapshot(self):
        manifest = {
            "subject": {"ticker": "TEST"}, "as_of": "2026-10-21",
            "sources": [{"id": "source.a", "type": "generic_text", "path": "unused"}],
            "source_lineage": {"source.a": {"derived_from": ["missing.parent"]}},
        }
        orch = OrchestratorV4()
        rid = orch.start_run({"ticker": "TEST"}, manifest)
        run = orch.runs[rid]
        run.records = [SourceRecord("source.a", "generic_text", "TEST", None,
                                    "body", "0011223344556677")]
        _, blocked = orch.validate_stage(rid, "ingest")

        self.assertTrue(blocked)
        self.assertEqual(run.source_lineage, {})
        self.assertNotIn("source_lineage", orch.snapshot(rid))

    def test_checkpoint_round_trip_and_legacy_missing_field(self):
        runner = seeded()
        runner.run.records = [SourceRecord("source.a", "generic_text", "TEST", None,
                                           "body", "0011223344556677")]
        runner.run.source_lineage = {
            "source.a": {"origin_id": "document-v1", "content_fingerprint": "0011223344556677"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
            self.assertEqual(restored.run.source_lineage, runner.run.source_lineage)
            lineage_hash = restored._hash()
            restored.run.source_lineage = {}
            self.assertNotEqual(restored._hash(), lineage_hash)

            legacy = json.loads(path.read_text(encoding="utf-8"))
            legacy["state"].pop("source_lineage", None)
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded_legacy = load_checkpoint(path)
            self.assertEqual(loaded_legacy.run.source_lineage, {})


if __name__ == "__main__":
    unittest.main()
