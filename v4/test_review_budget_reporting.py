"""Effective single-item and packet limits must not be confused in recovery hints."""
import tempfile
import unittest
from . import test_agent_reviews as fixtures


class ReviewBudgetReporting(unittest.TestCase):
    def test_block_reports_the_effective_item_limit_separately_from_packet_limit(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = fixtures.NativeReviews().bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner.review_batch_max_bytes = 1024
                state.setdefault('options', {})['review_packet_max_bytes'] = 262144
                workflow._save(state, runner)
            packet = workflow.next()
            self.assertEqual(packet['status'], 'blocked:review_context_budget')
            self.assertIn('item_budget=1024', packet['reason'])
            self.assertIn('packet_budget=262144', packet['reason'])
            self.assertIn('effective_item_budget=1024', packet['reason'])
            self.assertTrue(packet['oversized_items'])
            self.assertTrue(all(r['budget_bytes'] == 1024 for r in packet['oversized_items']))

    def test_deep_transport_round_trips_claim_bindings_and_full_evidence(self):
        import json
        from .atomic_phase6 import AtomicTask
        from .review_wire import decode_review_payload
        from .test_integration import seeded, verdict
        from .test_review_wire import claim, dimensions
        shared = dimensions()
        row = claim('a', [shared] * 8)
        row.update(refs=[], fact_refs=[], dependencies=[])
        task = AtomicTask('a', row, {'opaque': {'kind': 'evidence', 'value': {'text': '完整來源'}}},
                          [], [], context={})
        runner = seeded()
        runner.receipts['a'] = dict(result=verdict('a', 'FAIL'))
        prompt = runner._deep_prompt(task)
        payload = json.loads(prompt.split('<context-data>\n', 1)[1].split('\n</context-data>', 1)[0])
        self.assertIn('binding_dimension_catalog', payload)
        self.assertEqual(decode_review_payload(payload)['task'], task.review_dict())

    def test_unprojected_claim_binding_extra_is_not_mistaken_for_a_wire_reference(self):
        from .review_wire import decode_review_payload, project_review_payload
        from .test_review_wire import claim, dimensions
        row = claim('a', [dimensions()])
        row['bindings'][0]['binding_dimension_ref'] = 'an existing opaque annotation'
        payload = {'task': {'claim': row, 'context': {}}}
        self.assertEqual(project_review_payload(payload), payload)
        self.assertEqual(decode_review_payload(payload), payload)

    def test_fresh_runner_uses_the_agreed_review_defaults(self):
        from .runner import PipelineRunnerV4
        runner = PipelineRunnerV4()
        self.assertEqual(runner.review_batch_max_bytes, 128 * 1024)
        self.assertEqual(runner.review_deep_batch_max_bytes, 128 * 1024)

    def test_fresh_cloud_cli_uses_runtime_defaults_and_preserves_explicit_budget(self):
        import contextlib
        import io
        import json
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from .__main__ import main
        observed = []
        def stop_before_models(runner, *args):
            observed.append((runner.review_batch_max_bytes, runner.review_deep_batch_max_bytes))
            return dict(status='budget_inspected', blocked=False)
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / 'manifest.json'
            manifest.write_text(json.dumps(dict(subject=dict(ticker='TEST'))))
            base = ['--manifest', str(manifest), '--out', str(Path(root) / 'out')]
            for role in ('author', 'verifier', 'deep', 'global'):
                base += [f'--{role}-model', 'test/cloud']
            clients = {role: SimpleNamespace(calls=[]) for role in ('author', 'verifier', 'deep', 'global')}
            with patch('v4.__main__._cloud_clients', return_value=clients), \
                    patch('v4.__main__.PipelineRunnerV4.execute', new=stop_before_models), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(base), 0)
                self.assertEqual(main(base + ['--review-batch-max-bytes', '65536']), 0)
        self.assertEqual(observed, [(131072, 131072), (65536, 65536)])
