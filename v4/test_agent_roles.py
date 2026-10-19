"""Native role generation uses real collectors and restart-safe candidates."""
from pathlib import Path
import tempfile
import unittest
from .agent_workflow import NativeWorkflow
from .checkpoint import save_checkpoint
from .test_integration import seeded
from .test_journal import author_response


class NativeRoles(unittest.TestCase):
    def bootstrap(self, root):
        runner = seeded(); runner.run.claims = []; runner.run.analyses = {}
        source = Path(root) / 'original.json'
        save_checkpoint(runner, source)
        return source, NativeWorkflow.from_checkpoint(source, Path(root) / 'native')

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
            result = workflow.submit(again['packet_id'], {'fundamentals_analyst':
                author_response(again['items']['fundamentals_analyst']['prompt'])}, 'host/two', again['input_hash'])
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
