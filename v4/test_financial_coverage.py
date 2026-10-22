"""Publication-aware report inventory, without interpreting report prose."""
import unittest
from types import SimpleNamespace
from v3.facts import Fact, FactStore
from v3.evidence import EvidencePacket, EvidenceStore
from .financial_review import ingestion_diagnostics


def report(period='FY2031-Q2', coverage='single_period', published='2031-08-05', **extra):
    return dict(entity='ACME', period=period, period_coverage=coverage,
                published_or_filing_time=published, **extra)


def run_with(facts=(), evidence=(), declarations=(), requirements=None, as_of='2031-08-10'):
    fs, es = FactStore(), EvidenceStore()
    for i, row in enumerate(facts):
        fs.add(Fact(f'fact.{i}', 10, 'USD', concept='Revenue', source_id='statements', **row))
    for i, row in enumerate(evidence):
        es.add(EvidencePacket(f'ev.{i}', 'arbitrary prose', 'release', 'news_json', 'row.0', **row))
    manifest = dict(as_of=as_of)
    if requirements is not None:
        manifest['financial_coverage_requirements'] = requirements
    return SimpleNamespace(manifest=manifest, fact_store=fs, evidence_store=es,
        records=[SimpleNamespace(id='release', meta={'financial_report_coverage': list(declarations)})])


class FinancialCoverage(unittest.TestCase):
    def test_late_restatement_does_not_hide_a_newer_financial_period(self):
        d = self.diagnostic(run_with(declarations=[
            report('FY2030', 'annual', '2031-08-09'),
            report('FY2031', 'annual', '2031-08-01')]))
        self.assertEqual(d['latest'][0]['period'], 'FY2031')

    def test_report_inventory_does_not_repeat_a_row_for_every_metric(self):
        run = run_with(facts=[report() for _ in range(200)])
        diagnostic = self.diagnostic(run)
        self.assertEqual(len(diagnostic['coverage']), 1)
        self.assertEqual(len(diagnostic['coverage'][0]['ref_ids']), 200)

    def diagnostic(self, run):
        from .financial_coverage import financial_coverage_diagnostics
        return financial_coverage_diagnostics(run)[0]

    def test_evidence_only_interim_is_visible_despite_annual_facts(self):
        run = run_with(facts=[report('FY2030', 'annual', '2031-02-01')],
                       declarations=[report()])
        d = self.diagnostic(run)
        rows = {r['category']: r for r in d['latest']}
        self.assertTrue(rows['annual']['structured'])
        self.assertFalse(rows['quarter']['structured'])
        self.assertTrue(rows['quarter']['received'])
        self.assertEqual(rows['quarter']['source_refs'], ['release'])
        self.assertTrue(any(f['check_id'] == 'latest_report_not_structured' for f in d['findings']))
        self.assertTrue(all(f['status'] == 'WARN' for f in d['findings']))
        self.assertTrue(any(r['section'] == 'financial_coverage' for r in ingestion_diagnostics(run)))

    def test_publication_asof_not_period_label_controls_eligibility(self):
        d = self.diagnostic(run_with(declarations=[report(published='2031-08-11'),
            report('FY2031-Q1', published='2031-05-06')]))
        self.assertEqual(d['latest'][0]['period'], '2031-Q1')
        self.assertEqual(d['coverage'][0]['availability'], 'after_as_of')

    def test_quarter_ytd_ttm_and_annual_are_not_substitutes(self):
        facts = [report('FY2030', 'annual', '2031-02-01'), report(),
                 report('FY2031-Q2', 'year_to_date'), report('FY2031-TTM', 'rolling')]
        d = self.diagnostic(run_with(facts=facts))
        self.assertEqual({x['category'] for x in d['latest']},
                         {'annual', 'quarter', 'year_to_date', 'ttm'})
        self.assertTrue(all(x['structured'] for x in d['latest']))

    def test_unknown_dates_and_ambiguous_interim_remain_unknown(self):
        d = self.diagnostic(run_with(evidence=[dict(entity='ACME', period='FY2031-Q2')],
            declarations=[report(coverage='unknown', published='')]))
        self.assertFalse(d['latest'])
        self.assertTrue(all(x['availability'] == 'unknown' for x in d['coverage']))
        self.assertTrue(all(x['status'] == 'WARN' for x in d['findings']))

    def test_prose_is_never_classified_as_financial_report(self):
        run = run_with()
        # The test helper supplies prose itself; create this untyped packet directly.
        run.evidence_store = EvidenceStore()
        run.evidence_store.add(EvidencePacket('ev.prose', 'FY2031 Q2 10-Q published',
            'news', 'news_json', 'line.1'))
        d = self.diagnostic(run)
        self.assertFalse(d['latest'])
        self.assertFalse(d['coverage'])

    def test_explicit_required_report_missing_structured_facts_fails_only_when_known(self):
        req = report(report_type='interim', source_refs=['release'], require_structured=True,
                     purpose='current_analysis', form='10-Q')
        d = self.diagnostic(run_with(declarations=[report()], requirements=[req]))
        row = d['requirements'][0]
        self.assertEqual(row['status'], 'FAIL')
        self.assertTrue(row['received'])
        self.assertFalse(row['structured'])
        self.assertEqual(row['reason'], 'required_report_not_structured')
        req['published_or_filing_time'] = ''
        d = self.diagnostic(run_with(declarations=[report()], requirements=[req]))
        self.assertEqual(d['requirements'][0]['status'], 'WARN')

    def test_required_source_missing_and_success_and_future_report(self):
        req = report(report_type='interim', source_refs=['missing'], require_structured=True)
        d = self.diagnostic(run_with(requirements=[req]))
        self.assertFalse(d['requirements'][0]['received'])
        self.assertEqual(d['requirements'][0]['reason'], 'required_report_not_received')
        req['source_refs'] = ['statements']
        d = self.diagnostic(run_with(facts=[report()], requirements=[req]))
        self.assertEqual(d['requirements'][0]['status'], 'PASS')
        d = self.diagnostic(run_with(requirements=[req], as_of='2031-08-01'))
        self.assertEqual(d['requirements'][0]['status'], 'WARN')
        self.assertEqual(d['requirements'][0]['reason'], 'report_after_as_of')

    def test_explicit_requirement_can_accept_received_unstructured_report(self):
        req = report(source_refs=['release'], require_structured=False)
        result = self.diagnostic(run_with(declarations=[report()], requirements=[req]))
        row = result['requirements'][0]
        self.assertEqual(row['status'], 'PASS')
        self.assertTrue(row['received'])
        self.assertFalse(row['structured'])
        self.assertEqual(row['reason'], 'required_report_covered')

    def test_required_publication_version_cannot_use_same_period_other_version(self):
        req = report(source_refs=['release', 'statements'], require_structured=True)
        result = self.diagnostic(run_with(
            declarations=[report()],
            facts=[report(published='2031-08-06')],
            requirements=[req]))
        row = result['requirements'][0]
        self.assertEqual(row['status'], 'FAIL')
        self.assertTrue(row['received'])
        self.assertFalse(row['structured'])
        self.assertEqual(row['fact_refs'], [])
        self.assertEqual(row['reason'], 'required_report_not_structured')

    def test_requirement_received_source_is_distinct_from_structured_numbers(self):
        req = report(source_refs=['release'], require_structured=True)
        row = self.diagnostic(run_with(requirements=[req]))['requirements'][0]
        self.assertTrue(row['received'])
        self.assertFalse(row['structured'])
        self.assertEqual(row['reason'], 'required_report_not_structured')

    def test_unknown_fact_publication_does_not_become_known_structured_failure(self):
        req = report(source_refs=['statements'], require_structured=True)
        row = self.diagnostic(run_with(facts=[report(published='')],
            requirements=[req]))['requirements'][0]
        self.assertEqual(row['status'], 'WARN')
        self.assertEqual(row['reason'], 'structured_publication_unknown')

    def test_conflicting_period_metadata_is_unknown_not_an_annual_substitute(self):
        req = report(coverage='annual', source_refs=['release'], require_structured=True)
        d = self.diagnostic(run_with(declarations=[req], requirements=[req]))
        self.assertEqual(d['requirements'][0]['status'], 'WARN')
        self.assertFalse(d['latest'])

    def test_historical_analysis_does_not_invent_latest_report_requirement(self):
        d = self.diagnostic(run_with(facts=[report('FY2021', 'annual', '2022-02-01')]))
        self.assertFalse(d['requirements'])
        self.assertTrue(d['latest'][0]['structured'])
        self.assertFalse(any(f['status'] == 'FAIL' for f in d['findings']))

    def test_entities_and_same_period_versions_cannot_cover_each_other(self):
        newer = report(published='2031-08-09')
        other = report(); other['entity'] = 'OTHER'
        d = self.diagnostic(run_with(facts=[report(), other], declarations=[newer]))
        latest = next(x for x in d['latest'] if x['entity'] == 'ACME')
        self.assertFalse(latest['structured'])
        self.assertEqual(latest['published_or_filing_time'], '2031-08-09')

    def test_date_only_asof_includes_day_but_timestamp_does_not_include_later_release(self):
        row = report(published='2031-08-10T20:00:00Z')
        self.assertTrue(self.diagnostic(run_with(declarations=[row]))['latest'])
        self.assertFalse(self.diagnostic(run_with(declarations=[row],
            as_of='2031-08-10T19:00:00Z'))['latest'])


if __name__ == '__main__':
    unittest.main()
