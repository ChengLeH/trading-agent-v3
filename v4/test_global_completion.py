"""Incomplete global dependency coverage is completed, never assumed clean."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from .runner import PipelineRunnerV4
from .test_integration import seeded, review_all, CloudDouble, global_verdict
from .journal import RunJournal
from .checkpoint import load_checkpoint


class GlobalCompletion(unittest.TestCase):
    def partial(self, runner, ids, findings=None):
        response = global_verdict(runner, findings)
        response['dependency_reviews'] = [r for r in response['dependency_reviews'] if r['claim_id'] in ids]
        return response

    def test_partial_coverage_keeps_findings_and_only_requests_missing_claims(self):
        r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
        finding = dict(claim_ids=['b'], issue='Cross-section contradiction', evidence='Original global evidence')
        first = self.partial(r, ['a'], [finding])
        result = r.collect_phase6_global_sweep(first, model_identity='test/global')
        self.assertTrue(result['needs_dependency_completion'])
        missing = set(result['missing_dependency_claim_ids'])
        self.assertEqual(missing, {c['claim_id'] for c in r.run.claims} - {'a'})
        self.assertEqual(result['reviewed_dependency_claim_ids'], ['a'])
        self.assertIsNone(r.global_receipt)
        with self.assertRaises(ValueError): r.finalize()
        role, prompt = r.prepare_phase6_global_sweep()
        payload = json.loads(prompt.split('<context-data>\n')[1].split('\n</context-data>')[0])
        self.assertEqual(payload['review_scope'], 'dependency_completion')
        self.assertEqual(set(payload['target_claim_ids']), missing)
        result = r.collect_phase6_global_sweep(self.partial(r, missing), model_identity='test/global')
        self.assertFalse(result.get('needs_dependency_completion', False))
        self.assertEqual(result['findings'][0]['issue'], finding['issue'])
        self.assertEqual(len(r.global_receipt['dependency_reviews']), len(r.run.claims))
        self.assertTrue(result['needs_revision'])

    def test_native_execution_completes_coverage_without_repeating_global_sweep(self):
        r = seeded(); scopes = []
        def reviewer(prompt):
            payload = json.loads(prompt.split('<context-data>\n')[1].split('\n</context-data>')[0])
            scopes.append(payload['review_scope'])
            return self.partial(r, ['a'] if payload['review_scope'] == 'global_consistency' else payload['target_claim_ids'])
        result = r.execute_phase6(CloudDouble(), CloudDouble('test/deep'), CloudDouble('test/patch'),
                                 CloudDouble('test/global', reviewer))
        self.assertFalse(result['blocked'], result)
        self.assertEqual(scopes, ['global_consistency', 'dependency_completion'])

    def test_exhaustion_is_explicit_and_raising_ceiling_continues_remaining_work(self):
        r = seeded(); r.max_contract_attempts = 2
        review_all(r); r.prepare_phase6_global_sweep()
        first = r.collect_phase6_global_sweep(self.partial(r, ['a']), model_identity='test/global')
        self.assertTrue(first['needs_retry'])
        r.prepare_phase6_global_sweep()
        stopped = r.collect_phase6_global_sweep(self.partial(r, []), model_identity='test/global')
        self.assertFalse(stopped['needs_retry'])
        self.assertEqual(stopped['status'], 'blocked:global_dependency_coverage')
        self.assertIsNone(r.global_receipt)
        r.max_contract_attempts = 4
        r.prepare_phase6_global_sweep()
        completed = r.collect_phase6_global_sweep(self.partial(r, stopped['missing_dependency_claim_ids']), model_identity='test/global')
        self.assertFalse(completed['blocked'])
        self.assertFalse(r.finalize()['blocked'])

    def test_restart_replays_saved_global_and_supplement_before_new_paid_work(self):
        with tempfile.TemporaryDirectory() as temp:
            r = seeded(); r.journal = RunJournal(temp); review_all(r)
            author = CloudDouble('test/global', lambda _: self.partial(r, ['a']))
            role, prompt = r.prepare_phase6_global_sweep()
            response = r._complete(author, role, prompt)
            r.collect_phase6_global_sweep(response, model_identity=author.identity)
            self.assertIn('a', json.loads((Path(temp)/'checkpoint.json').read_text())['global_review_progress']['reviewed_dependency_claim_ids'])
            resumed = load_checkpoint(Path(temp)/'checkpoint.json'); resumed.journal = RunJournal(temp)
            self.assertIsNone(resumed.global_receipt)
            self.assertIsNone(resumed.pending_global)  # saved progress is diagnostic, not an accepted receipt
            review_all(resumed)
            role, prompt = resumed.prepare_phase6_global_sweep()
            replayed = resumed._complete(author, role, prompt)
            partial = resumed.collect_phase6_global_sweep(replayed, model_identity=author.identity)
            self.assertEqual(len(author.calls), 1)
            self.assertIn('a', partial['reviewed_dependency_claim_ids'])

    def test_supplement_cannot_overwrite_prior_rows_change_model_or_use_stale_state(self):
        for mutation in ('overwrite', 'model', 'stale'):
            with self.subTest(mutation=mutation):
                r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
                r.collect_phase6_global_sweep(self.partial(r, ['a']), model_identity='test/global')
                r.prepare_phase6_global_sweep()
                response = self.partial(r, ['a'] if mutation == 'overwrite' else [])
                if mutation == 'stale': r.run.claims[0]['text'] = 'changed'
                with self.assertRaises(ValueError):
                    r.collect_phase6_global_sweep(response, model_identity='test/changed' if mutation == 'model' else 'test/global')
                self.assertIsNone(r.global_receipt)

    def test_exhausted_restart_replays_saved_supplement_without_reset_or_spending(self):
        with tempfile.TemporaryDirectory() as temp:
            r = seeded(); r.max_contract_attempts = 2; r.journal = RunJournal(temp); review_all(r)
            author = CloudDouble('test/global', lambda _: self.partial(r, ['a']))
            role, prompt = r.prepare_phase6_global_sweep()
            r.collect_phase6_global_sweep(r._complete(author, role, prompt), model_identity=author.identity)
            author.responder = lambda _: self.partial(r, [])
            role, prompt = r.prepare_phase6_global_sweep()
            stopped = r.collect_phase6_global_sweep(r._complete(author, role, prompt), model_identity=author.identity)
            self.assertFalse(stopped['needs_retry']); self.assertEqual(len(author.calls), 2)
            resumed = load_checkpoint(Path(temp)/'checkpoint.json'); resumed.journal = RunJournal(temp); review_all(resumed)
            for _ in range(2):
                role, prompt = resumed.prepare_phase6_global_sweep()
                stopped = resumed.collect_phase6_global_sweep(resumed._complete(author, role, prompt), model_identity=author.identity)
            self.assertFalse(stopped['needs_retry']); self.assertEqual(stopped['coverage_round'], 2)
            self.assertEqual(len(author.calls), 2)
            resumed.max_contract_attempts = 3
            author.responder = lambda _: self.partial(resumed, stopped['missing_dependency_claim_ids'])
            role, prompt = resumed.prepare_phase6_global_sweep()
            done = resumed.collect_phase6_global_sweep(resumed._complete(author, role, prompt), model_identity=author.identity)
            self.assertFalse(done['blocked']); self.assertEqual(len(author.calls), 3)

    def test_completion_extra_findings_are_retained(self):
        r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(self.partial(r, ['a']), model_identity='test/global')
        r.prepare_phase6_global_sweep()
        missing = [c['claim_id'] for c in r.run.claims if c['claim_id'] != 'a']
        reply = self.partial(r, missing, [dict(claim_ids=['c'], issue='new contradiction')])
        result = r.collect_phase6_global_sweep(reply, model_identity='test/global')
        self.assertEqual(result['findings'][0]['issue'], 'new contradiction')
        self.assertTrue(result['blocked'])

    def test_invalid_saved_progress_is_not_accepted_as_review_receipt(self):
        from .checkpoint import save_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
            r.collect_phase6_global_sweep(self.partial(r, ['a']), model_identity='test/global')
            path = Path(temp)/'checkpoint.json'; save_checkpoint(r, path)
            data = json.loads(path.read_text())
            data['global_review_progress']['missing_dependency_claim_ids'] = []
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'progress coverage'): load_checkpoint(path)

    def test_trade_plan_and_reference_coverage_failures_identify_targets(self):
        from .pipeline import validate_trade_claim
        from .semantic_contracts import normalize_reference_checks
        r = seeded(); plans = copy.deepcopy(r.run.claims)
        for c in plans:
            if c['author_role'] == 'trader': c['assertion'] = dict(kind='interpretation')
        with self.assertRaisesRegex(ValueError, 'trader_claim_ids=.*role.trader'):
            validate_trade_claim(plans, r.run.trade)
        with self.assertRaisesRegex(ValueError, 'missing_ref_ids=.*source.missing'):
            normalize_reference_checks(['source.missing'], dict(verdict='PASS', reference_checks=[]))

    def test_dependency_cycle_across_separate_responses_is_rejected(self):
        r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
        first = self.partial(r, ['a'])
        first['dependency_reviews'][0]['required_dependencies'] = ['independent']
        result = r.collect_phase6_global_sweep(first, model_identity='test/global')
        r.prepare_phase6_global_sweep()
        second = self.partial(r, result['missing_dependency_claim_ids'])
        next(row for row in second['dependency_reviews'] if row['claim_id'] == 'independent')['required_dependencies'] = ['b']
        with self.assertRaises(ValueError): r.collect_phase6_global_sweep(second, model_identity='test/global')
        self.assertIsNone(r.global_receipt)

    def test_prior_clean_global_receipt_cannot_bypass_a_new_partial_sweep(self):
        r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(global_verdict(r), model_identity='test/global')
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(self.partial(r, ['a']), model_identity='test/global')
        self.assertIsNone(r.global_receipt)
        with self.assertRaisesRegex(ValueError, 'global sweep still pending'): r.finalize()

    def test_model_change_blocks_before_spending_for_supplement(self):
        r = seeded(); review_all(r); r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(self.partial(r, ['a']), model_identity='test/original')
        replacement = CloudDouble('test/replacement')
        result = r.execute_phase6(CloudDouble(), CloudDouble('test/deep'), CloudDouble('test/patch'), replacement)
        self.assertTrue(result['blocked']); self.assertEqual(replacement.calls, [])
        self.assertIn('global_review_progress', result)

    def test_fifty_seven_claims_can_complete_only_the_forty_four_missing_rows(self):
        from .test_contracts import claim
        r = seeded()
        extra = [claim(f'extra.{i}') for i in range(57 - len(r.run.claims))]
        for c in extra: c['author_role'] = 'extra'
        r.orch.submit_batch(r.run_id, dict(extra=extra))
        review_all(r); r.prepare_phase6_global_sweep()
        reviewed = [c['claim_id'] for c in r.run.claims[:13]]
        result = r.collect_phase6_global_sweep(self.partial(r, reviewed), model_identity='test/global')
        self.assertEqual(len(result['reviewed_dependency_claim_ids']), 13)
        self.assertEqual(len(result['missing_dependency_claim_ids']), 44)
        r.prepare_phase6_global_sweep()
        completed = r.collect_phase6_global_sweep(self.partial(r, result['missing_dependency_claim_ids']), model_identity='test/global')
        self.assertFalse(completed['blocked'])
        self.assertEqual(len(r.global_receipt['dependency_reviews']), 57)
        self.assertFalse(r.finalize()['blocked'])
