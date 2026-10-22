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

    def test_newer_untyped_news_warns_without_inventing_quarter_or_blocking(self):
        run = run_with(facts=[report('FY2030', 'annual', '2031-02-01')],
                       evidence=[dict(published_or_filing_time='2031-08-09')])
        d = self.diagnostic(run)
        self.assertEqual([x['category'] for x in d['latest']], ['annual'])
        row = next(x for x in d['findings']
                   if x['check_id'] == 'financial_coverage_untyped_newer_evidence')
        self.assertEqual(row['status'], 'WARN')
        self.assertEqual(row['evidence_refs'], ['ev.0'])
        self.assertFalse(d['requirements'])
        self.assertFalse(any(x['status'] == 'FAIL' for x in d['findings']))
        # A typed interim inventory removes this specific uncertainty warning.
        known = run_with(facts=[report('FY2030', 'annual', '2031-02-01'),
                                report('FY2031-Q2', 'single_period', '2031-08-05')],
                         evidence=[dict(published_or_filing_time='2031-08-09')])
        self.assertFalse(any(x['check_id'] == 'financial_coverage_untyped_newer_evidence'
                             for x in self.diagnostic(known)['findings']))

    def test_multi_entity_coverage_does_not_suppress_company_warning(self):
        a = report('FY2030', 'annual', '2031-02-01')
        b_quarter = report('FY2031-Q2', 'single_period', '2031-08-05')
        b_quarter['entity'] = 'SECOND'
        b_annual = report('FY2031', 'annual', '2031-08-10')
        b_annual['entity'] = 'SECOND'
        news = dict(entity='ACME', published_or_filing_time='2031-08-09')
        for other in (b_quarter, b_annual):
            with self.subTest(other=other['period']):
                d = self.diagnostic(run_with(facts=[a, other], evidence=[news]))
                warnings = [f for f in d['findings']
                    if f['check_id'] == 'financial_coverage_untyped_newer_evidence']
                self.assertEqual(len(warnings), 1)
                self.assertEqual(warnings[0]['entity'], 'ACME')
                self.assertEqual(warnings[0]['evidence_refs'], ['ev.0'])
                self.assertFalse(any(f['status'] == 'FAIL' for f in d['findings']))
        # The other company may have news, but its typed quarter must not
        # be turned into a false missing-quarter warning.
        d = self.diagnostic(run_with(facts=[a, b_quarter],
            evidence=[dict(entity='SECOND', published_or_filing_time='2031-08-09')]))
        self.assertFalse(any(f['check_id'] == 'financial_coverage_untyped_newer_evidence'
                             for f in d['findings']))

    def test_undetermined_news_identity_stays_unattributed(self):
        a = report('FY2030', 'annual', '2031-02-01')
        b = report('FY2031-Q2', 'single_period', '2031-08-05')
        b['entity'] = 'SECOND'
        d = self.diagnostic(run_with(facts=[a, b],
            evidence=[dict(published_or_filing_time='2031-08-09')]))
        rows = [f for f in d['findings']
                if f['check_id'] == 'financial_coverage_untyped_newer_evidence']
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['entity'])
        self.assertEqual(rows[0]['evidence_refs'], ['ev.0'])
        self.assertFalse(any(f['status'] == 'FAIL' for f in d['findings']))

    def test_stale_quarter_never_suppresses_recent_annual_warning(self):
        annual = report('FY2030', 'annual', '2031-02-01')
        news = dict(entity='ACME', published_or_filing_time='2031-08-09')
        cases = [
            ('FY2028-Q3', '2028-11-01', True),
            ('FY2028-Q3', '2031-08-08', True),  # late restatement of old period
            ('FY2030-Q3', '2030-11-01', True),
            ('FY2030-Q4', '2031-01-25', True),
            ('FY2031-Q1', '2031-05-01', False),
            ('FY2031-Q2', '2031-08-05', False),
        ]
        for fiscal, publication, should_warn in cases:
            with self.subTest(fiscal=fiscal, publication=publication):
                d = self.diagnostic(run_with(facts=[annual,
                    report(fiscal, 'single_period', publication)], evidence=[news]))
                rows = [f for f in d['findings']
                    if f['check_id'] == 'financial_coverage_untyped_newer_evidence']
                self.assertEqual(bool(rows), should_warn)
                if rows:
                    self.assertEqual(rows[0]['entity'], 'ACME')
                    self.assertEqual(rows[0]['evidence_refs'], ['ev.0'])

    def test_annual_date_label_cannot_make_an_old_quarter_look_newer(self):
        for label in ('FY2030', '2030-12-31'):
            for quarter in ('FY2030-Q3', 'FY2030-Q4'):
                with self.subTest(label=label, quarter=quarter):
                    d = self.diagnostic(run_with(facts=[
                        report(label, 'annual', '2031-02-01'),
                        report(quarter, 'single_period', '2031-01-25')],
                        evidence=[dict(entity='ACME', published_or_filing_time='2031-08-09')]))
                    self.assertTrue(any(f['check_id'] == 'financial_coverage_untyped_newer_evidence'
                                        for f in d['findings']))

    def test_intervals_without_quarter_end_cannot_suppress_same_year_warning(self):
        for coverage in ('year_to_date', 'rolling'):
            for label in ('FY2030-H1', 'FY2030-H2', 'FY2030-YTD', 'FY2030-TTM'):
                with self.subTest(coverage=coverage, label=label):
                    d = self.diagnostic(run_with(facts=[
                        report('FY2030', 'annual', '2031-02-01'),
                        report(label, coverage, '2031-07-01')],
                        evidence=[dict(entity='ACME', published_or_filing_time='2031-08-09')]))
                    self.assertTrue(any(f['check_id'] == 'financial_coverage_untyped_newer_evidence'
                                        for f in d['findings']))

    def test_combined_unknown_dimensions_stay_unverified_not_missing(self):
        req = report('FY2030', 'annual', '2031-02-01', source_refs=['statements'], require_structured=True)
        for entity in ('', 'ACME'):
            for publication in ('', '2031-02-01T09:30:00+00:00',
                                '2031-02-01T23:30:00-05:00',
                                '2031-02-02T00:30:00+05:00'):
                with self.subTest(entity=entity, publication=publication):
                    f = report('FY2030', 'annual', publication); f['entity'] = entity
                    row = self.diagnostic(run_with(facts=[f], requirements=[req]))['requirements'][0]
                    self.assertEqual(row['status'], 'WARN')
                    self.assertFalse(row['structured'])
                    self.assertEqual(row['unverified_fact_refs'], ['fact.0'])
        # Two precise, different timestamps are different versions even on one day.
        req['published_or_filing_time'] = '2031-02-01T10:00:00+00:00'
        f = report('FY2030', 'annual', '2031-02-01T09:30:00+00:00')
        row = self.diagnostic(run_with(facts=[f], requirements=[req]))['requirements'][0]
        self.assertEqual(row['status'], 'FAIL')
        self.assertEqual(row['unverified_fact_refs'], [])

    def test_source_subject_whitespace_and_provenance_survive_verdict_views(self):
        req = report('FY2030', 'annual', '2031-02-01', source_refs=['statements'], require_structured=True)
        f = report('FY2030', 'annual', '2031-02-01'); f['entity'] = ''
        for subject in ('ACME', ' ACME '):
            with self.subTest(subject=subject):
                run = run_with(facts=[f], requirements=[req])
                run.records.append(SimpleNamespace(id='statements', subject_ticker=subject, meta={}))
                d = self.diagnostic(run)
                self.assertEqual(d['latest'][0]['entity'], 'ACME')
                self.assertEqual(d['requirements'][0]['status'], 'PASS')
                for row in (d['latest'][0], d['requirements'][0]):
                    self.assertEqual(row['entity_provenance'], [dict(ref_id='fact.0',
                        source_id='statements', entity='ACME', entity_origin='source_record.subject_ticker')])
                self.assertFalse(any(x['check_id'] == 'financial_coverage_entity_unknown' for x in d['findings']))
                self.assertEqual(run.fact_store.get('fact.0').entity, '')

    def test_typed_fact_entity_can_only_come_from_unique_source_record(self):
        annual = report('FY2030', 'annual', '2031-02-01')
        annual['entity'] = ''
        req = dict(report('FY2030', 'annual', '2031-02-01'),
                   source_refs=['statements'], require_structured=True)
        run = run_with(facts=[annual], requirements=[req])
        no_source = self.diagnostic(run)
        self.assertFalse(no_source['latest'])
        self.assertTrue(any(f['check_id'] == 'financial_coverage_entity_unknown'
                            and f['ref_id'] == 'fact.0' for f in no_source['findings']))
        self.assertEqual(no_source['requirements'][0]['status'], 'WARN')
        self.assertEqual(no_source['requirements'][0]['reason'], 'structured_entity_unknown')
        self.assertEqual(no_source['requirements'][0]['unknown_entity_fact_refs'], ['fact.0'])

        run.records.append(SimpleNamespace(id='statements', subject_ticker='ACME', meta={}))
        resolved = self.diagnostic(run)
        self.assertEqual(resolved['latest'][0]['entity'], 'ACME')
        self.assertEqual(resolved['requirements'][0]['status'], 'PASS')
        self.assertEqual(resolved['requirements'][0]['fact_refs'], ['fact.0'])
        self.assertEqual(resolved['coverage'][0]['entity_origin'], 'source_record.subject_ticker')
        self.assertEqual(run.fact_store.get('fact.0').entity, '')  # view-only fallback

        run.records.append(SimpleNamespace(id='statements', subject_ticker='SECOND', meta={}))
        ambiguous = self.diagnostic(run)
        self.assertFalse(ambiguous['latest'])
        self.assertEqual(ambiguous['requirements'][0]['reason'], 'structured_entity_unknown')
        # A different, unique source identity may not satisfy ACME's requirement.
        run.records.pop()
        run.records[-1].subject_ticker = 'SECOND'
        wrong_company = self.diagnostic(run)
        self.assertEqual(wrong_company['latest'][0]['entity'], 'SECOND')
        self.assertNotEqual(wrong_company['requirements'][0]['status'], 'PASS')

    def test_unknown_entity_typed_fact_not_attributed_to_primary_run_ticker(self):
        annual = report('FY2030', 'annual', '2031-02-01')
        annual.pop('entity')
        run = run_with(facts=[annual])
        run.manifest['subject'] = dict(ticker='ACME')
        d = self.diagnostic(run)
        self.assertFalse(d['latest'])
        self.assertEqual([f['check_id'] for f in d['findings']].count(
            'financial_coverage_entity_unknown'), 1)

    def test_interim_unknown_not_misrepresented_as_absent(self):
        d = self.diagnostic(run_with(
            facts=[report('FY2030', 'annual', '2031-02-01')],
            declarations=[report('FY2031-Q2', 'unknown', '2031-08-05')],
            evidence=[dict(entity='ACME', published_or_filing_time='2031-08-09')]))
        self.assertIn('interim_unknown', {r['category'] for r in d['latest']})
        warnings = [f for f in d['findings']
            if f['check_id'] == 'financial_coverage_untyped_newer_evidence']
        self.assertEqual(len(warnings), 1)
        self.assertIn('Unclassified interim rows may exist', warnings[0]['reason'])

    def test_warn_timestamp_boundaries_for_company_and_unattributed_news(self):
        annual = report('FY2030', 'annual', '2031-02-01')
        for entity in ('ACME', None):
            for published, expected in [
                ('2031-02-01', False),
                ('2031-01-31', False),
                ('2031-08-09', True),
                ('2031-08-10', True),
                ('2031-08-11', False),
                ('', False)]:
                with self.subTest(entity=entity, published=published):
                    evidence = dict(published_or_filing_time=published)
                    if entity is not None:
                        evidence['entity'] = entity
                    d = self.diagnostic(run_with(facts=[annual], evidence=[evidence]))
                    rows = [f for f in d['findings']
                        if f['check_id'] == 'financial_coverage_untyped_newer_evidence']
                    self.assertEqual(bool(rows), expected)
                    if rows:
                        self.assertEqual(rows[0]['entity'], entity)
                        self.assertEqual(rows[0]['evidence_refs'], ['ev.0'])

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
