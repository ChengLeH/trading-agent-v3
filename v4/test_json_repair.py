"""Strict JSON correction is a bounded continuation of the same conversation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .cloud import AntigravityCloudClient
from .agy_session import AntigravityError,run_session
from .session_state import SessionState
from .test_cloud import cli_stub

REPAIR_BODY='''
init()
request=json.loads(sys.stdin.readline())["message"]["content"]
if "--conversation" not in sys.argv:
    assert request == "original evidence"
    result('```json\\n{"ok":true}\\n```')
else:
    assert request != "original evidence"
    assert "合法 JSON" in request
    result('{"ok":true}')
'''


class JsonRepair(unittest.TestCase):
    def test_bad_json_corrects_same_cid_without_extracting_fenced_content(self):
        with tempfile.TemporaryDirectory() as temp,cli_stub(REPAIR_BODY) as executable:
            client=AntigravityCloudClient('model',executable=executable,timeout=3,session_root=temp)
            self.assertEqual(client.complete('original evidence'),dict(ok=True))
            state=json.loads(next(Path(temp).glob('*/session.json')).read_text())
            self.assertEqual(state['continuations'],1)
            self.assertEqual(state['status'],'complete')
            self.assertTrue(state['json_required'])
            rows=[json.loads(line) for line in next(Path(temp).glob('*/events.jsonl')).read_text().splitlines()]
            self.assertEqual(len([r for r in rows if r['event']=='result']),2)
            self.assertIn('```json',next(r['result']['response'] for r in rows if r['event']=='result'))

    def test_bad_json_budget_survives_new_clients_and_never_opens_fourth_turn(self):
        body='init()\nsys.stdin.readline()\nresult("not JSON")\n'
        with tempfile.TemporaryDirectory() as temp,cli_stub(body) as executable:
            client=AntigravityCloudClient('model',executable=executable,timeout=3,session_root=temp)
            with self.assertRaisesRegex(AntigravityError,'invalid_json'):
                client.complete('original evidence')
            state=json.loads(next(Path(temp).glob('*/session.json')).read_text())
            self.assertEqual((state['status'],state['last_error'],state['continuations']),('paused','invalid_json',2))
            another=AntigravityCloudClient('model',executable=executable,timeout=3,session_root=temp)
            with patch('v4.agy_session.subprocess.Popen') as start,self.assertRaisesRegex(AntigravityError,'continuation_exhausted'):
                another.complete('original evidence')
            start.assert_not_called()

    def test_successful_terminal_with_bad_json_recovers_as_paused_not_complete(self):
        with tempfile.TemporaryDirectory() as temp,cli_stub('init()') as executable:
            # Capture a completed bad result under the older raw transport API.
            with cli_stub('init()\nsys.stdin.readline()\nresult("not JSON")') as old:
                self.assertEqual(run_session(old,'model','original evidence',3,session_dir=temp),'not JSON')
            path=Path(temp)/'session.json';state=json.loads(path.read_text());state['status']='in_flight';state['last_error']=None;state.pop('response');path.write_text(json.dumps(state))
            with patch('v4.agy_session.subprocess.Popen') as start,self.assertRaisesRegex(AntigravityError,'invalid_json'):
                run_session(executable,'model','original evidence',3,session_dir=temp,require_json=True)
            start.assert_not_called()
            state=json.loads(path.read_text())
            self.assertEqual((state['status'],state['last_error']),('paused','invalid_json'))

    def test_terminal_metadata_write_failure_recovers_saved_result_without_model_call(self):
        from .storage import write_json
        with tempfile.TemporaryDirectory() as temp,cli_stub('init()\nsys.stdin.readline()\nresult(\'{"ok":true}\')') as executable:
            failed=False
            def fail_complete(path,value):
                nonlocal failed
                if value.get('status')=='complete' and not failed:
                    failed=True;raise OSError('state write interrupted')
                write_json(path,value)
            with patch('v4.session_state.write_json',side_effect=fail_complete),self.assertRaises(AntigravityError):
                run_session(executable,'model','original evidence',3,session_dir=temp,require_json=True)
            with patch('v4.agy_session.subprocess.Popen') as start:
                self.assertEqual(run_session(executable,'model','original evidence',3,session_dir=temp,require_json=True,resume=True),'{"ok":true}')
            start.assert_not_called()

    def test_json_flags_without_durable_session_are_rejected_before_spawn(self):
        with patch('v4.agy_session.subprocess.Popen') as start,self.assertRaisesRegex(ValueError,'persistent session_dir'):
            run_session('agy','model','request',2,require_json=True)
        start.assert_not_called()
