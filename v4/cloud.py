"""Existing Antigravity (agy) cloud connection only; no direct API fallback."""
import json
import math
import os
import shutil
import subprocess
import threading
import time

_STDIN_THRESHOLD = 64 * 1024  # UTF-8 bytes; margin below a 128 KiB argv-string limit


class AntigravityCloudClient:
    execution = "cloud"

    def __init__(self, model, *, executable=None, timeout=120):
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

    def complete(self, prompt):
        text, last_reason = None, "empty response"
        for attempt in range(3):
            try:
                text = self._generate(prompt)
                if text and text.strip():
                    break
                last_reason = "empty response"
            except Exception as exc:
                # Unexpected exception text can contain request/account data.
                last_reason = type(exc).__name__
            text = None
            if attempt < 2:
                time.sleep(15)
        if text is None:
            raise RuntimeError(f"Antigravity request failed after 3 tries (model={self.identity}, "
                               f"prompt_len={len(prompt)}): {last_reason}; no API fallback") from None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            raise ValueError("Antigravity response must be strict JSON") from None
        with self._lock:
            self.calls.append(dict(transport="antigravity", requested_model=self.identity, served_model=None))
        return parsed

    def _generate(self, prompt):
        if len(prompt.encode("utf-8")) > _STDIN_THRESHOLD:
            return self._generate_stdin(prompt)
        try:
            result = subprocess.run(
                [self.executable, "--model", self.model, "--output-format", "text",
                 "--disable-slash-commands", "--mode", "plan", "--sandbox",
                 "--print-timeout", f"{self.timeout}s", "-p", prompt],
                capture_output=True, text=True, timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Antigravity cloud request timed out") from None
        except OSError:
            raise RuntimeError("Antigravity CLI could not start") from None
        if result.returncode != 0:
            # Diagnostics may contain prompts or account data; keep them out
            # of delivery artifacts. Failure must never switch transports.
            raise RuntimeError(f"Antigravity CLI failed (exit {result.returncode}); no API fallback")
        return result.stdout

    def _generate_stdin(self, prompt):
        timeout = max(self.timeout, 1800)
        message = json.dumps(dict(event="user", message=dict(role="user", content=prompt)), ensure_ascii=False) + "\n"
        try:
            result = subprocess.run(
                [self.executable, "--model", self.model,
                 "--disable-slash-commands", "--mode", "plan", "--sandbox",
                 "--input-format", "stream-json", "--output-format", "stream-json",
                 "--print-timeout", f"{timeout}s"],
                input=message, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Antigravity cloud request timed out") from None
        except OSError:
            raise RuntimeError("Antigravity CLI could not start") from None
        if result.returncode != 0:
            raise RuntimeError(f"Antigravity CLI failed (exit {result.returncode}); no API fallback")
        terminal = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                raise ValueError("Antigravity stream event must be strict JSON") from None
            if not isinstance(event, dict):
                raise ValueError("Antigravity stream event must be an object")
            if event.get("event") == "result":
                terminal.append(event.get("result"))
        if len(terminal) != 1 or not isinstance(terminal[0], dict):
            raise RuntimeError("Antigravity stream requires exactly one result event")
        response = terminal[0]
        if response.get("status") != "SUCCESS":
            raise RuntimeError("Antigravity stream returned an unsuccessful result")
        if not isinstance(response.get("response"), str):
            raise ValueError("Antigravity stream response must be text")
        return response["response"]
