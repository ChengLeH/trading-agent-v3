"""Durable evidence-work records remain operational metadata, not resolution proof."""
import json
import unittest
from copy import deepcopy

from .evidence_requests import (
    ensure_evidence_request,
    is_request_stale,
    record_search_result,
    record_submitted_source,
    validate_work_log,
)
from .schema import digest


class EvidenceRequestContracts(unittest.TestCase):
    def finding(self, finding_id='global.one', claim_ids=None):
        return dict(finding_id=finding_id, claim_ids=claim_ids or ['claim.a'],
            issue='Check whether the disclosed amount covers the full period.',
            evidence_request=dict(proposition='The amount covers the full period.',
                refs=['fact.revenue'], entity='Example Inc.', metric='Revenue', period='FY2025',
                unit='USD', basis='gaap', scope='consolidated',
                known_data_range=dict(start='FY2024', end='FY2025')))

    def claims(self):
        return [dict(claim_id='claim.a', text='Revenue was 10.', refs=['fact.revenue']),
                dict(claim_id='claim.b', text='Revenue grew.', refs=['fact.growth'])]

    def test_same_occurrence_reuses_request_and_distinct_findings_do_not_merge(self):
        claims = self.claims()
        log, first = ensure_evidence_request([], self.finding(), claims)
        repeated_log, repeated = ensure_evidence_request(log, self.finding(), claims)
        separate_log, separate = ensure_evidence_request(repeated_log,
            self.finding('global.two'), claims)

        self.assertEqual(repeated, first)
        self.assertEqual(repeated_log, log)
        self.assertNotEqual(separate['request_id'], first['request_id'])
        self.assertEqual({row['finding_id'] for row in separate_log}, {'global.one', 'global.two'})
        self.assertEqual(first['claim_ids'], ['claim.a'])
        self.assertEqual(first['claim_hashes'], {'claim.a': digest(claims[0])})
        self.assertEqual(first['evidence_request']['refs'], ['fact.revenue'])

    def test_search_and_submission_are_durable_but_never_resolve_the_finding(self):
        claims = self.claims()
        log, request = ensure_evidence_request([], self.finding(), claims)
        log = record_search_result(log, request['request_id'], 'not_found', query='annual report')
        log = record_search_result(log, request['request_id'], 'unavailable', details='provider timeout')
        log = record_submitted_source(log, request['request_id'], 'source.filing')

        restored = validate_work_log(json.loads(json.dumps(log)))
        row = restored[0]
        self.assertEqual(row['search_results'], [
            dict(outcome='not_found', query='annual report', details=''),
            dict(outcome='unavailable', query='', details='provider timeout'),
        ])
        self.assertEqual(row['submitted_source_ids'], ['source.filing'])
        self.assertEqual(row['resolution_status'], 'unresolved')
        self.assertEqual(row, log[0])

    def test_claim_changes_make_the_saved_request_stale_without_rewriting_it(self):
        claims = self.claims()
        log, request = ensure_evidence_request([], self.finding(), claims)
        saved = deepcopy(request)
        changed = deepcopy(claims)
        changed[0]['text'] = 'Revenue was 12.'
        removed = changed[1:]

        self.assertFalse(is_request_stale(request, claims))
        self.assertTrue(is_request_stale(request, changed))
        self.assertTrue(is_request_stale(request, removed))
        next_log, repeated = ensure_evidence_request(log, self.finding(), changed)
        self.assertEqual(repeated, saved)
        self.assertEqual(next_log, log)
        self.assertTrue(is_request_stale(repeated, changed))

    def test_legacy_finding_keeps_readable_issue_without_inventing_dimensions(self):
        finding = dict(finding_id='deep.receipt.4.0', claim_ids=['claim.a'], issue='Recheck the period.')
        log, request = ensure_evidence_request([], finding, self.claims())

        self.assertEqual(request['issue'], finding['issue'])
        self.assertNotIn('evidence_request', request)
        self.assertEqual(request['claim_ids'], ['claim.a'])
        self.assertEqual(validate_work_log(log), log)

    def test_unknown_dimensions_stay_blank_and_validation_rejects_resolution(self):
        finding = dict(finding_id='deep.receipt.5.0', claim_ids=['claim.a'], issue='Check coverage.',
            evidence_request=dict(proposition='The report covers the requested period.'))
        log, request = ensure_evidence_request([], finding, self.claims())

        self.assertEqual(request['evidence_request'], dict(proposition='The report covers the requested period.',
            refs=[], entity='', metric='', period='', unit='', basis='', scope='',
            known_data_range=dict(start='', end='')))
        resolved = deepcopy(log)
        resolved[0]['resolution_status'] = 'resolved'
        with self.assertRaisesRegex(ValueError, 'cannot resolve'):
            validate_work_log(resolved)

    def test_work_log_updates_do_not_mutate_claims_or_their_hashes(self):
        claims = self.claims()
        before = deepcopy(claims)
        before_hash = digest(claims)
        log, request = ensure_evidence_request([], self.finding(), claims)
        log = record_search_result(log, request['request_id'], 'found', details='candidate exists')
        log = record_submitted_source(log, request['request_id'], 'source.new')

        self.assertEqual(claims, before)
        self.assertEqual(digest(claims), before_hash)
        self.assertEqual(validate_work_log(log), log)
