"""Original-role reconsideration is durable work, never a finding-route waiver."""
from copy import deepcopy
import tempfile
import unittest

from .agent_reviews import NativeReviewWorkflow
from . import test_agent_reviews as fixtures


class NativeRedeliberationTests(unittest.TestCase):
    def reach_reconsideration(self, root):
        workflow = fixtures.NativeReviews().bootstrap(root)
        for _ in range(20):
            packet = workflow.next()
            if packet.get('kind') not in ('atomic', 'deep'):
                return workflow, packet
            answers = {}
            for cid, item in packet['items'].items():
                answers[cid] = fixtures.response(cid, item, 'FAIL' if cid == 'a' else 'PASS')
                if cid == 'a':
                    answers[cid]['findings'][0].update(
                        resolution_route='role_redeliberation',
                        resolution_reason='The author must reconsider the causal conclusion.')
            workflow.submit(packet['packet_id'], answers, 'host/reviewer', packet['input_hash'])
        self.fail('Original-role reconsideration was never reached')

    def answer(self, workflow, text='The original causal inference is withdrawn.'):
        with workflow.queue.transaction() as state:
            runner = workflow._load(state)
            claim = deepcopy(next(c for c in runner.run.claims if c['claim_id'] == 'a'))
        if text is not None:
            claim['text'] = text
        return dict(claims=[claim], reason='I reconsidered the supplied evidence and the finding.')

    def test_role_packet_survives_reload_and_revalidates_only_affected_descendants(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            self.assertEqual(packet.get('kind'), 'role_redeliberation', packet)
            self.assertEqual(list(packet['items']), ['bull'])
            self.assertEqual(packet['allowed_claim_ids'], ['a'])
            restored = NativeReviewWorkflow(workflow.directory)
            self.assertEqual(restored.next()['packet_id'], packet['packet_id'])
            outcome = restored.submit(packet['packet_id'], {'bull': self.answer(restored)},
                                      'host/main', packet['input_hash'])
            self.assertEqual(set(outcome['revalidate_claim_ids']), {'a', 'b', 'c'})
            with restored.queue.transaction() as state:
                runner = restored._load(state)
                self.assertIn('independent', runner.receipts)
                self.assertNotIn('a', runner.receipts)
                self.assertTrue(any(f['resolution_route'] == 'role_redeliberation'
                                    for f in runner.deferred_global_findings))
                candidates = runner._role_redeliberation_candidates(runner.deferred_global_findings)
                self.assertTrue(candidates)
                self.assertEqual(candidates[0]['claim_ids'], ['a'])
            self.assertEqual(restored.next()['kind'], 'atomic')

    def test_maintain_after_multi_packet_global_can_revalidate_and_start_fresh_sweep(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = fixtures.NativeReviews().bootstrap(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner.global_dependency_batch_size = 4
                workflow._save(state, runner)
            packet = fixtures.NativeReviews()._reach_global(workflow)
            first = True
            old_packets = []
            for _ in range(12):
                answer = fixtures.NativeReviews()._global_response(workflow)
                if first:
                    answer['findings'] = [dict(claim_id='a', issue='Reconsider the causal conclusion.',
                        evidence='Fixture premise requires original-author judgment.',
                        resolution_route='role_redeliberation', resolution_reason='Original author must reconsider.')]
                    first = False
                old_packets.append(packet['packet_id'])
                outcome = workflow.submit(packet['packet_id'],
                    {next(iter(packet['items'])): answer}, 'host/global', packet['input_hash'])
                if outcome['status'] == 'revision':
                    break
                packet = NativeReviewWorkflow(workflow.directory).next()
            self.assertGreater(len(old_packets), 1)
            reconsider = NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(reconsider['kind'], 'role_redeliberation', reconsider)
            original_hash = reconsider['input_hash']
            answer = self.answer(workflow, text=None)
            result = workflow.submit(reconsider['packet_id'], {'bull': answer},
                                     'host/main', original_hash)
            self.assertEqual(set(result['revalidate_claim_ids']), {'a', 'b', 'c'})
            reopened = NativeReviewWorkflow(workflow.directory)
            atomic = reopened.next()
            self.assertEqual(atomic['kind'], 'atomic', atomic)
            self.assertEqual(atomic['input_hash'], original_hash)
            fresh = fixtures.NativeReviews()._reach_global(reopened)
            self.assertNotIn(fresh['packet_id'], old_packets)
            with reopened.queue.transaction() as state:
                runner = reopened._load(state)
                self.assertEqual(runner.revision_round, 1)
                self.assertTrue(runner.deferred_global_findings)
                self.assertIsNone(runner.global_receipt)
                records = runner._role_redeliberation_candidates(runner.deferred_global_findings)
                finding_id = runner.deferred_global_findings[0]['finding_id']
            answer = fixtures.NativeReviews()._global_response(reopened)
            answer['resolved_external_findings'] = [dict(finding_id=finding_id, claim_ids=['a'],
                resolution_refs=[records[0]['attempt_id']], reason='Original author reconsidered and fresh reviews passed.',
                evidence='Current original-role answer is archived and matches the current Claim.')]
            for _ in range(12):
                outcome = reopened.submit(fresh['packet_id'],
                    {next(iter(fresh['items'])): answer}, 'host/global', fresh['input_hash'])
                if outcome['status'] == 'verified':
                    break
                fresh = NativeReviewWorkflow(workflow.directory).next()
                answer = fixtures.NativeReviews()._global_response(reopened)
            self.assertEqual(outcome['status'], 'verified', outcome)
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['status'], 'complete')

    def test_wrong_author_scope_and_cycle_are_rejected_without_losing_the_packet(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            self.assertEqual(packet.get('kind'), 'role_redeliberation', packet)
            for change in (dict(author_role='bear'), dict(claim_id='independent'),
                           dict(dependencies=['c'])):
                with self.subTest(change=change):
                    answer = self.answer(workflow)
                    answer['claims'][0].update(change)
                    before = workflow.queue.state_path.read_bytes()
                    with self.assertRaises(ValueError):
                        workflow.submit(packet['packet_id'], {'bull': answer},
                                        'host/main', packet['input_hash'])
                    self.assertEqual(workflow.queue.state_path.read_bytes(), before)
                    self.assertEqual(workflow.next()['packet_id'], packet['packet_id'])

    def test_unchanged_author_answer_does_not_self_resolve_the_finding(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            self.assertEqual(packet.get('kind'), 'role_redeliberation', packet)
            outcome = workflow.submit(packet['packet_id'], {'bull': self.answer(workflow, text=None)},
                                      'host/main', packet['input_hash'])
            self.assertEqual(set(outcome['revalidate_claim_ids']), {'a', 'b', 'c'})
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertTrue(runner.deferred_global_findings)
                self.assertNotIn('a', runner.receipts)
                self.assertIsNone(runner.pending_patch)
            self.assertEqual(workflow.next()['kind'], 'atomic')

    def test_received_role_reconsideration_can_be_resolved_by_fresh_global_review(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            self.assertEqual(packet.get('kind'), 'role_redeliberation', packet)
            workflow.submit(packet['packet_id'], {'bull': self.answer(workflow)},
                            'host/main', packet['input_hash'])
            global_packet = fixtures.NativeReviews()._reach_global(workflow)
            answer = fixtures.NativeReviews()._global_response(workflow)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                findings = runner.deferred_global_findings
                records = runner._role_redeliberation_candidates(findings)
                finding_id = findings[0]['finding_id']
            self.assertTrue(records)
            answer['resolved_external_findings'] = [dict(finding_id=finding_id,
                claim_ids=['a'], resolution_refs=[records[0]['attempt_id']],
                reason='The original author withdrew the unsupported premise, then fresh reviews passed.',
                evidence='The archived original-role answer matches the current canonical Claim.')]
            result = workflow.submit(global_packet['packet_id'],
                {next(iter(global_packet['items'])): answer}, 'host/global', global_packet['input_hash'])
            self.assertEqual(result['status'], 'verified', result)
            self.assertEqual(NativeReviewWorkflow(workflow.directory).next()['status'], 'complete')

    def test_exact_redelivery_after_changed_claim_is_idempotent_but_new_epoch_is_stale(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            self.assertEqual(packet.get('kind'), 'role_redeliberation', packet)
            answers = {'bull': self.answer(workflow)}
            workflow.submit(packet['packet_id'], answers, 'host/main', packet['input_hash'])
            result = NativeReviewWorkflow(workflow.directory).submit(
                packet['packet_id'], answers, 'host/main', packet['input_hash'])
            self.assertTrue(result['idempotent'])
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner._verification_epoch = 'f' * 32
                workflow._save(state, runner)
            with self.assertRaisesRegex(ValueError, 'stale'):
                workflow.submit(packet['packet_id'], answers, 'host/main', packet['input_hash'])

    def test_revision_limit_requires_explicit_cli_and_does_not_reset_used_rounds(self):
        import contextlib
        import io
        import json
        from .agent_cli import main
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                runner.revision_round = runner.max_revisions
                # Simulate an existing operator-only packet reconstructed at the cap.
                state['packets'][packet['packet_id']]['manual_only'] = True
                workflow._save(state, runner)
            answers = {'bull': self.answer(workflow)}
            with self.assertRaisesRegex(ValueError, 'explicit'):
                workflow.submit(packet['packet_id'], answers, 'host/main', packet['input_hash'])
            from pathlib import Path
            path = Path(root) / 'answer.json'
            path.write_text(json.dumps(answers))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = main(['submit-redeliberation', '--work-dir', str(workflow.directory),
                    '--packet-id', packet['packet_id'], '--input-hash', packet['input_hash'],
                    '--executor', 'host/main', '--responses', str(path)])
            self.assertEqual(rc, 0, output.getvalue())
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertGreater(runner.revision_round, runner.max_revisions)
                self.assertNotIn('a', runner.receipts)

    def test_rejected_graph_does_not_archive_a_role_answer_or_publish_progress(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            archives = workflow.directory / 'responses' / 'archive'
            before = {path.name for path in archives.glob('*.json')}
            answer = self.answer(workflow)
            answer['claims'][0]['dependencies'] = ['c']
            with self.assertRaises(ValueError):
                workflow.submit(packet['packet_id'], {'bull': answer}, 'host/main', packet['input_hash'])
            self.assertEqual({path.name for path in archives.glob('*.json')}, before)
            self.assertFalse((workflow.directory / 'progress.json').exists())
            workflow.submit(packet['packet_id'], {'bull': self.answer(workflow)},
                            'host/main', packet['input_hash'])
            self.assertFalse((workflow.directory / 'progress.json').exists())

    def test_maintained_conclusion_consumes_one_reconsideration_round(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            with workflow.queue.transaction() as state:
                before = workflow._load(state).revision_round
            workflow.submit(packet['packet_id'], {'bull': self.answer(workflow, text=None)},
                            'host/main', packet['input_hash'])
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertEqual(runner.revision_round, before + 1)
                self.assertTrue(runner.deferred_global_findings)

    def test_later_reconsideration_carries_prior_open_findings_for_the_same_author(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, first = self.reach_reconsideration(root)
            workflow.submit(first['packet_id'], {'bull': self.answer(workflow, 'First revised conclusion.')},
                            'host/main', first['input_hash'])
            for _ in range(16):
                packet = workflow.next()
                if packet.get('kind') == 'role_redeliberation':
                    break
                self.assertIn(packet.get('kind'), ('atomic', 'deep'), packet)
                answers = {cid: fixtures.response(cid, item, 'FAIL' if cid == 'a' else 'PASS')
                           for cid, item in packet['items'].items()}
                if 'a' in answers:
                    answers['a']['findings'][0].update(resolution_route='role_redeliberation',
                        resolution_reason='The first reconsideration still leaves a causal error.')
                workflow.submit(packet['packet_id'], answers, 'host/reviewer', packet['input_hash'])
            self.assertTrue(set(first['finding_ids']).issubset(packet['finding_ids']))
            self.assertGreater(len(packet['finding_ids']), len(first['finding_ids']))
            workflow.submit(packet['packet_id'], {'bull': self.answer(workflow, 'Second revised conclusion.')},
                            'host/main', packet['input_hash'])
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                for finding in runner.deferred_global_findings:
                    self.assertTrue(runner._role_redeliberation_candidates([finding]))

    def test_additional_role_response_notes_are_archived_without_blocking_valid_claims(self):
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            answer = self.answer(workflow)
            answer['notes'] = {'confidence': 'limited to supplied evidence'}
            result = workflow.submit(packet['packet_id'], {'bull': answer}, 'host/main', packet['input_hash'])
            self.assertIn('a', result['revalidate_claim_ids'])

    def test_packet_created_at_the_saved_revision_cap_is_operator_only(self):
        from unittest.mock import patch
        original_seed = fixtures.seeded
        def seed_at_limit():
            runner = original_seed()
            runner.revision_round = runner.max_revisions
            return runner
        with tempfile.TemporaryDirectory() as root, \
                patch('v4.test_agent_reviews.seeded', side_effect=seed_at_limit):
            workflow, packet = self.reach_reconsideration(root)
            self.assertEqual(packet['kind'], 'role_redeliberation')
            self.assertEqual(packet['status'], 'blocked:revision_limit')
            self.assertTrue(packet['manual_only'])
            self.assertTrue(packet['blocked'])
            with self.assertRaisesRegex(ValueError, 'explicit'):
                workflow.submit(packet['packet_id'], {'bull': self.answer(workflow)},
                                'host/main', packet['input_hash'])

    def test_source_append_invalidates_old_role_proof_even_when_claim_text_is_unchanged(self):
        from .evidence_repair import append_sources
        from . import test_general_contracts as evidence_fixtures
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            workflow.submit(packet['packet_id'], {'bull': self.answer(workflow)},
                            'host/main', packet['input_hash'])
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                prior_claims = deepcopy(runner.run.claims)
                self.assertTrue(runner._role_redeliberation_candidates(runner.deferred_global_findings))
            manifest = evidence_fixtures.EvidenceRecoveryContracts().manifest(root)
            append_sources(workflow, manifest, reason='Additional canonical context must be considered.')
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertEqual(runner.run.claims, prior_claims)
                self.assertFalse(runner._role_redeliberation_candidates(runner.deferred_global_findings))

    def test_queue_commit_failure_reuses_the_archived_role_answer_on_retry(self):
        from unittest.mock import patch
        from .journal import RunJournal
        with tempfile.TemporaryDirectory() as root:
            workflow, packet = self.reach_reconsideration(root)
            answer = self.answer(workflow)
            before = workflow.queue.state_path.read_bytes()
            with patch('v4.agent_queue.write_json', side_effect=OSError('simulated queue commit failure')):
                with self.assertRaisesRegex(OSError, 'queue commit failure'):
                    workflow.submit(packet['packet_id'], {'bull': answer}, 'host/main', packet['input_hash'])
            self.assertEqual(workflow.queue.state_path.read_bytes(), before)
            journal = RunJournal(workflow.directory)
            archived = [row for row in journal.received_attempts('fixture')
                        if row['task_id'] == 'bull' and row['response'] == answer]
            self.assertEqual(len(archived), 1)
            result = NativeReviewWorkflow(workflow.directory).submit(packet['packet_id'],
                {'bull': answer}, 'host/main', packet['input_hash'])
            self.assertEqual(set(result['revalidate_claim_ids']), {'a', 'b', 'c'})
            replayed = [row for row in journal.received_attempts('fixture')
                        if row['task_id'] == 'bull' and row['response'] == answer]
            self.assertEqual([row['attempt_id'] for row in replayed], [archived[0]['attempt_id']])
            with workflow.queue.transaction() as state:
                runner = workflow._load(state)
                self.assertEqual(runner.revision_round, 1)
                self.assertTrue(runner._role_redeliberation_candidates(runner.deferred_global_findings))
