"""Explicit Gemini cloud transport. No local endpoints, CLI, or fallback.

Protocol reference: https://ai.google.dev/api/generate-content
Models are caller-selected, not inferred from the host's installed tools.
"""
import json
import os
import threading
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import quote


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("cloud transport does not follow redirects")


class GeminiCloudClient:
    execution = "cloud"

    def __init__(self, model, *, key_env="GEMINI_API_KEY", timeout=120):
        if not isinstance(model, str) or not model or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in model):
            raise ValueError("explicit Gemini model ID required")
        self.model = model
        self.identity = f"google/{model}"
        self.key_env = key_env
        self.timeout = timeout
        self.calls = []
        self._lock = threading.Lock()

    def complete(self, prompt):
        key = os.environ.get(self.key_env)
        if not key:
            raise RuntimeError(f"missing cloud credential environment variable: {self.key_env}")
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + quote(self.model, safe="") + ":generateContent"
        payload = dict(contents=[dict(role="user", parts=[dict(text=prompt)])],
                       generationConfig=dict(responseMimeType="application/json", temperature=0.1))
        req = Request(url, data=json.dumps(payload).encode(), method="POST",
                      headers={"Content-Type": "application/json", "x-goog-api-key": key})
        # Honor system/egress proxies while keeping the fixed Google HTTPS
        # endpoint, TLS verification and redirect rejection. No key in URLs/logs.
        opener = build_opener(ProxyHandler(), NoRedirect())
        try:
            with opener.open(req, timeout=self.timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            exc.close()
            raise RuntimeError(f"Gemini cloud HTTP {exc.code}") from None
        except URLError:
            raise RuntimeError("Gemini cloud connection failed") from None
        candidates = data.get("candidates", [])
        if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
            raise ValueError("cloud returned blocked, incomplete, or ambiguous completion")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
        result = json.loads(text)  # no prose/regex extraction or repair guessing
        with self._lock:
            self.calls.append(dict(requested_model=self.identity, served_model=data.get("modelVersion"),
                                   response_id=data.get("responseId"), usage=data.get("usageMetadata", {})))
        return result
