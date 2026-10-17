"""Antigravity transport tests replace subprocesses, never call real models."""
import os
import subprocess
import unittest
from unittest.mock import patch
from .cloud import AntigravityCloudClient


class Transport(unittest.TestCase):
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
            self.assertEqual(run.call_count, 1)
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
