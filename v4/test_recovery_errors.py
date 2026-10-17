"""Application-level failures resume the bound conversation, never a new task."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .test_cloud import cli_stub
from .agy_session import run_session, AntigravityError

ERROR_BODY = '''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
emit(dict(event="step_update", step_update=dict(step_type="error_message", state="DONE", text_delta="upstream application error")))
'''
RESUMED_BODY = '''
init()
assert "--conversation" in sys.argv
request=json.loads(sys.stdin.readline())["message"]["content"]
assert request != "original request"
result('{"done":true}')
'''


class RecoveryErrors(unittest.TestCase):
    def test_application_error_is_resumable_without_fresh_replay(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as executable:
            with self.assertRaises(AntigravityError) as caught:
                run_session(executable,"model","original request",2,session_dir=temp)
            self.assertEqual(caught.exception.code,"session_pending")
            self.assertFalse(caught.exception.retryable)
            data=json.loads((Path(temp)/'session.json').read_text())
            self.assertEqual(data['last_error'],'result_failed')
            with cli_stub(RESUMED_BODY) as working:
                self.assertEqual(run_session(working,'model','original request',2,session_dir=temp,resume=True),' {"done":true}'.strip())

    def test_legacy_invalid_stream_error_message_can_resume(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as executable:
            with self.assertRaises(AntigravityError):
                run_session(executable,'model','original request',2,session_dir=temp)
            path=Path(temp)/'session.json'; data=json.loads(path.read_text());data['last_error']='invalid_stream';path.write_text(json.dumps(data))
            with cli_stub(RESUMED_BODY) as working:
                self.assertEqual(run_session(working,'model','original request',2,session_dir=temp,resume=True),'{"done":true}')

    def test_malformed_delivery_flags_never_start_a_continuation(self):
        for key in ("turn_acknowledged", "request_written", "json_required"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp, cli_stub(ERROR_BODY) as executable:
                with self.assertRaises(AntigravityError):
                    run_session(executable, "model", "original request", 2, session_dir=temp)
                path = Path(temp) / "session.json"
                data = json.loads(path.read_text())
                data[key] = "false"
                path.write_text(json.dumps(data))
                with patch("v4.agy_session.subprocess.Popen") as start:
                    with self.assertRaisesRegex(AntigravityError, "session_binding_changed"):
                        run_session(executable, "model", "original request", 2, session_dir=temp, resume=True)
                start.assert_not_called()
