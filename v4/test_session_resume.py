"""Durable timeout recovery must never create an unbound new conversation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .agy_session import run_session, AntigravityError
from .test_cloud import cli_stub


class SessionResume(unittest.TestCase):
    def test_timeout_saves_conversation_and_partial_output_without_fresh_retry(self):
        body = '''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
emit(dict(event="step_update", step_update=dict(step_type="agent_response", text_delta='{"partial":true}')))
time.sleep(10)
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            with self.assertRaises(AntigravityError) as caught:
                run_session(executable, "model", "original request", 1, session_dir=temp)
            self.assertEqual(caught.exception.code, "session_pending")
            self.assertFalse(caught.exception.retryable)
            state = json.loads((Path(temp) / "session.json").read_text())
            self.assertTrue(state["conversation_id"])
            self.assertTrue(state["request_sent"])
            self.assertIn('partial', (Path(temp) / "events.jsonl").read_text())
            with patch("v4.agy_session.subprocess.Popen") as start:
                with self.assertRaisesRegex(AntigravityError, "session_pending"):
                    run_session(executable, "model", "original request", 2, session_dir=temp)
                start.assert_not_called()

    def test_explicit_resume_uses_same_id_workspace_and_continuation_only(self):
        body = '''
init()
request = json.loads(sys.stdin.readline())["message"]["content"]
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
if "--conversation" not in sys.argv:
    assert request == "original request"
    time.sleep(10)
else:
    assert sys.argv[sys.argv.index("--conversation") + 1] == "11111111-1111-4111-8111-111111111111"
    assert request != "original request"
    assert "接續" in request
    result('{"done":true}')
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            with self.assertRaises(AntigravityError):
                run_session(executable, "model", "original request", 1, session_dir=temp)
            original = json.loads((Path(temp)/"session.json").read_text())
            self.assertEqual(run_session(executable, "model", "original request", 2, session_dir=temp, resume=True), '{"done":true}')
            after = json.loads((Path(temp)/"session.json").read_text())
            self.assertEqual(after["agent_name"], original["agent_name"])
            self.assertEqual(after["conversation_id"], original["conversation_id"])
            self.assertEqual(after["continuations"], 1)
            self.assertEqual(after["status"], "complete")

    def test_changed_input_or_model_and_wrong_resumed_id_cannot_fallback(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub('init()\nsys.stdin.readline()\nemit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))\ntime.sleep(10)') as executable:
            with self.assertRaises(AntigravityError):
                run_session(executable,"model","original",1,session_dir=temp)
            for model,prompt in (("other","original"),("model","changed")):
                with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaises(AntigravityError):
                    run_session(executable,model,prompt,2,session_dir=temp,resume=True)
                start.assert_not_called()
            with cli_stub('init(conversation="22222222-2222-4222-8222-222222222222")\ntime.sleep(10)') as wrong:
                with patch("v4.agy_session.os.write", wraps=__import__('os').write) as write, self.assertRaises(AntigravityError) as caught:
                    run_session(wrong,"model","original",2,session_dir=temp,resume=True)
                self.assertEqual(caught.exception.code,"conversation_unverified")
                write.assert_not_called()

    def test_unacknowledged_delivery_never_starts_a_second_request(self):
        from .agy_session import load_agent_profile
        from .session_state import SessionState
        with tempfile.TemporaryDirectory() as temp:
            store = SessionState(temp, "model", "request", load_agent_profile()[1])
            with store.lock():
                store.save(request_sent=True, turn_acknowledged=False, status="in_flight")
            with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaisesRegex(AntigravityError, "delivery_unconfirmed"):
                run_session("agy", "model", "request", 2, session_dir=temp, resume=True)
            start.assert_not_called()

    def test_terminal_event_survives_crash_before_response_archive(self):
        from .agy_session import load_agent_profile
        from .session_state import SessionState
        with tempfile.TemporaryDirectory() as temp:
            store = SessionState(temp, "model", "request", load_agent_profile()[1])
            with store.lock():
                store.save(request_sent=True, turn_acknowledged=True, status="in_flight", active_attempt_id="attempt")
                store.event(dict(event="result", result=dict(status="SUCCESS", response='{"done":true}')))
            with patch("v4.agy_session.subprocess.Popen") as start:
                self.assertEqual(run_session("agy", "model", "request", 2, session_dir=temp, resume=True), '{"done":true}')
            start.assert_not_called()

    def test_continuation_limit_budget_and_process_lock_survive_restart(self):
        from .agy_session import load_agent_profile
        from .session_state import SessionState
        with tempfile.TemporaryDirectory() as temp:
            store = SessionState(temp, "model", "request", load_agent_profile()[1])
            with store.lock():
                store.save(request_sent=True, turn_acknowledged=True, status="paused", last_error="timeout",
                           conversation_id="11111111-1111-4111-8111-111111111111", continuations=2)
                with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaisesRegex(AntigravityError, "session_busy"):
                    run_session("agy", "model", "request", 2, session_dir=temp, resume=True)
                start.assert_not_called()
            with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaisesRegex(AntigravityError, "continuation_exhausted"):
                run_session("agy", "model", "request", 2, session_dir=temp, resume=True)
            start.assert_not_called()
            with store.lock():
                store.save(continuations=0, elapsed_seconds=6, budget_seconds=6)
            with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaisesRegex(AntigravityError, "continuation_exhausted"):
                run_session("agy", "model", "request", 2, session_dir=temp, resume=True)
            start.assert_not_called()

    def test_live_orphan_and_corrupt_event_tail_never_start_another_turn(self):
        import os
        from .agy_session import load_agent_profile
        from .session_state import SessionState
        with tempfile.TemporaryDirectory() as temp:
            store = SessionState(temp, "model", "request", load_agent_profile()[1])
            with store.lock():
                store.save(request_sent=True, turn_acknowledged=True, status="in_flight", process_id=os.getpid())
            with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaisesRegex(AntigravityError, "session_busy"):
                run_session("agy", "model", "request", 2, session_dir=temp, resume=True)
            start.assert_not_called()
            with store.lock():
                store.save(process_id=None)
            (Path(temp) / "events.jsonl").write_text('{"event":')
            with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaisesRegex(AntigravityError, "session_binding_changed"):
                run_session("agy", "model", "request", 2, session_dir=temp, resume=True)
            start.assert_not_called()

    def test_pre_init_timeout_can_retry_without_resending_a_paid_turn(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub('time.sleep(10)') as stalled:
            with patch("v4.agy_session.os.write", wraps=__import__('os').write) as write, self.assertRaises(AntigravityError) as caught:
                # The initial timeout also sets the persisted total budget;
                # leave room for real subprocess startup on the next attempt.
                run_session(stalled, "model", "request", 1, session_dir=temp)
            self.assertEqual(caught.exception.code, "missing_init")
            self.assertTrue(caught.exception.retryable)
            write.assert_not_called()
            state = json.loads((Path(temp) / "session.json").read_text())
            self.assertFalse(state["request_sent"])
            with cli_stub("""init()\nsys.stdin.readline()\nresult('{"done":true}')""") as working:
                self.assertEqual(run_session(working, "model", "request", 2, session_dir=temp, resume=True), '{"done":true}')

    def test_operator_can_raise_exhausted_limits_without_resetting_used_work(self):
        from .session_state import SessionState
        from .agy_session import load_agent_profile
        with tempfile.TemporaryDirectory() as temp:
            store = SessionState(temp, "model", "request", load_agent_profile()[1])
            with store.lock():
                store.save(request_sent=True, turn_acknowledged=True, status="paused", last_error="timeout",
                           conversation_id="11111111-1111-4111-8111-111111111111", continuations=2,
                           elapsed_seconds=6, budget_seconds=6)
            body = "init()\nassert \"--conversation\" in sys.argv\nsys.stdin.readline()\nresult('{\"done\":true}')"
            with cli_stub(body) as executable:
                self.assertEqual(run_session(executable, "model", "request", 2, session_dir=temp,
                                             resume=True, max_continuations=5, budget_seconds=20), '{"done":true}')
            state = json.loads((Path(temp) / "session.json").read_text())
            self.assertEqual(state["continuations"], 3)
            self.assertEqual(state["max_continuations"], 5)
            self.assertEqual(state["budget_seconds"], 20)
            self.assertGreaterEqual(state["elapsed_seconds"], 6)
            with store.lock():
                store.configure_limits(2, max_continuations=1, budget_seconds=1)
            self.assertEqual(store.data["max_continuations"], 5)
            self.assertEqual(store.data["budget_seconds"], 20)
            self.assertEqual(len(store.data["limit_changes"]), 1)

    def test_cloud_timeout_automatically_continues_same_cid_without_resending_task(self):
        from .cloud import AntigravityCloudClient
        body = r'''
init()
message=json.loads(sys.stdin.readline())["message"]["content"]
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
if "--conversation" not in sys.argv:
    assert message == "original request"
    time.sleep(10)
else:
    assert message != "original request"
    assert "接續" in message
    result('{"done":true}')
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            client = AntigravityCloudClient("model", executable=executable, timeout=2, session_root=temp)
            self.assertEqual(client.complete("original request"), dict(done=True))
            state = json.loads(next(Path(temp).glob("*/session.json")).read_text())
            self.assertEqual(state["continuations"], 1)
            self.assertEqual(state["conversation_id"], "11111111-1111-4111-8111-111111111111")

    def test_continuation_startup_timeout_preserves_previous_resumable_turn(self):
        from .session_state import SessionState
        from .agy_session import load_agent_profile
        with tempfile.TemporaryDirectory() as temp, cli_stub("time.sleep(10)") as stalled:
            store = SessionState(temp, "model", "request", load_agent_profile()[1])
            with store.lock():
                store.save(request_sent=True, turn_acknowledged=True, status="paused", last_error="timeout",
                           conversation_id="11111111-1111-4111-8111-111111111111", continuations=0,
                           elapsed_seconds=1, budget_seconds=10)
            with self.assertRaises(AntigravityError) as caught:
                run_session(stalled, "model", "request", 1, session_dir=temp, resume=True)
            self.assertEqual(caught.exception.code, "missing_init")
            self.assertTrue(caught.exception.retryable)
            state = json.loads((Path(temp) / "session.json").read_text())
            self.assertEqual((state["last_error"], state["continuations"]), ("timeout", 0))
            with cli_stub("init()\nassert \"--conversation\" in sys.argv\nsys.stdin.readline()\nresult()") as working:
                run_session(working, "model", "request", 2, session_dir=temp, resume=True)
            self.assertEqual(json.loads((Path(temp) / "session.json").read_text())["continuations"], 1)

    def test_invalid_configurable_limits_never_spawn(self):
        for limits in (dict(max_continuations=True), dict(max_continuations=-1),
                       dict(budget_seconds=float("inf")), dict(budget_seconds=0)):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as temp:
                with patch("v4.agy_session.subprocess.Popen") as start, self.assertRaises(ValueError):
                    run_session("agy", "model", "request", 2, session_dir=temp, **limits)
                start.assert_not_called()


if __name__ == '__main__': unittest.main()

class StreamInterruptionRecovery(unittest.TestCase):
    ERROR = 'The stream was interrupted. Please continue the task you were working on.'

    def test_transport_retry_does_not_spend_content_continuations_and_survives_restart(self):
        from .cloud import AntigravityCloudClient
        body = '''
init()
request = json.loads(sys.stdin.readline())["message"]["content"]
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
marker = Path.cwd()/"transport-count"
count = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(count+1))
if count < 3:
    emit(dict(event="result", result=dict(status="ERROR", error="The stream was interrupted. Please continue the task you were working on.", error_code=None)))
else:
    assert "--conversation" in sys.argv
    assert "original request" not in request
    result('{"done":true}')
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable, patch('v4.cloud.sleep') as sleep:
            client = AntigravityCloudClient('model', executable=executable, session_root=temp, timeout=2, request_attempts=2, max_continuations=0)
            with self.assertRaises(AntigravityError) as caught:
                client.complete('original request')
            self.assertEqual(caught.exception.code, 'stream_interrupted')
            self.assertTrue(caught.exception.retryable)
            self.assertTrue(caught.exception.details()['resumable'])
            # A new client/process starts from the persisted conversation.
            client = AntigravityCloudClient('model', executable=executable, session_root=temp, timeout=2, request_attempts=3, max_continuations=0)
            self.assertEqual(client.complete('original request'), {'done': True})
            state = json.loads(next(Path(temp).glob('*/session.json')).read_text())
            self.assertEqual(state['continuations'], 0)
            self.assertEqual(state['transport_retries'], 3)
            self.assertGreater(state['elapsed_seconds'], 0)
            self.assertTrue(sleep.called)

    def test_legacy_result_failed_with_exhausted_count_is_reclassified_from_exact_event(self):
        body = '''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
if "--conversation" not in sys.argv:
    emit(dict(event="result", result=dict(status="ERROR", error="The stream was interrupted. Please continue the task you were working on.")))
else:
    result('{"done":true}')
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            with self.assertRaises(AntigravityError):
                run_session(executable,'model','original',2,session_dir=temp)
            path = Path(temp)/'session.json'
            state = json.loads(path.read_text())
            state.update(last_error='result_failed', continuations=2, max_continuations=2)
            path.write_text(json.dumps(state))
            self.assertEqual(run_session(executable,'model','original',2,session_dir=temp,resume=True), '{"done":true}')
            state = json.loads(path.read_text())
            self.assertEqual(state['continuations'],2)  # never erase historical consumption
            self.assertEqual(state['transport_retries'],1)

    def test_time_limit_still_blocks_transport_recovery_without_new_call(self):
        from .session_state import SessionState
        from .agy_session import load_agent_profile
        with tempfile.TemporaryDirectory() as temp:
            store = SessionState(temp,'model','original',load_agent_profile()[1])
            with store.lock():
                store.save(status='paused',request_sent=True,turn_acknowledged=True,
                           conversation_id='11111111-1111-4111-8111-111111111111',
                           last_error='stream_interrupted',continuations=0,max_continuations=0,
                           elapsed_seconds=10,budget_seconds=10)
            with patch('v4.agy_session.subprocess.Popen') as start, self.assertRaises(AntigravityError) as caught:
                run_session('agy','model','original',2,session_dir=temp,resume=True)
            start.assert_not_called()
            self.assertEqual(caught.exception.session_budget['exhausted_by'],['time'])

    def test_unknown_result_errors_and_success_prose_do_not_get_transport_exception(self):
        for status, error in [('ERROR','unknown failure'), ('SUCCESS',self.ERROR), ('ERROR','Explanation: '+self.ERROR)]:
            body = 'init()\nsys.stdin.readline()\nemit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))\n'
            body += f'emit(dict(event="result",result=dict(status={status!r},error={error!r},response="not JSON")))\n'
            with self.subTest(status=status,error=error), tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
                with self.assertRaises(AntigravityError):
                    run_session(executable,'model','original',2,session_dir=temp,require_json=True)
                state=json.loads((Path(temp)/'session.json').read_text())
                self.assertEqual(state['last_error'],'invalid_json' if status=='SUCCESS' else 'result_failed')

    def test_journal_restart_keeps_same_session_after_stream_failure(self):
        from .cloud import AntigravityCloudClient
        from .journal import RunJournal
        from .test_integration import seeded
        body = '''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
if "--conversation" not in sys.argv:
    emit(dict(event="result", result=dict(status="ERROR", error="The stream was interrupted. Please continue the task you were working on.")))
else:
    result('{"done":true}')
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            runner = seeded()
            runner.journal = RunJournal(temp)
            client = AntigravityCloudClient('model',executable=executable,timeout=2,request_attempts=1)
            review = dict(layer='atomic',verification_epoch=runner._verification_epoch,task_hashes={'claim.one':'hash-one'})
            with self.assertRaises(AntigravityError) as caught:
                runner._complete(client,'atomic.target','original',review_context=review)
            self.assertEqual(caught.exception.code,'stream_interrupted')
            before = json.loads(next((Path(temp)/'sessions').glob('*/session.json')).read_text())
            runner.journal = RunJournal(temp)
            self.assertEqual(runner._complete(client,'atomic.target','original',review_context=review),{'done':True})
            states = list((Path(temp)/'sessions').glob('*/session.json'))
            self.assertEqual(len(states),1)
            after = json.loads(states[0].read_text())
            self.assertEqual(before['conversation_id'],after['conversation_id'])
            self.assertEqual(after['continuations'],0)

    def test_auto_continue_can_be_disabled_without_losing_resumable_error(self):
        from .cloud import AntigravityCloudClient
        body = '''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
emit(dict(event="result",result=dict(status="ERROR",error="The stream was interrupted. Please continue the task you were working on.")))
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable, patch('v4.cloud.sleep') as sleep:
            client=AntigravityCloudClient('model',executable=executable,session_root=temp,timeout=2,continue_pending=False)
            with self.assertRaises(AntigravityError) as caught:
                client.complete('original')
            self.assertEqual(caught.exception.code,'stream_interrupted')
            self.assertEqual(caught.exception.attempts,1)
            self.assertTrue(caught.exception.details()['resumable'])
            sleep.assert_not_called()
