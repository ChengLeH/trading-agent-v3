"""Regression coverage for starting a fresh verification from a saved run."""
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from .checkpoint import load_checkpoint, save_checkpoint
from .test_integration import seeded, review_all


class CheckpointRecoveryRegression(unittest.TestCase):
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
