"""Regression coverage for starting a fresh verification from a saved run."""
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from .checkpoint import load_checkpoint, save_checkpoint
from .test_integration import seeded, review_all


class CheckpointRecoveryRegression(unittest.TestCase):
    def test_cloud_driver_rejects_ephemeral_output_before_writing(self):
        import contextlib
        import io
        import json
        from .__main__ import main
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / 'run'
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['--resume', 'unused.json', '--out', str(output_dir),
                             '--author-model', 'a', '--verifier-model', 'b',
                             '--deep-model', 'c', '--global-model', 'd'])
            self.assertEqual(code, 2)
            self.assertIn('temporary storage', json.loads(output.getvalue())['reason'])
            self.assertFalse(output_dir.exists())

    def test_native_load_skips_unchanged_checkpoint_but_repairs_stale_view(self):
        from .test_agent_reviews import NativeReviews
        from .storage import write_json
        with tempfile.TemporaryDirectory() as root:
            workflow = NativeReviews().bootstrap(root)
            with workflow.queue.transaction() as state:
                workflow._load(state)
                with patch('v4.agent_workflow.write_json', wraps=write_json) as writer:
                    workflow._load(state)
                    writer.assert_not_called()
                    workflow.checkpoint_path.write_text('{}', encoding='utf-8')
                    workflow._load(state)
                    writer.assert_called_once()

    def test_combined_patches_invalidate_shared_descendants_once(self):
        runner = seeded()
        review_all(runner)
        runner.prepare_phase6_revision_v4([
            dict(claim_id='a', issue='first correction'),
            dict(claim_id='b', issue='second correction')])
        outcome = runner.collect_phase6_revision([
            dict(claim_id='a', text='corrected first claim'),
            dict(claim_id='b', text='corrected second claim')])
        self.assertEqual(set(outcome['revalidate_claim_ids']), {'a', 'b', 'c'})
        self.assertEqual(len(outcome['revalidate_claim_ids']), 3)
        self.assertIn('independent', runner.receipts)
        self.assertEqual(runner.revision_round, 1)

    def test_invalid_batch_keeps_pending_repairs_and_receipts_for_retry(self):
        from copy import deepcopy
        runner = seeded()
        review_all(runner)
        before = deepcopy(runner.run.claims)
        receipts = deepcopy(runner.receipts)
        runner.prepare_phase6_revision_v4([
            dict(claim_id='a', issue='first correction'),
            dict(claim_id='b', issue='second correction')])
        with self.assertRaises((ValueError, TypeError, KeyError)):
            runner.collect_phase6_revision([
                dict(claim_id='a', text='corrected first claim'),
                dict(claim_id='b', dependencies=['missing-claim'])])
        self.assertEqual(runner.run.claims, before)
        self.assertEqual(runner.receipts, receipts)
        self.assertEqual(set(runner.pending_patch['ids']), {'a', 'b', 'c'})
        outcome = runner.collect_phase6_revision([
            dict(claim_id='a', text='corrected first claim'),
            dict(claim_id='b', text='corrected second claim')])
        self.assertEqual(set(outcome['revalidate_claim_ids']), {'a', 'b', 'c'})

    def test_fresh_verification_loads_deep_transport_attempt_ids(self):
        with tempfile.TemporaryDirectory() as root:
            runner = seeded()
            runner.deep_finding_baselines = {
                'deep.' + 'a' * 64: dict(trusted=True, source_ids=[], claim_hashes={},
                                          transport_attempt_ids=[uuid4().hex])}
            path = Path(root) / 'checkpoint.json'
            save_checkpoint(runner, path)
            loaded = load_checkpoint(path, continue_verification=False)
            self.assertEqual(len(loaded.deep_finding_baselines), 1)
