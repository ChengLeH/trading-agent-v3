"""Antigravity transport tests replace subprocesses, never call real models."""
import os
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch
from .cloud import AntigravityCloudClient, _STDIN_THRESHOLD
from .semantic_contracts import SEMANTIC_REVIEW_SCHEMA


def stream_response(text, status="SUCCESS"):
    return json.dumps(dict(event="result", result=dict(status=status, response=text))) + "\n"


class Transport(unittest.TestCase):
    def setUp(self):
        sleeper = patch("v4.cloud.time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_semantic_schema_is_sent_to_cli_for_both_transport_paths(self):
        for prompt in ("short", "a" * (_STDIN_THRESHOLD + 1)):
            with self.subTest(size=len(prompt)), patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                    patch("v4.cloud.subprocess.run") as run:
                output = "{}" if len(prompt) <= _STDIN_THRESHOLD else stream_response("{}")
                run.return_value = subprocess.CompletedProcess([], 0, output, "")
                client = AntigravityCloudClient("model")
                self.assertEqual(client.complete_structured(prompt, SEMANTIC_REVIEW_SCHEMA), {})
                args = run.call_args.args[0]
                schema_text = args[args.index("--json-schema") + 1]
                schema = json.loads(schema_text)
                self.assertIn("reference_checks", schema["required"])
                self.assertEqual(schema, SEMANTIC_REVIEW_SCHEMA)
                self.assertLess(len(schema_text.encode("utf-8")), _STDIN_THRESHOLD)

    def test_oversize_schema_blocks_before_cli_or_retries(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), patch("v4.cloud.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "schema exceeds"):
                AntigravityCloudClient("model").complete_structured("prompt", dict(description="a" * _STDIN_THRESHOLD))
            run.assert_not_called()
            self.sleep.assert_not_called()

    def test_179kb_prompt_reaches_child_stdin_without_entering_argv(self):
        prompt = "資料\n" * 27000
        self.assertGreater(len(prompt.encode("utf-8")), 179 * 1024)
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "agy-stub"
            executable.write_text("#!" + sys.executable + "\n" +
                                  "import sys, json, hashlib\n" +
                                  "request = json.loads(sys.stdin.read())\n" +
                                  "data = request['message']['content'].encode('utf-8')\n" +
                                  "payload = json.dumps(dict(size=len(data), sha256=hashlib.sha256(data).hexdigest(), "
                                  "longest_arg=max(map(len, sys.argv))))\n" +
                                  "print(json.dumps(dict(event='init')))\n" +
                                  "print(json.dumps(dict(event='step_update')))\n" +
                                  "print(json.dumps(dict(event='result', result=dict(status='SUCCESS', response=payload))))\n")
            executable.chmod(0o700)
            result = AntigravityCloudClient("test-model", executable=str(executable)).complete(prompt)
            self.assertEqual(result["size"], len(prompt.encode("utf-8")))
            self.assertEqual(result["sha256"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())
            self.assertLess(result["longest_arg"], 1024)

    def test_empty_output_retries_and_recovers(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                patch("v4.cloud.subprocess.run", side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, " \n\t", ""),
                    subprocess.CompletedProcess([], 0, '{"ok":true}', "")]) as run:
            client = AntigravityCloudClient("model")
            self.assertEqual(client.complete("same prompt"), dict(ok=True))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(self.sleep.call_args_list, [call(15), call(15)])

    def test_repeated_empty_output_is_bounded_and_blocks(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                patch("v4.cloud.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            client = AntigravityCloudClient("model")
            with self.assertRaisesRegex(RuntimeError, "after 3 tries.*empty response") as caught:
                client.complete("secret prompt")
            self.assertNotIn("secret prompt", str(caught.exception))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(client.calls, [])
            self.assertEqual(self.sleep.call_count, 2)

    def test_nonempty_invalid_json_does_not_retry(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                patch("v4.cloud.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "not JSON", "")) as run:
            with self.assertRaisesRegex(ValueError, "strict JSON"):
                AntigravityCloudClient("model").complete("prompt")
            self.assertEqual(run.call_count, 1)
            self.sleep.assert_not_called()

    def test_utf8_threshold_stdin_flags_and_timeout(self):
        prompts = ("a" * _STDIN_THRESHOLD, "語" * (_STDIN_THRESHOLD // 3 + 1))
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                patch("v4.cloud.subprocess.run") as run:
            for prompt in prompts:
                large = len(prompt.encode("utf-8")) > _STDIN_THRESHOLD
                run.return_value = subprocess.CompletedProcess([], 0, stream_response("{}") if large else "{}", "")
                self.assertEqual(AntigravityCloudClient("model", timeout=60).complete(prompt), {})
                args, kwargs = run.call_args.args[0], run.call_args.kwargs
                if large:
                    self.assertNotIn("-p", args)
                    self.assertNotIn(prompt, args)
                    self.assertIn("--input-format", args)
                    self.assertEqual(json.loads(kwargs["input"])["message"]["content"], prompt)
                    self.assertEqual(kwargs["timeout"], 1800)
                    self.assertEqual(args[args.index("--print-timeout") + 1], "1800s")
                else:
                    self.assertEqual(args[-2:], ["-p", prompt])
                    self.assertEqual(kwargs["timeout"], 60)
                self.assertIn("--sandbox", args)
                self.assertIn("--disable-slash-commands", args)
                self.assertNotIn("--dangerously-skip-permissions", args)

    def test_empty_stream_response_and_transient_error_retry(self):
        prompt = "a" * (_STDIN_THRESHOLD + 1)
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                patch("v4.cloud.subprocess.run", side_effect=[
                    subprocess.TimeoutExpired(["secret"], 1),
                    subprocess.CompletedProcess([], 0, stream_response(""), ""),
                    subprocess.CompletedProcess([], 0, stream_response('{"ok":true}'), "")]) as run:
            self.assertEqual(AntigravityCloudClient("model").complete(prompt), dict(ok=True))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(self.sleep.call_count, 2)
            self.assertEqual({json.loads(call.kwargs["input"])["message"]["content"] for call in run.call_args_list}, {prompt})

    def test_missing_duplicate_or_failed_stream_result_never_passes(self):
        for output in (json.dumps(dict(event="init")), stream_response("{}") * 2,
                       stream_response("{}", "ERROR"), stream_response(None)):
            with self.subTest(output=output), patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                    patch("v4.cloud.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
                client = AntigravityCloudClient("model")
                with self.assertRaisesRegex(RuntimeError, "after 3 tries"):
                    client.complete("a" * (_STDIN_THRESHOLD + 1))
                self.assertEqual(run.call_count, 3)
                self.assertEqual(client.calls, [])

    def test_agy_without_api_key_inherits_environment(self):
        with patch.dict(os.environ, {"https_proxy": "http://egress.invalid:3128"}, clear=True), \
                patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                patch("v4.cloud.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"ok":true}', "")
            client = AntigravityCloudClient("selected-model")
            self.assertEqual(client.complete("claim context"), dict(ok=True))
            args = run.call_args.args[0]
            self.assertEqual(args[:3], ["/existing/bin/agy", "--model", "selected-model"])
            self.assertEqual(args[-2:], ["-p", "claim context"])
            self.assertIn("--disable-slash-commands", args)
            self.assertIn("--sandbox", args)
            self.assertNotIn("--dangerously-skip-permissions", args)
            self.assertNotIn("env", run.call_args.kwargs)
            self.assertFalse(run.call_args.kwargs.get("shell", False))
            self.assertEqual(client.calls, [dict(transport="antigravity", requested_model="antigravity/selected-model", served_model=None)])

    def test_missing_cli_never_falls_back(self):
        with patch("v4.cloud.shutil.which", return_value=None), patch("v4.cloud.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "Antigravity CLI unavailable"):
                AntigravityCloudClient("model")
            run.assert_not_called()

    def test_cli_failure_never_exposes_output_or_switches_transport(self):
        with patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                patch("v4.cloud.subprocess.run", return_value=subprocess.CompletedProcess([], 3, "secret", "secret")) as run:
            client = AntigravityCloudClient("model")
            with self.assertRaisesRegex(RuntimeError, "no API fallback") as caught:
                client.complete("test")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(client.calls, [])

    def test_timeout_start_failure_and_non_json_fail_closed(self):
        for failure in (subprocess.TimeoutExpired(["secret"], 1), OSError("secret"), None):
            with self.subTest(failure=type(failure).__name__), \
                    patch("v4.cloud.shutil.which", return_value="/existing/bin/agy"), \
                    patch("v4.cloud.subprocess.run", side_effect=failure) as run:
                run.return_value = subprocess.CompletedProcess([], 0, "prose ```json {} ```", "")
                with self.assertRaises((RuntimeError, ValueError)) as caught:
                    AntigravityCloudClient("model").complete("secret")
                self.assertNotIn("secret", str(caught.exception))

    def test_explicit_model_and_executable_selection(self):
        for model in ("", "--other-option", "model with spaces", "$(command)"):
            with self.assertRaises(ValueError):
                AntigravityCloudClient(model)
        with patch.dict(os.environ, {"AGY_EXECUTABLE": "/chosen/agy"}), \
                patch("v4.cloud.shutil.which", return_value="/chosen/agy") as which:
            AntigravityCloudClient("selected-model")
            which.assert_called_once_with("/chosen/agy")


if __name__ == "__main__":
    unittest.main()
