"""Real manifest/checkpoint/gate path for explicit financial coverage requirements."""
import json
from pathlib import Path
import tempfile
import unittest

from .pipeline import OrchestratorV4
from .checkpoint import save_checkpoint, load_checkpoint
from .test_integration import seeded


class FinancialGate(unittest.TestCase):
    def requirement(self):
        return dict(entity='TEST', period='FY2031-Q2', period_coverage='single_period',
                    published_or_filing_time='2031-08-05', source_refs=['release'],
                    require_structured=True)

    def test_known_required_report_blocks_and_survives_checkpoint(self):
        runner = seeded()
        runner.run.manifest.update(as_of='2031-08-10', financial_coverage_requirements=[self.requirement()])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
            findings, blocked = restored.orch.validate_stage(restored.run_id, 'final')
            self.assertTrue(blocked)
            row = next(f for f in findings if f['check_id'] == 'financial_coverage_requirement')
            self.assertEqual(row['reason'], 'required_report_not_received')
            self.assertEqual(row['coverage']['requirement'], self.requirement())

    def test_unknown_requirement_warns_without_blocking(self):
        runner = seeded()
        req = self.requirement()
        req['published_or_filing_time'] = ''
        runner.run.manifest['financial_coverage_requirements'] = [req]
        findings, blocked = runner.orch.validate_stage(runner.run_id, 'final')
        row = next(f for f in findings if f['check_id'] == 'financial_coverage_requirement')
        self.assertEqual(row['status'], 'WARN')
        self.assertFalse(blocked)

    def test_coverage_warnings_reach_stage_findings_without_blocking(self):
        from v3.facts import Fact
        from v3.evidence import EvidencePacket
        runner = seeded()
        runner.run.manifest['as_of'] = '2031-08-10'
        runner.run.fact_store.add(Fact('fact.annual', 100, 'USD', 'FY2030',
            concept='Revenue', source_id='statements', entity='ACME',
            period_coverage='annual', published_or_filing_time='2031-02-01'))
        runner.run.evidence_store.add(EvidencePacket('ev.new', 'unspecified news',
            'news', 'news_json', 'row.1', entity='ACME',
            published_or_filing_time='2031-08-09'))
        rows, blocked = runner.orch.validate_stage(runner.run_id, 'final')
        warn = next(row for row in rows
            if row['check_id'] == 'financial_coverage_untyped_newer_evidence')
        self.assertEqual(warn['status'], 'WARN')
        self.assertEqual(warn['entity'], 'ACME')
        self.assertEqual(warn['evidence_refs'], ['ev.new'])
        self.assertFalse(blocked)

    def test_unknown_fact_entity_warning_reaches_stage(self):
        from v3.facts import Fact
        runner = seeded()
        runner.run.manifest['as_of'] = '2031-08-10'
        runner.run.fact_store.add(Fact('fact.no-issuer', 100, 'USD', 'FY2030',
            concept='Revenue', source_id='statements', entity='',
            period_coverage='annual', published_or_filing_time='2031-02-01'))
        rows, blocked = runner.orch.validate_stage(runner.run_id, 'final')
        warn = next(row for row in rows if row['check_id'] == 'financial_coverage_entity_unknown')
        self.assertEqual(warn['status'], 'WARN')
        self.assertEqual(warn['ref_id'], 'fact.no-issuer')
        self.assertFalse(blocked)

    def test_fresh_manifest_preserves_evidence_only_report_declaration(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'release.txt'
            source.write_text('Source text is not parsed to invent statement numbers.')
            requirement = self.requirement()
            manifest = dict(subject=dict(ticker='TEST'), as_of='2031-08-10',
                financial_coverage_requirements=[requirement], sources=[dict(id='release',
                type='generic_text', path=str(source), financial_report_coverage=[requirement])])
            orch = OrchestratorV4(); rid = orch.start_run(dict(ticker='TEST'), manifest)
            orch.ingest(rid)
            self.assertEqual(orch.runs[rid].manifest['financial_coverage_requirements'], [requirement])
            self.assertEqual(orch.runs[rid].records[0].meta['financial_report_coverage'], [requirement])
            rows, blocked = orch.validate_stage(rid, 'ingest')
            self.assertTrue(blocked)
            row = next(f for f in rows if f['check_id'] == 'financial_coverage_requirement')
            self.assertTrue(row['coverage']['received'])
            self.assertFalse(row['coverage']['structured'])
            self.assertEqual(row['reason'], 'required_report_not_structured')


if __name__ == '__main__':
    unittest.main()
