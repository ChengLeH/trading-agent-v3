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

    def test_shared_delivery_takes_precedence_over_old_role_file_flag(self):
        from .task_delivery import review_brief
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'news.json').write_text(json.dumps({'items': [{'title': 'Fixture', 'url': 'https://example.test', 'published': '2026-10-15'}]}))
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'subject': {'ticker': 'TEST'}, 'as_of': '2026-10-16', 'sources': [{'id': 'news', 'type': 'news_json', 'path': 'news.json'}]}))
            workflow = NativeReviewWorkflow.from_manifest(manifest, root / 'native')
            rc, delivered = invoke(['next', '--work-dir', str(workflow.directory), '--delivery-files', '--prompt-files'])
            self.assertEqual(rc, 0, delivered)
            brief = review_brief(delivered['delivery'], 'market_analyst')
            self.assertIn('instructions', brief)
            self.assertIn('facts', brief['payload_keys'])

    def test_cost_audit_is_readonly_and_canonical_summary_uses_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = self.setup_work(root)
            packet = workflow.next()
            workflow.submit(packet['packet_id'], {cid: response(cid, item) for cid, item in packet['items'].items()}, 'host/test', packet['input_hash'])
            before = workflow.queue.state_path.read_bytes()
            rc, metrics = invoke(['audit-cost', '--work-dir', str(workflow.directory)])
            self.assertEqual(rc, 0, metrics)
            self.assertEqual(before, workflow.queue.state_path.read_bytes())
            self.assertIsNone(metrics['usage_tokens']['input_tokens'])
            rc, summary = invoke(['completion-summary', '--work-dir', str(workflow.directory)])
            self.assertEqual(rc, 0, summary)
            self.assertEqual(summary['completed_count'], len(packet['items']))
            self.assertEqual(summary['receipt_status'], 'accepted')
            self.assertNotIn('findings', summary)

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
