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
