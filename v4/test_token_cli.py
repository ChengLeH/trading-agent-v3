import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from .agent_cli import main
from .agent_reviews import NativeReviewWorkflow
from .checkpoint import save_checkpoint
from .test_integration import seeded
from .test_agent_reviews import response


def invoke(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(args)
    return rc, json.loads(out.getvalue())


class TokenCliTests(unittest.TestCase):
    def setup_work(self, root):
        source = Path(root) / 'source.json'
        save_checkpoint(seeded(), source)
        return NativeReviewWorkflow.from_checkpoint(source, Path(root) / 'native')

    def test_file_delivery_materializes_exact_next_packet(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.setup_work(root)
            workflow.next()
            expected = workflow.next()
            rc, delivered = invoke(['next', '--work-dir', str(workflow.directory), '--delivery-files'])
            self.assertEqual(rc, 0)
            self.assertNotIn('items', delivered)
            pointer = delivered['delivery']
            rc, original = invoke(['read-task', '--manifest', pointer['manifest_file'], '--sha256', pointer['manifest_sha256'], '--action', 'materialize'])
            self.assertEqual(rc, 0)
            self.assertEqual(original, expected)

    def test_batched_claim_delivery_accepts_per_claim_answers(self):
        from .task_delivery import materialize_delivery
        from .schema import digest
        with tempfile.TemporaryDirectory() as root:
            workflow = self.setup_work(root)
            rc, delivered = invoke(['claim-reviews', '--work-dir', str(workflow.directory), '--executor', 'host/batch', '--batch-size', '4', '--delivery-files'])
            self.assertEqual(rc, 0)
            reserved = materialize_delivery(delivered['delivery'])
            self.assertEqual(sum(len(r['claim_ids']) for r in reserved['requests']), 4)
            for request in reserved['requests']:
                self.assertLessEqual(len(request['claim_ids']), 4)
                with workflow.queue.transaction() as state:
                    packet = state['packets'][reserved['packet_id']]
                    answers = [response(cid, packet['items'][cid]) for cid in request['claim_ids']]
                result = workflow.submit_review_request(reserved['packet_id'], request['request_id'], {'results': answers}, 'host/batch', reserved['input_hash'])
                self.assertFalse(result.get('review_errors'), result)

    def test_submit_then_next_returns_saved_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.setup_work(root)
            packet = workflow.next()
            path = Path(root) / 'answers.json'
            path.write_text(json.dumps({cid: response(cid, item) for cid, item in packet['items'].items()}))
            rc, result = invoke(['submit', '--work-dir', str(workflow.directory), '--packet-id', packet['packet_id'], '--input-hash', packet['input_hash'], '--executor', 'host/test', '--responses', str(path), '--next', '--delivery-files'])
            self.assertEqual(rc, 0, result)
            self.assertTrue(result['submission_saved'])
            self.assertIn('delivery', result['next'])
