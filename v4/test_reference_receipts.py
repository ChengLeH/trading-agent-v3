"""Completed reference supplements remain verifiable after a restart."""
import json
from pathlib import Path
import tempfile
import unittest
from .checkpoint import load_checkpoint
from .journal import RunJournal
from .test_integration import seeded, CloudDouble, verdict, global_verdict


class ReferenceReceipts(unittest.TestCase):
    def test_legacy_partial_answers_can_be_completed_with_a_new_bound_verdict(self):
        from .atomic_phase6 import build_atomic_tasks
        from .schema import digest
        from .semantic_contracts import SEMANTIC_REVIEW_CONTRACT
        runner = seeded()
        next(c for c in runner.run.claims if c['claim_id'] == 'b')['dependencies'].extend(['independent', 'role.bear'])
        with tempfile.TemporaryDirectory() as temp:
            runner.journal = RunJournal(temp)
            client = CloudDouble('test/first')
            for ref in ['a', 'independent']:
                prepared = runner.prepare_phase6_atomic()
                task = next(t for t in build_atomic_tasks(runner.run) if t.claim_id == 'b')
                client.responder = lambda _, ref=ref: verdict('b', ref_ids=[ref])
                # A pre-upgrade request has no parent-attempt metadata.
                context = dict(layer='atomic', verification_epoch=runner._verification_epoch,
                    contract=SEMANTIC_REVIEW_CONTRACT, task_hashes={'b': digest(task.to_dict())})
                raw, proof = runner._complete(client, 'b', dict(prepared['atomic_prompts'])['b'],
                    response_schema=prepared['response_schemas']['b'], review_context=context, with_provenance=True)
                runner._record_review_progress({'b': raw}, client, 'atomic',
                    {'b': {**proof, 'claim_id': 'b', 'layer': 'atomic'}})
                runner.collect_phase6_atomic({'b': raw}, model_identity=client.identity, allow_partial=True)
            def finish(prompt):
                payload = json.loads(prompt.split('```json\n')[1].split('\n```')[0])
                return verdict(payload['claim']['claim_id'], ref_ids=payload['expected_reference_ids'])
            client.responder = finish
            result = runner.execute_phase6(client, CloudDouble('test/deep'), CloudDouble('test/patch'),
                CloudDouble('test/global', lambda _: global_verdict(runner)))
            self.assertFalse(result['blocked'], result)
            restored = load_checkpoint(Path(temp) / 'checkpoint.json')
            self.assertEqual(restored.receipts, runner.receipts)

    def test_native_partial_delivery_does_not_spend_unattempted_claim_retry_budgets(self):
        runner = seeded(); runner.prepare_phase6_atomic()
        result = runner.collect_phase6_atomic({'a': verdict('a')}, model_identity='host/one', allow_partial=True)
        self.assertTrue(result['partial'])
        self.assertFalse(result['complete'])
        self.assertEqual(runner._contract_attempts, {})
        runner.collect_phase6_atomic({'independent': verdict('independent')}, model_identity='host/two', allow_partial=True)
        self.assertEqual(runner.receipts['a']['model'], 'host/one')
        self.assertEqual(runner.receipts['independent']['model'], 'host/two')
        with self.assertRaises(ValueError):
            runner.collect_phase6_atomic({'unknown': verdict('unknown')}, model_identity='host/one', allow_partial=True)
        with self.assertRaises(ValueError):
            runner.finalize()

    def test_native_deep_partial_delivery_keeps_other_nonpass_claims_pending(self):
        runner = seeded(); prepared = runner.prepare_phase6_atomic()
        claims = {c['claim_id']: c for c in runner.run.claims}
        from .test_integration import reference_ids
        runner.collect_phase6_atomic({cid: verdict(cid, 'FAIL' if cid in ['b', 'c'] else 'PASS',
            ref_ids=reference_ids(claims[cid])) for cid, _ in prepared['atomic_prompts']}, model_identity='host/first')
        runner.prepare_phase6_deep_review()
        result = runner.collect_phase6_deep_review({'b': verdict('b', ref_ids=['a'])},
                                                  model_identity='host/deep', allow_partial=True)
        self.assertEqual(result['pending_claim_ids'], ['c'])
        self.assertEqual(runner._contract_attempts, {})
        with self.assertRaises(ValueError):
            runner.finalize()

    def run_supplement(self, directory, layer, *, final_fail=False):
        runner = seeded()
        next(c for c in runner.run.claims if c['claim_id'] == 'b')['dependencies'].append('independent')
        runner.journal = RunJournal(directory)
        def response(prompt, review_layer):
            if '```json\n' in prompt:
                payload = json.loads(prompt.split('```json\n')[1].split('\n```')[0])
            else:
                payload = json.loads(prompt.split('<context-data>\n')[1].split('\n</context-data>')[0])
            cid = payload.get('claim_id') or payload['task']['claim_id']
            refs = payload['expected_reference_ids']
            target = layer == review_layer
            if cid == 'b' and target and 'accepted_reference_checks' not in payload:
                refs = ['a']
            status = 'FAIL' if cid == 'b' and layer == 'deep' and not target else 'PASS'
            if cid == 'b' and target and 'accepted_reference_checks' in payload and final_fail:
                status = 'FAIL'
            return verdict(cid, status, ref_ids=refs)
        verifier = CloudDouble('test/first', lambda prompt: response(prompt, 'atomic'))
        deep = CloudDouble('test/deep', lambda prompt: response(prompt, 'deep'))
        result = runner.execute_phase6(verifier, deep, CloudDouble('test/patch'),
                                      CloudDouble('test/global', lambda _: global_verdict(runner)))
        self.assertFalse(result['blocked'], result)
        return runner

    def test_completed_atomic_and_deep_supplements_restore_from_every_raw_part(self):
        for layer in ['atomic', 'deep']:
            with self.subTest(layer=layer), tempfile.TemporaryDirectory() as temp:
                runner = self.run_supplement(temp, layer)
                restored = load_checkpoint(Path(temp) / 'checkpoint.json')
                target = 'receipts' if layer == 'atomic' else 'deep_receipts'
                self.assertEqual(getattr(restored, target), getattr(runner, target))
                self.assertNotIn('b', restored.reference_supplements[layer])
                restored.journal = RunJournal(temp)
                def no_call(_):
                    self.fail('an accepted unchanged review must not call a model again')
                result = restored.execute_phase6(CloudDouble('test/first', no_call),
                    CloudDouble('test/deep', no_call), CloudDouble('test/patch', no_call),
                    CloudDouble('test/global', no_call))
                self.assertFalse(result['blocked'], result)

    def test_tampering_an_earlier_reference_answer_invalidates_only_its_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = self.run_supplement(temp, 'atomic')
            changed = False
            for path in (Path(temp) / 'responses/archive').glob('*.json'):
                row = json.loads(path.read_text())
                response = row.get('response', {})
                if isinstance(response, dict) and response.get('claim_id') == 'b' and [
                        check['ref_id'] for check in response.get('reference_checks', [])] == ['a']:
                    response['reference_checks'][0]['source_evidence'] = 'not the original evidence'
                    path.write_text(json.dumps(row))
                    changed = True
                    break
            self.assertTrue(changed)
            restored = load_checkpoint(Path(temp) / 'checkpoint.json')
            self.assertEqual(set(restored.receipts), set(runner.receipts) - {'b'})

    def test_composition_cannot_promote_an_earlier_partial_pass_over_final_fail(self):
        for reorder in [False, True]:
            with self.subTest(reorder=reorder), tempfile.TemporaryDirectory() as temp:
                self.run_supplement(temp, 'atomic', final_fail=True)
                path = Path(temp) / 'checkpoint.json'; state = json.loads(path.read_text())
                source = state['review_sources']['atomic']['b']
                first = source['sources'][0]
                if reorder:
                    source['sources'] = list(reversed(source['sources']))
                source['verdict_source'] = first
                state['audit']['atomic']['b']['result'].update(verdict='PASS', findings=[])
                path.write_text(json.dumps(state))
                restored = load_checkpoint(path)
                self.assertNotIn('b', restored.receipts)


if __name__ == '__main__':
    unittest.main()
