"""Legacy scope migration uses ordinary patches and semantic revalidation."""
import tempfile
import unittest

from .catalog_scope_migration import prepare_catalog_scope_migration, validate_scope_patches
from .test_agent_reviews import NativeReviews


class CatalogScopeMigration(unittest.TestCase):
    def test_migration_batches_changes_and_revalidates_descendants(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = NativeReviews().bootstrap(root)
            NativeReviews()._reach_global(workflow)
            outcome = prepare_catalog_scope_migration(workflow, ['a', 'b'], reason='classify legacy scopes')
            self.assertEqual(set(outcome['allowed_claim_ids']), {'a', 'b'})
            from .agent_reviews import NativeReviewWorkflow
            workflow = NativeReviewWorkflow(workflow.directory)
            packet = workflow.next()
            self.assertEqual(packet['kind'], 'patch')
            self.assertIn('不要提交其他 Claim', packet['items']['claim_patcher']['prompt'])
            patches = [dict(claim_id=cid, assertion=dict(kind='interpretation',
                       catalog_scope='referenced_sources')) for cid in ('a', 'b')]
            result = workflow.submit_patch(packet['packet_id'], patches, 'host/test', packet['input_hash'])
            self.assertEqual(set(result['revalidate_claim_ids']), {'a', 'b', 'c'})
            with workflow.queue.transaction() as state:
                self.assertNotIn('catalog_scope_migration', state)
                runner = workflow._load(state)
                self.assertNotIn('a', runner.receipts)
                self.assertNotIn('b', runner.receipts)
                self.assertNotIn('c', runner.receipts)
                self.assertIn('independent', runner.receipts)

    def test_scope_migration_never_accepts_other_changes(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = NativeReviews().bootstrap(root)
            NativeReviews()._reach_global(workflow)
            prepare_catalog_scope_migration(workflow, ['a'], reason='classify legacy scope')
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                marker = state['catalog_scope_migration']
                with self.assertRaisesRegex(ValueError, 'only assertion'):
                    validate_scope_patches(runner, [dict(claim_id='a', text='rewritten')], marker)
                with self.assertRaisesRegex(ValueError, 'other assertion'):
                    validate_scope_patches(runner, [dict(claim_id='a', assertion=dict(
                        kind='forecast', catalog_scope='referenced_sources'))], marker)
