"""Explicit one-generation paid replay and interrupted archive transactions."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .agy_session import run_session, load_agent_profile, AntigravityError, abandon_session
from .session_state import SessionState
from .session_replay import replay_marker
from .storage import write_json
from .test_cloud import cli_stub


def paused(directory, **changes):
    store=SessionState(directory,'model','original request',load_agent_profile()[1])
    with store.lock():
        store.save(status='paused',request_sent=True,turn_acknowledged=True,continuations=2,
                   last_error='result_failed',active_attempt_id='old-attempt',process_id=None,
                   conversation_id='11111111-1111-4111-8111-111111111111')
        if changes: store.save(**changes)
    return store

FRESH='''
init()
assert "--conversation" not in sys.argv
assert json.loads(sys.stdin.readline())["message"]["content"] == "original request"
result('{"fresh":true}')
'''


class SessionReplay(unittest.TestCase):
    def test_explicit_replay_archives_state_and_allows_one_new_generation(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub(FRESH) as executable:
            directory=Path(temp)/'session';paused(directory)
            before=(directory/'session.json').read_bytes()
            archive=abandon_session(directory,reason='operator explicitly authorizes one paid retry')
            self.assertEqual((archive/'session.json').read_bytes(),before)
            self.assertFalse(directory.exists())
            self.assertEqual(abandon_session(directory,reason='repeat same request'),archive)
            self.assertEqual(run_session(executable,'model','original request',2,session_dir=directory),'{"fresh":true}')
            state=json.loads((directory/'session.json').read_text())
            self.assertEqual(state['replay_generation'],replay_marker(directory)['generation'])
            self.assertEqual(state['continuations'],0)
            with self.assertRaisesRegex(ValueError,'complete'):
                abandon_session(directory,reason='must not discard successful reply')

    def test_refuses_complete_live_unknown_delivery_and_concurrent_session(self):
        for changes in (dict(status='complete'),dict(process_id=os.getpid()),dict(turn_acknowledged=False)):
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as temp:
                directory=Path(temp)/'session';paused(directory,**changes)
                with self.assertRaises(ValueError): abandon_session(directory,reason='operator request')
                self.assertTrue((directory/'session.json').exists())
        with tempfile.TemporaryDirectory() as temp:
            store=paused(Path(temp)/'session')
            with store.lock(),self.assertRaises(BlockingIOError):
                abandon_session(store.directory,reason='concurrent request')

    def test_archive_rename_failure_is_recovered_before_one_fresh_request(self):
        with tempfile.TemporaryDirectory() as temp,cli_stub(FRESH) as executable:
            directory=Path(temp)/'session';paused(directory)
            with patch('v4.session_replay.os.rename',side_effect=OSError('rename interrupted')),self.assertRaises(OSError):
                abandon_session(directory,reason='operator authorized replay')
            self.assertEqual(replay_marker(directory)['phase'],'prepared')
            self.assertEqual(run_session(executable,'model','original request',2,session_dir=directory),'{"fresh":true}')
            self.assertEqual(replay_marker(directory)['phase'],'committed')
            self.assertEqual(len(list(Path(temp).glob('session.abandoned.*'))),1)

    def test_crash_after_rename_before_marker_commit_recovers_same_generation(self):
        with tempfile.TemporaryDirectory() as temp,cli_stub(FRESH) as executable:
            directory=Path(temp)/'session';paused(directory)
            def interrupted(path,value):
                if value.get('phase')=='committed': raise OSError('commit interrupted')
                write_json(path,value)
            with patch('v4.session_replay.write_json',side_effect=interrupted),self.assertRaises(OSError):
                abandon_session(directory,reason='operator authorized replay')
            generation=replay_marker(directory)['generation']
            self.assertEqual(run_session(executable,'model','original request',2,session_dir=directory),'{"fresh":true}')
            self.assertEqual(replay_marker(directory)['generation'],generation)
            self.assertEqual(len(list(Path(temp).glob('session.abandoned.*'))),1)

    def test_replay_authorization_cannot_be_used_for_another_prompt_or_model(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'session';paused(directory);abandon_session(directory,reason='one exact request')
            for model,prompt in [('other','original request'),('model','different request')]:
                with patch('v4.agy_session.subprocess.Popen') as start,self.assertRaisesRegex(AntigravityError,'session_binding_changed'):
                    run_session('agy',model,prompt,2,session_dir=directory)
                start.assert_not_called()

    def test_complete_terminal_cannot_be_abandoned_when_metadata_is_paused(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'session';paused(directory,last_error='cli_io_failed')
            (directory/'events.jsonl').write_text(json.dumps(dict(event='result',v4_attempt_id='old-attempt',result=dict(status='SUCCESS',response='{"done":true}')))+'\n')
            with self.assertRaisesRegex(ValueError,'complete result already saved'):
                abandon_session(directory,reason='must recover saved answer')
            self.assertTrue((directory/'session.json').exists())
            self.assertIsNone(replay_marker(directory))

    def test_missing_or_malformed_delivery_flags_cannot_authorize_paid_replay(self):
        for key,value in [('request_sent',None),('request_sent','false'),('turn_acknowledged','true'),('conversation_id',None)]:
            with self.subTest(key=key,value=value),tempfile.TemporaryDirectory() as temp:
                directory=Path(temp)/'session';paused(directory)
                path=directory/'session.json';data=json.loads(path.read_text())
                if value is None: data.pop(key)
                else: data[key]=value
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError): abandon_session(directory,reason='invalid state must block')
                self.assertIsNone(replay_marker(directory))
                self.assertTrue(path.exists())

    def test_journal_generation_snapshot_cannot_lose_replay_authorization(self):
        from .journal import RunJournal
        from .cloud import AntigravityCloudClient
        from .test_integration import seeded
        from .test_recovery_errors import ERROR_BODY
        from .session_replay import replay_marker as actual_marker
        with tempfile.TemporaryDirectory() as temp,cli_stub(ERROR_BODY) as failing,cli_stub(FRESH) as working:
            r=seeded();r.journal=RunJournal(temp)
            client=AntigravityCloudClient('model',executable=failing,timeout=3)
            with self.assertRaisesRegex(AntigravityError,'session_pending'):
                r._complete(client,'bull','original request',reuse_role_output=True)
            record_path=next((Path(temp)/'responses').glob('*.json'))
            directory=Path(json.loads(record_path.read_text())['session_directory'])
            injected=False
            def old_snapshot(path):
                nonlocal injected
                value=actual_marker(path)
                if not injected:
                    injected=True;abandon_session(directory,reason='operator replay between snapshot and request')
                return value
            client.executable=working
            with patch('v4.journal.replay_marker',side_effect=old_snapshot):
                self.assertEqual(r._complete(client,'bull','original request',reuse_role_output=True),dict(fresh=True))
            self.assertEqual(json.loads(record_path.read_text())['replay_generation'],actual_marker(directory)['generation'])
            with patch('v4.agy_session.subprocess.Popen') as start:
                self.assertEqual(r._complete(client,'bull','original request',reuse_role_output=True),dict(fresh=True))
            start.assert_not_called()
