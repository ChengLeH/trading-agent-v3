"""Existing Antigravity (agy) cloud connection only; no direct API fallback."""
import hashlib
from pathlib import Path
import json
import math
import os
import shutil
import threading
from time import sleep
from .agy_session import AGENT_NAME, AntigravityError, run_session
from .session_replay import lifecycle_lock
from .session_state import SessionState
from .agy_session import load_agent_profile

_STDIN_THRESHOLD = 64 * 1024  # UTF-8 bytes: long-prompt timeout


def structured_prompt(prompt, serialized_schema):
    if serialized_schema is None:
        return prompt
    footer = ("\n\n回覆必須是符合下列 schema 的單一 JSON object，不要 Markdown 或重複物件。"
            "每個引用仍須由你作語義驗證，不能以預設欄位代替。\n<response-schema>\n"
            + serialized_schema + "\n</response-schema>")
    # Exact serialized schema footer deduplication only; no prose semantics.
    return prompt if prompt.endswith(footer) else prompt + footer


def request_completion(client, prompt, response_schema=None, *, session_dir=None, resume=False):
    durable = getattr(client, "complete_in_session", None)
    if session_dir is not None and callable(durable):
        return durable(prompt, response_schema, session_dir=session_dir, resume=resume)
    structured = getattr(client, "complete_structured", None)
    if response_schema is not None and callable(structured):
        return structured(prompt, response_schema)
    schema = None if response_schema is None else json.dumps(response_schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return client.complete(structured_prompt(prompt, schema))


class AntigravityCloudClient:
    execution = "cloud"

    def __init__(self, model, *, executable=None, timeout=1200, session_root=None):
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
        self.session_root = Path(session_root or os.environ.get("V4_SESSION_DIR", Path.cwd() / ".v4-sessions"))
        self._request_context = threading.local()
        self.calls = []
        self._lock = threading.Lock()

    def prepare_session(self, prompt, response_schema=None, *, session_dir):
        """Persist an unsent request identity before a journal in-flight record."""
        schema = None if response_schema is None else json.dumps(response_schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        wire = structured_prompt(prompt, schema)
        store = SessionState(session_dir, self.model, wire, load_agent_profile()[1])
        with store.lock():
            if not store.data.get("json_required"):
                store.save(json_required=True)

    def complete_in_session(self, prompt, response_schema=None, *, session_dir, resume=False):
        old = getattr(self._request_context, "value", None)
        self._request_context.value = (Path(session_dir), resume)
        try:
            return self.complete(prompt, response_schema=response_schema)
        finally:
            self._request_context.value = old

    def resume_request(self, prompt, response_schema=None):
        schema = None if response_schema is None else json.dumps(response_schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        wire = structured_prompt(prompt, schema)
        return self.complete_in_session(prompt, response_schema, session_dir=self._session_directory(wire), resume=True)

    def _session_directory(self, wire_prompt):
        key = hashlib.sha256((self.identity + "\0" + wire_prompt).encode()).hexdigest()
        return self.session_root / key

    def complete_structured(self, prompt, response_schema):
        return self.complete(prompt, response_schema=response_schema)

    def check_capabilities(self):
        """Start CLI and check agent binding without sending any prompt or model turn."""
        return run_session(self.executable, self.model, None, min(self.timeout, 60))

    def complete(self, prompt, *, response_schema=None):
        schema = None if response_schema is None else json.dumps(response_schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        wire = structured_prompt(prompt, schema)
        context = getattr(self._request_context, "value", None)
        directory = context[0] if context else self._session_directory(wire)
        try:
            with lifecycle_lock(directory):
                return self._complete_locked(prompt, response_schema=response_schema)
        except BlockingIOError:
            raise AntigravityError("session_busy") from None

    def _complete_locked(self, prompt, *, response_schema=None):
        schema = None if response_schema is None else json.dumps(response_schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        last_error, repair_json = AntigravityError("empty_response", retryable=True), False
        for attempt in range(3):
            try:
                text = self._generate(prompt, response_schema=schema, repair_json=repair_json)
                if not text or not text.strip():
                    raise AntigravityError("empty_response", retryable=True)
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    raise AntigravityError("invalid_json", retryable=True) from None
                break
            except AntigravityError as exc:
                if not exc.retryable:
                    raise AntigravityError(exc.code, exit_code=exc.exit_code, attempts=attempt + 1,
                                           conversation_id=exc.conversation_id) from None
                last_error = exc
                if exc.code == "invalid_json":
                    repair_json = True  # same-CID correction, never a fresh prompt
                    continue
            except Exception:
                raise AntigravityError("unexpected_error") from None
            if attempt < 2:
                sleep(15)
        else:
            raise AntigravityError(last_error.code,
                                   retryable=last_error.retryable and last_error.code != "invalid_json",
                                   exit_code=last_error.exit_code, attempts=3,
                                   conversation_id=last_error.conversation_id) from None
        with self._lock:
            self.calls.append(dict(transport="antigravity", requested_model=self.identity, served_model=None,
                                   agent_profile=AGENT_NAME, agent_policy_verified=True))
        return parsed

    def _generate(self, prompt, *, response_schema=None, repair_json=False):
        # Every size uses stdin so init can be checked before sending evidence.
        # agy native --json-schema produced repeated responses/multiple turns
        # without structured_output in the real stdin test. Keep the contract
        # in stdin and let the per-Claim validators reject/repair omissions.
        wire_prompt = structured_prompt(prompt, response_schema)
        timeout = max(self.timeout, 1800) if len(wire_prompt.encode("utf-8")) > _STDIN_THRESHOLD else self.timeout
        context = getattr(self._request_context, "value", None)
        directory, resume = context or (self._session_directory(wire_prompt), False)
        return run_session(self.executable, self.model, wire_prompt, timeout, None,
                           session_dir=directory, resume=resume or repair_json, require_json=True, repair_json=repair_json)
