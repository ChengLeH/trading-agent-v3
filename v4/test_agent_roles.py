"""Native role generation uses real collectors and restart-safe candidates."""
from pathlib import Path
import tempfile
import unittest
from .agent_workflow import NativeWorkflow
from .checkpoint import save_checkpoint
from .test_integration import seeded
from .test_journal import author_response


class NativeRoles(unittest.TestCase):
    def test_role_prompt_is_compact_without_losing_context(self):
        import json
        from .runner import ROLE_CONTRACT, json_prompt
        runner = seeded()
        role, prompt = runner._role_prompts(['market_analyst'])[0]
        packet = runner.orch.get_packet(runner.run_id, role)
        payload = prompt.split('<context-data>\n', 1)[1].split('\n</context-data>', 1)[0]
        self.assertEqual(json.loads(payload), packet)
        self.assertLess(len(prompt.encode()), len(json_prompt(ROLE_CONTRACT, packet).encode()))

    def test_failed_role_redelivery_gives_retry_action_and_does_not_replace_response(self):
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root)
            packet = workflow.next(); role = 'market_analyst'
            bad = {'claims': [{}]}
            result = workflow.submit(packet['packet_id'], {role: bad}, 'host/main', packet['input_hash'])
            self.assertEqual(result['next_action'], 'retry-role')
            result = NativeWorkflow(workflow.directory).submit(packet['packet_id'], {role: bad}, 'host/main', packet['input_hash'])
            self.assertTrue(result['role_retry_required'])
            self.assertIn(role, result['retry_roles'])
            with self.assertRaisesRegex(ValueError, 'retry-role'):
                workflow.submit(packet['packet_id'], {role: {'claims': []}}, 'host/main', packet['input_hash'])
            with workflow.queue.transaction() as state:
                self.assertEqual(state['packets'][packet['packet_id']]['received'][role]['response'], bad)
            workflow.retry_role(role)
            self.assertIn(role, workflow.next()['items'])

    def test_large_role_prompt_has_exact_durable_artifact_and_budget_notice(self):
        import hashlib
        import json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root)
            prompt = '資料' * 10000
            with workflow.queue.transaction() as state:
                state['options'] = dict(role_prompt_budget_bytes=1024)
            with patch('v4.runner.PipelineRunnerV4._role_prompts', return_value=[
                    (role, prompt) for role in ('market_analyst','fundamentals_analyst','news_analyst','social_analyst')]):
                packet = workflow.next()
            item = packet['items']['market_analyst']
            delivery = item['delivery']
            self.assertEqual(delivery['prompt_bytes'], len(prompt.encode('utf-8')))
            self.assertTrue(delivery['budget_exceeded'])
            self.assertEqual(delivery['prompt_sha256'], hashlib.sha256(prompt.encode('utf-8')).hexdigest())
            self.assertEqual(json.loads(Path(delivery['prompt_file']).read_text())['prompt'], prompt)
            self.assertEqual(item['prompt'], prompt)  # no evidence truncation
            resumed = NativeWorkflow(workflow.directory).next()
            self.assertEqual(resumed['items']['market_analyst']['delivery'], delivery)
            self.assertEqual(packet['warnings'][0]['code'], 'role_prompt_budget_exceeded')

    def bootstrap(self, root):
        runner = seeded(); runner.run.claims = []; runner.run.analyses = {}
        source = Path(root) / 'original.json'
        save_checkpoint(runner, source)
        return source, NativeWorkflow.from_checkpoint(source, Path(root) / 'native')

    def test_retry_keeps_shape_rejected_raw_response_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root); packet = workflow.next()
            role = 'fundamentals_analyst'
            bad = {'claims': [dict(claim_id=role + '.one', text='Original financial assertion',
                                  refs=['invented-source'], fact_refs=['invented-fact'])]}
            workflow.submit(packet['packet_id'], {role: bad}, 'host/main', packet['input_hash'])
            NativeWorkflow(workflow.directory).retry_role(role)
            following = NativeWorkflow(workflow.directory).next()
            self.assertIn('Original financial assertion', following['items'][role]['prompt'])
            self.assertIn('invented-fact', following['items'][role]['prompt'])
            with workflow.queue.transaction() as state:
                self.assertEqual(state['role_retry_context'][role]['previous_response'], bad)

    def test_group_reference_error_routes_exact_claim_and_field(self):
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root); packet = workflow.next()
            outputs = {role: author_response(item['prompt']) for role, item in packet['items'].items()}
            bad = outputs['fundamentals_analyst']['claims'][0]
            bad['refs'] = ['invented-source']
            outcome = workflow.submit(packet['packet_id'], outputs, 'host/main', packet['input_hash'])
            validation = outcome['role_validation']
            self.assertEqual(validation['author_role'], 'fundamentals_analyst')
            self.assertEqual(validation['claim_id'], bad['claim_id'])
            self.assertEqual(validation['field'], 'refs')
            self.assertEqual(validation['invalid_ids'], ['invented-source'])
            self.assertEqual(outcome['allowed_retry_roles'], ['fundamentals_analyst'])

    def test_retry_cannot_drop_citations_from_an_unchanged_assertion(self):
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root); packet = workflow.next()
            role = 'fundamentals_analyst'; output = author_response(packet['items'][role]['prompt'])
            output['claims'][0]['refs'] = ['invented-source']
            siblings = {r: author_response(item['prompt']) for r, item in packet['items'].items() if r != role}
            workflow.submit(packet['packet_id'], {**siblings, role: output}, 'host/main', packet['input_hash'])
            workflow.retry_role(role); following = workflow.next()
            output['claims'][0]['refs'] = []
            result = workflow.submit(following['packet_id'], {role: output}, 'host/main', following['input_hash'])
            self.assertEqual(result['status'], 'blocked:roles')
            self.assertEqual(result['role_errors'][role]['role_validation']['code'], 'unchanged_claim_sources_removed')
            with workflow.queue.transaction() as state:
                self.assertEqual(state['checkpoint']['state']['claims'], [])
            workflow.retry_role(role); repeated = workflow.next()
            result = workflow.submit(repeated['packet_id'], {role: output}, 'host/main', repeated['input_hash'])
            self.assertEqual(result['role_errors'][role]['role_validation']['code'], 'unchanged_claim_sources_removed')
            # A genuinely revised source-free interpretation remains eligible
            # for the mandatory semantic check; code does not classify its prose.
            workflow.retry_role(role); third = workflow.next()
            output['claims'][0]['text'] = 'Revised interpretation with no external assertion.'
            result = workflow.submit(third['packet_id'], {role: output}, 'host/main', third['input_hash'])
            self.assertNotEqual(result['status'], 'blocked:roles')
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertEqual(len(runner.run.claims), 4)
                self.assertEqual(runner.receipts, {})

    def test_all_roles_survive_partial_submissions_and_new_instances(self):
        with tempfile.TemporaryDirectory() as root:
            source, workflow = self.bootstrap(root); original = source.read_bytes(); completed = []
            while True:
                packet = workflow.next()
                if packet['status'] == 'phase6_ready':
                    break
                role = next(iter(packet['items']))
                output = author_response(packet['items'][role]['prompt'])
                outcome = workflow.submit(packet['packet_id'], {role: output}, 'host/' + role, packet['input_hash'])
                self.assertFalse(outcome.get('blocked'), outcome)
                completed.append(role)
                workflow = NativeWorkflow(workflow.directory)
                following = workflow.next()
                if following.get('packet_id') == packet['packet_id']:
                    self.assertNotIn(role, following['items'])
            self.assertEqual(len(completed), 9)
            self.assertEqual(packet['claim_count'], 9)
            self.assertEqual(packet['semantic_receipts'], 0)
            self.assertEqual(source.read_bytes(), original)
            with self.assertRaisesRegex(ValueError, 'committed'):
                workflow.retry_role('market_analyst')

    def test_retry_bad_shape_preserves_other_candidates_and_retires_old_packet(self):
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root); packet = workflow.next()
            outputs = {role: author_response(item['prompt']) for role, item in packet['items'].items()}
            outputs['fundamentals_analyst'] = {'claims': []}
            outcome = workflow.submit(packet['packet_id'], outputs, 'host/one', packet['input_hash'])
            self.assertEqual(set(outcome['role_errors']), {'fundamentals_analyst'})
            self.assertEqual(workflow.next()['status'], 'blocked:roles')
            workflow.retry_role('fundamentals_analyst')
            again = NativeWorkflow(workflow.directory).next()
            self.assertEqual(set(again['items']), {'fundamentals_analyst'})
            with self.assertRaises(ValueError):
                workflow.submit(packet['packet_id'], outputs, 'host/one', packet['input_hash'])
            result = workflow.submit(again['packet_id'], {'fundamentals_analyst':
                author_response(again['items']['fundamentals_analyst']['prompt'])}, 'host/two', again['input_hash'])
            self.assertEqual(result['missing_item_ids'], [])
            with workflow.queue.transaction() as state:
                self.assertEqual(len(state['checkpoint']['state']['claims']), 4)
                self.assertIn(packet['packet_id'], state['packets'])

    def test_group_source_error_does_not_label_every_sibling_invalid(self):
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root); packet = workflow.next()
            outputs = {role: author_response(item['prompt']) for role, item in packet['items'].items()}
            outputs['fundamentals_analyst']['claims'][0]['refs'] = ['unknown.source']
            outcome = workflow.submit(packet['packet_id'], outputs, 'host/one', packet['input_hash'])
            self.assertEqual(outcome['status'], 'blocked:roles')
            workflow.retry_role('fundamentals_analyst')
            again = workflow.next()
            self.assertEqual(set(again['items']), {'fundamentals_analyst'})
            revised = author_response(again['items']['fundamentals_analyst']['prompt'])
            revised['claims'][0]['text'] = 'Revised unsupported assertion into an explicit limitation.'
            result = workflow.submit(again['packet_id'], {'fundamentals_analyst': revised},
                                     'host/two', again['input_hash'])
            self.assertFalse(result.get('blocked'), result)
            self.assertEqual(set(workflow.next()['items']), {'bull', 'bear'})

    def test_import_cannot_overwrite_original_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            runner = seeded(); source = Path(root) / 'checkpoint.json'
            save_checkpoint(runner, source); original = source.read_bytes()
            with self.assertRaisesRegex(ValueError, 'source'):
                NativeWorkflow.from_checkpoint(source, root)
            self.assertEqual(source.read_bytes(), original)

    def test_received_role_is_recovered_after_crash_before_collector(self):
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root); packet = workflow.next()
            role = next(iter(packet['items']))
            workflow.queue.submit(packet['packet_id'], {role: author_response(packet['items'][role]['prompt'])},
                                  'host/one', packet['input_hash'])
            resumed = NativeWorkflow(workflow.directory).next()
            self.assertEqual(resumed['packet_id'], packet['packet_id'])
            self.assertNotIn(role, resumed['items'])
            with workflow.queue.transaction() as state:
                self.assertIn(role, state['role_candidates'])

    def test_tampered_staged_candidate_cannot_enter_canonical_graph(self):
        with tempfile.TemporaryDirectory() as root:
            _, workflow = self.bootstrap(root); packet = workflow.next()
            first = 'market_analyst'
            workflow.submit(packet['packet_id'], {first: author_response(packet['items'][first]['prompt'])},
                            'host/one', packet['input_hash'])
            with workflow.queue.transaction() as state:
                state['role_candidates'][first]['claims'][0]['text'] = 'not the received output'
            remaining = {role: author_response(item['prompt']) for role, item in packet['items'].items() if role != first}
            result = NativeWorkflow(workflow.directory).submit(packet['packet_id'], remaining,
                                                              'host/two', packet['input_hash'])
            self.assertEqual(result['status'], 'blocked:roles')
            self.assertIn('untrusted role candidate', result['role_errors'][first]['reason'])
            with workflow.queue.transaction() as state:
                self.assertEqual(state['checkpoint']['state']['claims'], [])


if __name__ == '__main__':
    unittest.main()
