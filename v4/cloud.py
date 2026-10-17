"""Existing Antigravity (agy) cloud connection only; no direct API fallback."""
import json
import math
import os
import shutil
import threading
from time import sleep
from .agy_session import AGENT_NAME, AntigravityError, run_session

_STDIN_THRESHOLD = 64 * 1024  # UTF-8 bytes: long-prompt timeout


def structured_prompt(prompt, serialized_schema):
    if serialized_schema is None:
        return prompt
    return (prompt + "\n\n回覆必須是符合下列 schema 的單一 JSON object，不要 Markdown 或重複物件。"
            "每個引用仍須由你作語義驗證，不能以預設欄位代替。\n<response-schema>\n"
            + serialized_schema + "\n</response-schema>")


def request_completion(client, prompt, response_schema=None):
    structured = getattr(client, "complete_structured", None)
    if response_schema is not None and callable(structured):
        return structured(prompt, response_schema)
    return client.complete(prompt)


class AntigravityCloudClient:
    execution = "cloud"

    def __init__(self, model, *, executable=None, timeout=1200):
        if (not isinstance(model, str) or not model or model.startswith("-")
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:/" for c in model)):
            raise ValueError("explicit Antigravity model ID required")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("positive finite Antigravity timeout required")
        command = executable or os.environ.get("AGY_EXECUTABLE", "agy")
        self.executable = shutil.which(command)
        if not self.executable:
            raise RuntimeError("Antigravity CLI unavailable; configure existing agy on PATH or AGY_EXECUTABLE")
        self.model = model
        self.identity = f"antigravity/{model}"
        self.timeout = timeout
        self.calls = []
        self._lock = threading.Lock()

    def complete_structured(self, prompt, response_schema):
        return self.complete(prompt, response_schema=response_schema)

    def check_capabilities(self):
        """Start CLI and check agent binding without sending any prompt or model turn."""
        return run_session(self.executable, self.model, None, min(self.timeout, 60))

    def complete(self, prompt, *, response_schema=None):
        schema = None if response_schema is None else json.dumps(response_schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        text, last_error = None, AntigravityError("empty_response", retryable=True)
        for attempt in range(3):
            try:
                text = self._generate(prompt, response_schema=schema)
                if text and text.strip():
                    break
                last_error = AntigravityError("empty_response", retryable=True)
            except AntigravityError as exc:
                if not exc.retryable:
                    raise AntigravityError(exc.code, exit_code=exc.exit_code, attempts=attempt + 1) from None
                last_error = exc
            except Exception:
                # Unexpected exception text may contain request/account data.
                last_error = AntigravityError("unexpected_error", retryable=True)
            text = None
            if attempt < 2:
                sleep(15)
        if text is None:
            raise AntigravityError(last_error.code, retryable=last_error.retryable,
                                   exit_code=last_error.exit_code, attempts=3) from None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            raise AntigravityError("invalid_json") from None
        with self._lock:
            self.calls.append(dict(transport="antigravity", requested_model=self.identity, served_model=None,
                                   agent_profile=AGENT_NAME, agent_policy_verified=True))
        return parsed

    def _generate(self, prompt, *, response_schema=None):
        # Every size uses stdin so init can be checked before sending evidence.
        # agy native --json-schema produced repeated responses/multiple turns
        # without structured_output in the real stdin test. Keep the contract
        # in stdin and let the per-Claim validators reject/repair omissions.
        wire_prompt = structured_prompt(prompt, response_schema)
        timeout = max(self.timeout, 1800) if len(wire_prompt.encode("utf-8")) > _STDIN_THRESHOLD else self.timeout
        return run_session(self.executable, self.model, wire_prompt, timeout, None)
