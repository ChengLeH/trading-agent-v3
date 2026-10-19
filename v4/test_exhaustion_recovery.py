"""Budget diagnosis and explicitly scoped native recovery, with no real model."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .agy_session import AntigravityError, run_session, load_agent_profile
from .session_state import SessionState
from .cloud import AntigravityCloudClient
from .journal import RunJournal
from .test_integration import seeded, CloudDouble
from .test_cloud import cli_stub
from .test_recovery_errors import ERROR_BODY
from .test_session_replay import FRESH

CID = '11111111-1111-4111-8111-111111111111'


class ExhaustionRecovery(unittest.TestCase):
    def test_exhausted_budget_reports_which_limits_and_prior_failure_without_model_call(self):
        for cause in ('continuations', 'time', 'both'):
            with self.subTest(cause=cause), tempfile.TemporaryDirectory() as temp:
                store = SessionState(temp, 'model', 'original request', load_agent_profile()[1])
                with store.lock():
                    store.save(status='paused', request_sent=True, turn_acknowledged=True,
                               conversation_id=CID, last_error='invalid_json', continuations=2 if cause != 'time' else 0,
                               max_continuations=2, elapsed_seconds=10 if cause != 'continuations' else 1, budget_seconds=10)
                with patch('v4.agy_session.subprocess.Popen') as start, self.assertRaises(AntigravityError) as caught:
                    run_session('agy', 'model', 'original request', 2, session_dir=temp, resume=True)
                start.assert_not_called()
                details = caught.exception.details()
                self.assertTrue(details['resumable'])
                budget = details['session_budget']
                self.assertEqual(set(budget['exhausted_by']), {'continuations', 'time'} if cause == 'both' else {cause})
                self.assertEqual(budget['last_session_error'], 'invalid_json')
                self.assertEqual(budget['budget_seconds'], 10)

    def exhausted_request(self, directory, executable, schema=None):
        runner = seeded(); runner.journal = RunJournal(directory)
        client = AntigravityCloudClient('model', executable=executable, timeout=3, request_attempts=1)
        with self.assertRaises(AntigravityError):
            runner._complete(client, 'atomic.target', 'original request', response_schema=schema)
        path = next((Path(directory)/'sessions').glob('*/session.json'))
        state = json.loads(path.read_text()); state['continuations'] = 2
        path.write_text(json.dumps(state))
        with self.assertRaises(AntigravityError) as caught:
            runner._complete(client, 'atomic.target', 'original request', response_schema=schema)
        self.assertEqual(caught.exception.code, 'continuation_exhausted')
        return runner, client, path, caught.exception

    def test_group_signal_permission_denied_preserves_original_stream_error(self):
        # A sandbox may forbid killpg despite permitting termination of the
        # owned child PID. Cleanup must never overwrite the stream failure.
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as failing:
            with patch('v4.agy_session.os.killpg',
                       side_effect=PermissionError('sandbox denied group signal')) as group_kill:
                runner, client, path, exc = self.exhausted_request(temp, failing)
            self.assertTrue(group_kill.called)
            self.assertEqual(exc.code, 'continuation_exhausted')
            self.assertTrue(path.exists())

    def test_cloud_and_journal_preserve_budget_and_request_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as failing:
            runner, client, path, exc = self.exhausted_request(temp, failing)
            details = exc.details()
            self.assertEqual(details['request_context']['task_id'], 'atomic.target')
            self.assertEqual(details['request_context']['session_directory'], str(path.parent.resolve()))
            self.assertEqual(details['session_budget']['continuations_used'], 2)
            record = next(json.loads(p.read_text()) for p in (Path(temp)/'responses').glob('*.json'))
            self.assertEqual(record['session_budget'], details['session_budget'])

    def test_explicit_replay_reopens_only_failed_session_and_reuses_successful_requests(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as failing, cli_stub(FRESH) as working:
            runner, client, path, exc = self.exhausted_request(temp, failing)
            baseline = CloudDouble('test/other', lambda _: dict(kept=True))
            for i in range(49): runner._complete(baseline, f'atomic.good.{i}', f'stable request {i}')
            old_bytes = path.read_bytes()
            recovery = runner.journal.authorize_session_replay(runner, CID, reason='operator approves one new paid generation')
            self.assertEqual(recovery['task_id'], 'atomic.target')
            self.assertEqual((Path(recovery['archive_directory'])/'session.json').read_bytes(), old_bytes)
            client.executable = working
            self.assertEqual(runner._complete(client, 'atomic.target', 'original request'), dict(fresh=True))
            baseline.responder = lambda _: self.fail('successful item must be replayed, not regenerated')
            for i in range(49):
                self.assertEqual(runner._complete(baseline, f'atomic.good.{i}', f'stable request {i}'), dict(kept=True))
            self.assertEqual(len(baseline.calls), 49)
            with self.assertRaises(ValueError):
                runner.journal.authorize_session_replay(runner, CID, reason='must not replay twice')

    def test_replay_refuses_blank_reason_wrong_run_unknown_cid_and_completed_state(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as failing:
            runner, client, path, exc = self.exhausted_request(temp, failing)
            for cid, reason in ((CID, ''), ('22222222-2222-4222-8222-222222222222', 'operator')):
                with self.subTest(cid=cid, reason=reason), self.assertRaises(ValueError):
                    runner.journal.authorize_session_replay(runner, cid, reason=reason)
            wrong = seeded(); wrong.run_id = 'other-run'
            with self.assertRaises(ValueError):
                runner.journal.authorize_session_replay(wrong, CID, reason='operator')
            state = json.loads(path.read_text()); state['status'] = 'complete'; state['response'] = '{"done":true}'
            path.write_text(json.dumps(state))
            with self.assertRaises(ValueError):
                runner.journal.authorize_session_replay(runner, CID, reason='operator')
            self.assertTrue(path.exists())

    def test_cli_requires_explicit_scoped_authorization_and_records_it(self):
        from .__main__ import main
        from .checkpoint import save_checkpoint
        from .runner import PipelineRunnerV4
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp)/'checkpoint.json'; save_checkpoint(seeded(), checkpoint)
            argv = ['--resume', str(checkpoint), '--out', str(Path(temp)/'out')]
            for role in ('author', 'verifier', 'deep', 'global'): argv += [f'--{role}-model', 'model']
            with patch('v4.__main__.AntigravityCloudClient', return_value=CloudDouble()), \
                 patch.object(RunJournal, 'authorize_session_replay', return_value=dict(task_id='atomic.target')) as authorize, \
                 patch.object(PipelineRunnerV4, 'resume', return_value=dict(blocked=True, status='test_pause')):
                self.assertEqual(main(argv + ['--recover-session', CID, '--replay-reason', 'operator approves']), 2)
                self.assertEqual(authorize.call_args.args[1], CID)
                self.assertEqual(authorize.call_args.kwargs['reason'], 'operator approves')
            result = json.loads((Path(temp)/'out/result.json').read_text())
            self.assertEqual(result['session_recovery']['task_id'], 'atomic.target')
            with patch('v4.__main__.AntigravityCloudClient') as client:
                with self.assertRaises(SystemExit): main(argv + ['--recover-session', CID])
                client.assert_not_called()

    def test_budget_diagnostics_reject_raw_prompt_or_account_fields(self):
        with self.assertRaises(ValueError):
            AntigravityError('continuation_exhausted', session_budget=dict(prompt='private evidence'))
        with self.assertRaises(ValueError):
            AntigravityError('continuation_exhausted', request_context=dict(account='private account'))

    def test_failed_batch_identifies_claims_from_structured_review_metadata(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as failing:
            runner = seeded(); runner.journal = RunJournal(temp)
            client = AntigravityCloudClient('model', executable=failing, timeout=3, request_attempts=1)
            with self.assertRaises(AntigravityError) as caught:
                runner._complete(client, 'atomic.batch.fixture', 'original request', review_context=dict(
                    layer='atomic', verification_epoch=runner._verification_epoch,
                    task_hashes={'claim.one':'hash-one', 'claim.two':'hash-two'}))
            self.assertEqual(caught.exception.details()['request_context']['claim_ids'], ['claim.one', 'claim.two'])

    def test_replay_refuses_external_paths_symlinks_and_mismatched_request_binding(self):
        import shutil
        for mutation in ('external', 'symlink', 'prompt', 'attempt'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as failing:
                out = Path(temp)/'run'
                runner, client, path, exc = self.exhausted_request(out, failing)
                record_path = next((out/'responses').glob('*.json'))
                record = json.loads(record_path.read_text())
                if mutation in ('external', 'symlink'):
                    external = Path(temp)/'unrelated-session'; shutil.copytree(path.parent, external)
                    if mutation == 'symlink':
                        link = out/'sessions'/'external-link'; link.symlink_to(external, target_is_directory=True)
                        record['session_directory'] = str(link)
                    else: record['session_directory'] = str(external)
                    record_path.write_text(json.dumps(record))
                elif mutation == 'prompt':
                    request_path = path.parent/'request.json'; request = json.loads(request_path.read_text())
                    request['prompt'] = 'another request'; request_path.write_text(json.dumps(request))
                else:
                    record['attempt_id'] = 'invalid'; record_path.write_text(json.dumps(record))
                with self.assertRaises(ValueError):
                    runner.journal.authorize_session_replay(runner, CID, reason='operator')
                self.assertTrue(path.exists())
                if mutation in ('external', 'symlink'): self.assertTrue((external/'session.json').exists())

    def test_schema_bound_request_can_be_authorized_and_repeated_authorization_is_idempotent(self):
        schema = dict(type='object', required=['done'])
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as failing:
            runner, client, path, exc = self.exhausted_request(temp, failing, schema)
            first = runner.journal.authorize_session_replay(runner, CID, reason='original operator reason')
            repeated = runner.journal.authorize_session_replay(runner, CID, reason='repeat after interruption')
            self.assertEqual(first['generation'], repeated['generation'])
            self.assertEqual(first['archive_directory'], repeated['archive_directory'])
            self.assertEqual(repeated['reason'], 'original operator reason')
