"""Transport formatting tolerance never fabricates semantic review content."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .cloud import AntigravityCloudClient
from .agy_session import run_session, AntigravityError, abandon_session
from .test_cloud import cli_stub


class ResponseJson(unittest.TestCase):
    def test_single_complete_json_fence_needs_no_paid_correction(self):
        for opening in ('```json', '```JSON', '```'):
            raw = opening + '\n{"ok":true,"reference_checks":[]}\n```'
            body = 'init()\nsys.stdin.readline()\nresult(' + repr(raw) + ')'
            with self.subTest(opening=opening), tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
                client = AntigravityCloudClient('model', executable=executable, timeout=3, session_root=temp)
                self.assertEqual(client.complete('original evidence'), dict(ok=True, reference_checks=[]))
                state = json.loads(next(Path(temp).glob('*/session.json')).read_text())
                self.assertEqual(state['continuations'], 0)
                self.assertEqual(json.loads(state['response']), dict(ok=True, reference_checks=[]))
                rows = [json.loads(line) for line in next(Path(temp).glob('*/events.jsonl')).read_text().splitlines()]
                self.assertEqual([r['result']['response'] for r in rows if r['event']=='result'], [raw])

    def test_prose_fragments_multiple_objects_and_bad_inner_json_are_not_extracted(self):
        from .response_json import normalize_json_response
        for text in ('explanation\n```json\n{}\n```', '```json\n{}\n```\nextra',
                     '```json\n{}\n```\n```json\n{}\n```', '```json\n{} {}\n```',
                     '```json\n{"ok":\n```', '```python\n{}\n```'):
            with self.subTest(text=text), self.assertRaises(ValueError): normalize_json_response(text)
        self.assertEqual(json.loads(normalize_json_response('```json\n[1,2]\n```')), [1,2])

    def test_old_fenced_success_recovers_even_after_budget_exhaustion_without_model(self):
        raw = '```json\n{"ok":true}\n```'
        body = 'init()\nsys.stdin.readline()\nresult(' + repr(raw) + ')'
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            self.assertEqual(run_session(executable, 'model', 'request', 3, session_dir=temp), raw)
            path = Path(temp)/'session.json'; state = json.loads(path.read_text())
            state.update(status='paused', last_error='invalid_json', continuations=2,
                         max_continuations=2, elapsed_seconds=10, budget_seconds=10, json_required=True)
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, 'complete result already saved'):
                abandon_session(temp, reason='must recover existing valid result instead')
            with patch('v4.agy_session.subprocess.Popen') as start:
                recovered = run_session(executable, 'model', 'request', 3, session_dir=temp, resume=True, require_json=True)
            start.assert_not_called()
            self.assertEqual(json.loads(recovered), dict(ok=True))
            self.assertEqual(json.loads(path.read_text())['continuations'], 2)

    def test_valid_json_in_partial_delta_without_terminal_is_not_accepted(self):
        body = r'''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
emit(dict(event="step_update", step_update=dict(step_type="agent_response", text_delta='{"ok":true}')))
time.sleep(10)
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            with self.assertRaisesRegex(AntigravityError, 'session_pending'):
                run_session(executable, 'model', 'request', 1, session_dir=temp, require_json=True)
            self.assertNotEqual(json.loads((Path(temp)/'session.json').read_text())['status'], 'complete')

    def test_continuation_requires_full_object_not_remaining_fragment(self):
        body = r'''
init()
message=json.loads(sys.stdin.readline())["message"]["content"]
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
if "--conversation" not in sys.argv:
    emit(dict(event="step_update", step_update=dict(step_type="agent_response", text_delta='{"first":1,')))
    time.sleep(10)
else:
    assert "重新完整輸出" in message
    assert "不要重複輸出先前物件" not in message
    result('{"first":1,"second":2}')
'''
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable:
            with self.assertRaises(AntigravityError): run_session(executable, 'model', 'request', 2, session_dir=temp)
            response = run_session(executable, 'model', 'request', 2, session_dir=temp, resume=True, require_json=True)
            self.assertEqual(json.loads(response), dict(first=1, second=2))

    def test_tolerated_wrapper_does_not_fill_missing_semantic_verification_fields(self):
        from .response_json import normalize_json_response
        from .atomic_phase6 import normalize_atomic_result
        text = '```json\n{"claim_id":"c","verdict":"PASS","findings":[]}\n```'
        value = json.loads(normalize_json_response(text))
        self.assertNotIn('reference_checks', value)
        with self.assertRaises(ValueError):
            normalize_atomic_result('c', value, expected_ref_ids=['source.required'])

class InterruptedTerminalResponse(unittest.TestCase):
    ERROR = 'The stream was interrupted. Please continue the task you were working on.'

    def test_interruption_format_variants_recover_live_and_saved_answers(self):
        for error in ('  THE stream was interrupted! Please continue.  ',
                      'The stream\nwas interrupted. Please continue the task you were working on!'):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as root, cli_stub(self.body('{"done":true}', error=error)) as exe:
                self.assertEqual(json.loads(run_session(exe, 'model', 'request', 3,
                    session_dir=root, require_json=True)), {'done': True})
                path = Path(root) / 'session.json'
                state = json.loads(path.read_text())
                state.update(status='paused', last_error='continuation_exhausted', continuations=2, max_continuations=2)
                path.write_text(json.dumps(state))
                with patch('v4.agy_session.subprocess.Popen') as start:
                    self.assertEqual(json.loads(run_session(exe, 'model', 'request', 3,
                        session_dir=root, resume=True, require_json=True)), {'done': True})
                start.assert_not_called()

    def test_interruption_code_precedes_text_but_unknown_errors_are_not_success(self):
        from .agy_protocol import interrupted_json_response
        self.assertEqual(interrupted_json_response(dict(status='ERROR', error_code='stream_interrupted',
            error='provider changed wording', response='{}')), '{}')
        for code, error in [('permission_denied', self.ERROR), ('new_error', self.ERROR),
                            (None, 'Not the stream was interrupted.'), (None, 'Quoted: ' + self.ERROR)]:
            self.assertIsNone(interrupted_json_response(dict(status='ERROR',error_code=code,error=error,response='{}')))

    def body(self, response, *, status='ERROR', error=None, suffix=''):
        terminal = dict(status=status, error=self.ERROR if error is None else error, response=response, error_code=None)
        return 'init()\nsys.stdin.readline()\nemit(dict(event="step_update",step_update=dict(step_type="user_input",state="DONE")))\nemit(' + repr(dict(event='result', result=terminal)) + ')\n' + suffix

    def test_complete_interrupted_answers_reach_receipts_and_keep_fail_verdicts(self):
        from .test_integration import seeded, verdict, reference_ids
        for status in ('PASS', 'FAIL', 'SUSPICIOUS'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                runner = seeded()
                prepared = runner.prepare_phase6_atomic()
                answer = verdict('a', status)
                raw = '```json\n' + json.dumps(answer) + '\n```'
                with cli_stub(self.body(raw)) as executable:
                    client = AntigravityCloudClient('model', executable=executable, session_root=temp, timeout=3, request_attempts=1)
                    received = client.complete('original evidence')
                index = {c['claim_id']:c for c in runner.run.claims}
                results = {cid:verdict(cid,ref_ids=reference_ids(index[cid])) for cid,_ in prepared['atomic_prompts']}
                results['a'] = received
                summary = runner.collect_phase6_atomic(results, model_identity=client.identity)
                self.assertEqual(runner.receipts['a']['result']['verdict'], status)
                self.assertEqual(summary['needs_deep_review'], status != 'PASS')
                state = json.loads(next(Path(temp).glob('*/session.json')).read_text())
                self.assertEqual(state['transport_anomaly'], 'stream_interrupted')
                self.assertEqual(state['continuations'], 0)
                rows = [json.loads(line) for line in next(Path(temp).glob('*/events.jsonl')).read_text().splitlines()]
                self.assertEqual(rows[-1]['result']['status'], 'ERROR')
                self.assertEqual(rows[-1]['result']['response'], raw)

    def test_saved_interrupted_answer_recovers_before_exhaustion_without_new_call(self):
        raw = '{"claim_id":"a","verdict":"FAIL","findings":[{"issue":"wrong attribution"}],"reference_checks":[]}'
        with tempfile.TemporaryDirectory() as temp, cli_stub(self.body(raw)) as executable:
            try:
                run_session(executable,'model','request',3,session_dir=temp,require_json=True)
            except AntigravityError:
                pass  # simulate a pre-fix reader that left the terminal in its audit
            path = Path(temp)/'session.json'
            state = json.loads(path.read_text())
            state.update(status='paused',last_error='stream_interrupted',continuations=2,max_continuations=2,
                         elapsed_seconds=10,budget_seconds=10)
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError,'complete result already saved'):
                abandon_session(temp,reason='recover the answer instead')
            with patch('v4.agy_session.subprocess.Popen') as start:
                recovered=run_session(executable,'model','request',3,session_dir=temp,resume=True,require_json=True)
            start.assert_not_called()
            self.assertEqual(json.loads(recovered)['verdict'],'FAIL')
            after=json.loads(path.read_text())
            self.assertEqual(after['continuations'],2)
            self.assertEqual(after['elapsed_seconds'],10)
            self.assertEqual(after['transport_anomaly'],'stream_interrupted')

    def test_only_complete_json_in_the_known_interrupted_terminal_is_recovered(self):
        cases = [(None,self.ERROR),('',self.ERROR),('{"a":',self.ERROR),('explanation {}',self.ERROR),
                 ('{} {}',self.ERROR),('{}','unknown error')]
        for response,error in cases:
            with self.subTest(response=response,error=error), tempfile.TemporaryDirectory() as temp, cli_stub(self.body(response,error=error)) as executable:
                with self.assertRaises(AntigravityError):
                    run_session(executable,'model','request',3,session_dir=temp,require_json=True)
                state=json.loads((Path(temp)/'session.json').read_text())
                self.assertNotEqual(state['status'],'complete')
                self.assertEqual(state['last_error'],'stream_interrupted' if error==self.ERROR else 'result_failed')

    def test_recovered_json_still_requires_reference_checks(self):
        from .atomic_phase6 import normalize_atomic_result
        raw='{"claim_id":"a","verdict":"PASS","findings":[]}'
        with tempfile.TemporaryDirectory() as temp, cli_stub(self.body(raw)) as executable:
            client=AntigravityCloudClient('model',executable=executable,session_root=temp,timeout=3,request_attempts=1)
            response=client.complete('original evidence')
            with self.assertRaises(ValueError):
                normalize_atomic_result('a',response,expected_ref_ids=['source.required'])

    def test_second_terminal_still_rejects_an_otherwise_recoverable_answer(self):
        with tempfile.TemporaryDirectory() as temp, cli_stub(self.body('{}',suffix='result("{}")\n')) as executable:
            with self.assertRaises(AntigravityError) as caught:
                run_session(executable,'model','request',3,session_dir=temp,require_json=True)
            self.assertEqual(caught.exception.code,'invalid_stream')
            self.assertNotEqual(json.loads((Path(temp)/'session.json').read_text())['status'],'complete')

    def test_nonzero_cli_exit_keeps_explicit_failure_and_protects_saved_answer(self):
        body = self.body('{}',suffix='sys.exit(7)\n')
        import subprocess
        start = subprocess.Popen
        def completed_exit_snapshot(*args, **kwargs):
            process = start(*args, **kwargs)
            # Exercise the known-exit guard, independently of OS reap timing.
            process.poll = lambda: process.wait(timeout=3)
            return process
        with tempfile.TemporaryDirectory() as temp, cli_stub(body) as executable, \
                patch('v4.agy_session.subprocess.Popen',side_effect=completed_exit_snapshot):
            with self.assertRaises(AntigravityError):
                run_session(executable,'model','request',3,session_dir=temp,require_json=True)
            state=json.loads((Path(temp)/'session.json').read_text())
            self.assertEqual(state['last_error'],'cli_exit')
            self.assertNotEqual(state['status'],'complete')
            with self.assertRaisesRegex(ValueError,'complete result already saved'):
                abandon_session(temp,reason='do not discard the saved answer')
