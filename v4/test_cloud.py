"""Cloud transport tests use deterministic doubles or real local stub processes."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch
from .cloud import AntigravityCloudClient, _STDIN_THRESHOLD
from .agy_session import AGENT_NAME, AntigravityError, run_session
from .semantic_contracts import SEMANTIC_REVIEW_SCHEMA


@contextlib.contextmanager
def cli_stub(body):
    with tempfile.TemporaryDirectory() as directory:
        executable = Path(directory) / "agy-stub"
        executable.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys, time
from pathlib import Path
def emit(value):
    print(json.dumps(value), flush=True)
def init(tools=None, agent="v4-evidence-only", cwd=None, model=None):
    emit(dict(event="init", init=dict(agent=agent, tools=[] if tools is None else tools,
                                     cwd=str(Path.cwd()) if cwd is None else cwd,
                                     model=sys.argv[sys.argv.index("--model") + 1] if model is None else model)))
def result(response="{}", status="SUCCESS"):
    emit(dict(event="result", result=dict(status=status, response=response)))
''' + body, encoding="utf-8")
        executable.chmod(0o700)
        yield str(executable)


class Transport(unittest.TestCase):
    def setUp(self):
        sleeper = patch("v4.cloud.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_semantic_schema_is_forwarded_for_every_prompt_size(self):
        for prompt in ("short", "a" * (_STDIN_THRESHOLD + 1)):
            with self.subTest(size=len(prompt)), patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                 patch("v4.cloud.run_session", return_value="{}") as run:
                self.assertEqual(AntigravityCloudClient("model").complete_structured(prompt, SEMANTIC_REVIEW_SCHEMA), {})
                schema = json.loads(run.call_args.args[4])
                self.assertEqual(schema, SEMANTIC_REVIEW_SCHEMA)
                self.assertIn("reference_checks", schema["required"])

    def test_oversize_schema_blocks_before_cli_or_retries(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), patch("v4.cloud.run_session") as run:
            with self.assertRaisesRegex(ValueError, "schema exceeds"):
                AntigravityCloudClient("model").complete_structured("prompt", dict(description="a" * _STDIN_THRESHOLD))
            run.assert_not_called()
            self.sleep.assert_not_called()

    def test_empty_output_retries_and_recovers(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
             patch("v4.cloud.run_session", side_effect=["", " \n\t", '{"ok":true}']) as run:
            self.assertEqual(AntigravityCloudClient("model").complete("same prompt"), dict(ok=True))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(self.sleep.call_args_list, [call(15), call(15)])

    def test_repeated_empty_output_is_bounded_and_blocks(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
             patch("v4.cloud.run_session", return_value="") as run:
            client = AntigravityCloudClient("model")
            with self.assertRaises(AntigravityError) as caught:
                client.complete("secret prompt")
            self.assertEqual(caught.exception.code, "empty_response")
            self.assertEqual(caught.exception.attempts, 3)
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(client.calls, [])
            self.assertEqual(self.sleep.call_count, 2)

    def test_nonempty_invalid_json_does_not_retry(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
             patch("v4.cloud.run_session", return_value="not JSON") as run:
            with self.assertRaisesRegex(AntigravityError, "invalid_json") as caught:
                AntigravityCloudClient("model").complete("prompt")
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(run.call_count, 1)
            self.sleep.assert_not_called()

    def test_utf8_threshold_changes_timeout_without_using_argv_prompts(self):
        for prompt, timeout in (("a" * _STDIN_THRESHOLD, 60), ("語" * (_STDIN_THRESHOLD // 3 + 1), 1800)):
            with self.subTest(size=len(prompt)), patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                 patch("v4.cloud.run_session", return_value="{}") as run:
                AntigravityCloudClient("model", timeout=60).complete(prompt)
                self.assertEqual(run.call_args.args[2:4], (prompt, timeout))

    def test_transient_error_retry_retains_safe_failure_reason(self):
        for error in (AntigravityError("timeout", retryable=True),
                      AntigravityError("cli_exit", retryable=True, exit_code=3),
                      AntigravityError("cli_start_failed", retryable=True)):
            with self.subTest(code=error.code), patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                 patch("v4.cloud.run_session", side_effect=error) as run:
                with self.assertRaises(AntigravityError) as caught:
                    AntigravityCloudClient("model").complete("secret prompt")
                self.assertEqual(caught.exception.code, error.code)
                self.assertEqual(caught.exception.exit_code, error.exit_code)
                self.assertEqual(caught.exception.attempts, 3)
                self.assertEqual(run.call_count, 3)
                self.assertNotIn("secret", str(caught.exception))
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
             patch("v4.cloud.run_session", side_effect=[AntigravityError("timeout", retryable=True), '{"ok":true}']):
            self.assertEqual(AntigravityCloudClient("model").complete("prompt"), dict(ok=True))

    def test_policy_failure_never_retries_or_switches_transport(self):
        for code in ("permission_denied", "tool_policy_unverified", "unexpected_tool_use", "missing_init"):
            with self.subTest(code=code), patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                 patch("v4.cloud.run_session", side_effect=AntigravityError(code)) as run:
                self.sleep.reset_mock()
                with self.assertRaises(AntigravityError) as caught:
                    AntigravityCloudClient("model").complete("secret prompt")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(run.call_count, 1)
                self.sleep.assert_not_called()

    def test_unknown_exception_does_not_expose_diagnostics(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
             patch("v4.cloud.run_session", side_effect=RuntimeError("secret account data")):
            with self.assertRaises(AntigravityError) as caught:
                AntigravityCloudClient("model").complete("secret prompt")
            self.assertEqual(caught.exception.code, "unexpected_error")
            self.assertNotIn("secret", str(caught.exception))

    def test_agy_without_api_key_inherits_environment(self):
        with patch.dict(os.environ, {"https_proxy": "http://egress.invalid:3128"}, clear=True), \
             patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
             patch("v4.cloud.run_session", return_value='{"ok":true}') as run:
            client = AntigravityCloudClient("selected-model")
            self.assertEqual(client.complete("claim context"), dict(ok=True))
            self.assertEqual(run.call_args.args[:2], ("/existing/bin/agy", "selected-model"))
            self.assertEqual(client.calls, [dict(transport="antigravity", requested_model="antigravity/selected-model",
                                                served_model=None, agent=AGENT_NAME, tools_verified=True)])

    def test_missing_cli_never_falls_back(self):
        with patch("v4.cloud.shutil.which", return_value=None), patch("v4.cloud.run_session") as run:
            with self.assertRaisesRegex(RuntimeError, "Antigravity CLI unavailable"):
                AntigravityCloudClient("model")
            run.assert_not_called()

    def test_explicit_model_and_executable_selection(self):
        for model in ("", "--other-option", "model with spaces", "$(command)"):
            with self.assertRaises(ValueError):
                AntigravityCloudClient(model)
        with patch.dict(os.environ, {"AGY_EXECUTABLE": "/chosen/agy"}), \
             patch("v4.cloud.shutil.which", return_value="/chosen/agy") as which:
            AntigravityCloudClient("selected-model")
            which.assert_called_once_with("/chosen/agy")


class ToolFreeSession(unittest.TestCase):
    def test_capability_check_closes_stdin_without_sending_a_user_turn(self):
        with cli_stub('init()\nassert sys.stdin.read() == ""\n') as executable, \
             patch("v4.agy_session.os.write", wraps=os.write) as write:
            result = AntigravityCloudClient("model", executable=executable).check_capabilities()
            self.assertEqual(result, dict(agent=AGENT_NAME, tools=[], tools_verified=True, user_prompt_sent=False))
            write.assert_not_called()

    def test_179kb_prompt_and_schema_reach_stdin_after_preflight(self):
        prompt = "資料\n" * 27000
        body = '''
import hashlib, select
assert not select.select([sys.stdin], [], [], 0.05)[0], "prompt arrived before init"
profile = Path(".agents/agents/v4-evidence-only/agent.md").read_text()
for setting in ("tools: []", "excludeDefaultComponents: true", "inheritCustomizations: false"):
    assert setting in profile
init()
request = json.loads(sys.stdin.readline())
assert sys.stdin.read() == ""
data = request["message"]["content"].encode("utf-8")
result(json.dumps(dict(size=len(data), sha256=hashlib.sha256(data).hexdigest(), argv=sys.argv,
                       schema=json.loads(sys.argv[sys.argv.index("--json-schema") + 1]),
                       proxy=os.environ.get("https_proxy"))))
'''
        with cli_stub(body) as executable, patch.dict(os.environ, {"https_proxy": "http://egress.invalid:3128"}):
            raw = run_session(executable, "test-model", prompt, 5, json.dumps(SEMANTIC_REVIEW_SCHEMA))
        result = json.loads(raw)
        self.assertEqual(result["size"], len(prompt.encode("utf-8")))
        self.assertEqual(result["sha256"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        self.assertNotIn(prompt, result["argv"])
        self.assertNotIn("-p", result["argv"])
        self.assertNotIn("--mode", result["argv"])
        self.assertNotIn("--dangerously-skip-permissions", result["argv"])
        self.assertIn("--sandbox", result["argv"])
        self.assertIn("--disable-slash-commands", result["argv"])
        self.assertEqual(result["schema"], SEMANTIC_REVIEW_SCHEMA)
        self.assertEqual(result["proxy"], "http://egress.invalid:3128")

    def test_tools_or_wrong_agent_or_workspace_never_receive_prompt_bytes(self):
        for init_code in ('init(["run_command"])', 'init(agent="other")', 'init(cwd="/elsewhere")',
                          'emit(dict(event="init", init=dict(agent="v4-evidence-only", cwd=str(Path.cwd()))))'):
            with self.subTest(init_code=init_code), cli_stub(init_code + '\ntime.sleep(10)\n') as executable, \
                 patch("v4.agy_session.os.write", wraps=os.write) as write:
                with self.assertRaises(AntigravityError) as caught:
                    run_session(executable, "model", "secret evidence", 2)
                self.assertFalse(caught.exception.retryable)
                write.assert_not_called()

    def test_missing_partial_or_malformed_init_is_bounded_without_sending_prompt(self):
        bodies = ('sys.stdout.write("{partial"); sys.stdout.flush(); time.sleep(10)',
                  'time.sleep(10)', 'emit(dict(event="result", result=dict(status="SUCCESS", response="{}")))')
        for body in bodies:
            with self.subTest(body=body), cli_stub(body) as executable, \
                 patch("v4.agy_session.os.write", wraps=os.write) as write:
                with self.assertRaises(AntigravityError) as caught:
                    run_session(executable, "model", "secret evidence", 0.15)
                self.assertFalse(caught.exception.retryable)
                write.assert_not_called()

    def test_missing_or_wrong_model_blocks_before_sending_prompt(self):
        for init_code in ('init(model="wrong-model")',
                          'emit(dict(event="init", init=dict(agent="v4-evidence-only", tools=[], cwd=str(Path.cwd()))))'):
            with self.subTest(init_code=init_code), cli_stub(init_code + '\ntime.sleep(10)\n') as executable, \
                 patch("v4.agy_session.os.write", wraps=os.write) as write:
                with self.assertRaises(AntigravityError) as caught:
                    run_session(executable, "model", "evidence", 2)
                self.assertEqual(caught.exception.code, "model_unverified")
                write.assert_not_called()

    def test_unknown_or_missing_step_type_is_rejected(self):
        for step in ({}, dict(step_type="unknown"), dict(step_type=[])):
            with self.subTest(step=step), cli_stub('init()\nsys.stdin.readline()\nemit(' + repr(dict(event="step_update", step_update=step)) + ')\nresult()\n') as executable:
                with self.assertRaises(AntigravityError) as caught:
                    run_session(executable, "model", "evidence", 2)
                self.assertEqual(caught.exception.code, "invalid_stream")

    def test_headless_permission_denial_is_explicit_and_never_retried(self):
        body = '''
sys.stderr.write('warning: mode ignored. jetski: a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied. secret account data\\n')
sys.stderr.flush()
'''
        for diagnostic in (body, body.replace('sys.stderr.flush()', 'sys.stderr.write("x" * 20000); sys.stderr.flush()')):
            with cli_stub(diagnostic) as executable, patch("v4.cloud.sleep") as sleep:
                with self.assertRaises(AntigravityError) as caught:
                    AntigravityCloudClient("model", executable=executable).complete("secret prompt")
                self.assertEqual(caught.exception.code, "permission_denied")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.attempts, 1)
                self.assertNotIn("secret", str(caught.exception))
                sleep.assert_not_called()

    def test_tool_or_subagent_event_aborts_even_with_successful_looking_result(self):
        for step in (dict(step_type="tool", tool_name="run_command"),
                     dict(step_type="agent_response", tool_info={}), dict(subagent_info={})):
            with self.subTest(step=step), cli_stub('init()\nsys.stdin.readline()\nemit(' + repr(dict(event="step_update", step_update=step)) + ')\nresult()\n') as executable:
                with self.assertRaises(AntigravityError) as caught:
                    run_session(executable, "model", "evidence", 2)
                self.assertEqual(caught.exception.code, "unexpected_tool_use")
                self.assertFalse(caught.exception.retryable)

    def test_failed_duplicate_missing_or_invalid_result_never_passes(self):
        for ending in ('result(status="ERROR")', 'result(); result()', 'pass', 'result(response=None)', 'print("not-json", flush=True)'):
            with self.subTest(ending=ending), cli_stub('init()\nsys.stdin.readline()\n' + ending + '\n') as executable:
                with self.assertRaises(AntigravityError):
                    run_session(executable, "model", "evidence", 2)

    def test_timeout_and_cli_exit_preserve_safe_categories(self):
        for ending, code in (('time.sleep(10)', "timeout"), ('sys.exit(7)', "cli_exit")):
            with self.subTest(ending=ending), cli_stub('init()\nsys.stdin.readline()\n' + ending + '\n') as executable:
                with self.assertRaises(AntigravityError) as caught:
                    run_session(executable, "model", "evidence", 2)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.retryable)
                if code == "cli_exit":
                    self.assertEqual(caught.exception.exit_code, 7)

    def test_start_failure_is_explicit(self):
        with self.assertRaises(AntigravityError) as caught:
            run_session("/nonexistent/agy", "model", "evidence", 1)
        self.assertEqual(caught.exception.code, "cli_start_failed")
        self.assertTrue(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
