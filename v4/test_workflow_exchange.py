import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from .checkpoint import save_checkpoint
from .test_integration import seeded
from .test_agent_reviews import response
from .agent_reviews import NativeReviewWorkflow


class WorkflowExchangeTests(unittest.TestCase):
    def test_submit_and_next_preserves_receipts_and_duplicate_is_idempotent(self):
        from .workflow_exchange import submit_and_next
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source.json'
            save_checkpoint(seeded(), source)
            workflow = NativeReviewWorkflow.from_checkpoint(source, Path(root) / 'native')
            packet = workflow.next()
            answers = {cid: response(cid, item) for cid, item in packet['items'].items()}
            result = submit_and_next(workflow, packet['packet_id'], answers, 'host/test', packet['input_hash'])
            self.assertTrue(result['submission_saved'])
            self.assertNotEqual(result['next']['packet_id'], packet['packet_id'])
            duplicate = submit_and_next(workflow, packet['packet_id'], answers, 'host/test', packet['input_hash'])
            self.assertEqual(duplicate['next']['packet_id'], result['next']['packet_id'])
            with workflow.queue.transaction() as state:
                self.assertTrue(set(answers) <= set(workflow._load(state).receipts))

    def test_next_failure_does_not_undo_saved_submission(self):
        from .workflow_exchange import submit_and_next
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source.json'
            save_checkpoint(seeded(), source)
            workflow = NativeReviewWorkflow.from_checkpoint(source, Path(root) / 'native')
            packet = workflow.next()
            answers = {cid: response(cid, item) for cid, item in packet['items'].items()}
            with patch.object(workflow, 'next', side_effect=OSError('delivery unavailable')):
                result = submit_and_next(workflow, packet['packet_id'], answers, 'host/test', packet['input_hash'])
            self.assertTrue(result['submission_saved'])
            self.assertEqual(result['next_error']['type'], 'OSError')
            restored = NativeReviewWorkflow(workflow.directory)
            with restored.queue.transaction() as state:
                self.assertTrue(set(answers) <= set(restored._load(state).receipts))
            self.assertTrue(restored.next()['items'])
