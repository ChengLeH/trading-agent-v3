"""Transport contract tests with HTTP replaced; no external model calls."""
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler
from .cloud import GeminiCloudClient, NoRedirect


class Transport(unittest.TestCase):
    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode())

    def test_environment_proxy_is_used_without_allowing_redirects(self):
        for env, expected in (({"https_proxy": "http://egress.invalid:3128"},
                               {"https": "http://egress.invalid:3128"}), ({}, {})):
            with self.subTest(env=env), patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", **env}, clear=True), \
                    patch("v4.cloud.build_opener") as opener:
                opener.return_value.open.return_value = self.response(dict(candidates=[
                    dict(finishReason="STOP", content=dict(parts=[dict(text='{"ok":true}')]))]))
                self.assertEqual(GeminiCloudClient("test").complete("test"), {"ok": True})
                handlers = opener.call_args.args
                self.assertEqual(next(h.proxies for h in handlers if isinstance(h, ProxyHandler)), expected)
                self.assertTrue(any(isinstance(h, NoRedirect) for h in handlers))

    def test_explicit_cloud_endpoint_model_and_audit(self):
        client = GeminiCloudClient("test-model")
        payload = dict(modelVersion="served-test", responseId="response", usageMetadata={"totalTokenCount": 10},
                       candidates=[dict(finishReason="STOP", content=dict(parts=[dict(text='{"ok":true}')]))])
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("v4.cloud.build_opener") as opener:
            opener.return_value.open.return_value = self.response(payload)
            self.assertEqual(client.complete("test"), {"ok": True})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent")
            self.assertNotIn("test-key", request.full_url)
            self.assertEqual(client.calls[0]["served_model"], "served-test")
            self.assertEqual(client.calls[0]["requested_model"], "google/test-model")

    def test_missing_key_never_opens_network(self):
        with patch.dict(os.environ, {}, clear=True), patch("v4.cloud.build_opener") as opener:
            with self.assertRaises(RuntimeError):
                GeminiCloudClient("test").complete("test")
            opener.assert_not_called()

    def test_truncated_or_invalid_completion_fails_closed(self):
        for payload in ({"candidates": []},
                        {"candidates": [dict(finishReason="MAX_TOKENS", content={})]},
                        {"candidates": [dict(finishReason="STOP", content=dict(parts=[dict(text="not JSON")]))]}):
            with self.subTest(payload=payload), patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                 patch("v4.cloud.build_opener") as opener:
                opener.return_value.open.return_value = self.response(payload)
                with self.assertRaises(ValueError):
                    GeminiCloudClient("test").complete("test")

    def test_http_failure_does_not_leak_credential_or_body(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("v4.cloud.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("sensitive-url", 401, "secret", {}, None)
            with self.assertRaisesRegex(RuntimeError, "^Gemini cloud HTTP 401$"):
                GeminiCloudClient("test").complete("test")

    def test_no_local_endpoint_or_redirect(self):
        for model in ("http://localhost:8080/model", "../local", ""):
            with self.assertRaises(ValueError):
                GeminiCloudClient(model)
        with self.assertRaises(TypeError):
            GeminiCloudClient("test", base_url="http://localhost")
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "http://localhost")


if __name__ == "__main__":
    unittest.main()
