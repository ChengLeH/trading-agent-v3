"""Existing Antigravity (agy) cloud connection only; no direct API fallback."""
import json
import math
import os
import shutil
import subprocess
import threading


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
        try:
            parsed = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise ValueError("Antigravity response must be strict JSON") from None
        with self._lock:
            self.calls.append(dict(transport="antigravity", requested_model=self.identity, served_model=None))
        return parsed
