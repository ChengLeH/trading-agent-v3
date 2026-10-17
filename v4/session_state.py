"""Durable, locked identity for a single cloud request and its conversation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4
from .storage import write_json


class SessionState:
    def __init__(self, directory, model, prompt, template_hash):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "session.json"
        self.binding = dict(model=model, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                            agent_template_sha256=template_hash, format="v4-agy-session/1",
                            continuation_protocol="same-conversation/1")
        self.data = None

    @contextmanager
    def lock(self):
        with (self.directory / "request.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                if self.path.exists():
                    self.data = json.loads(self.path.read_text())
                    if not isinstance(self.data, dict):
                        raise ValueError("invalid session state")
                    if (type(self.data.get("continuations")) is not int or not 0 <= self.data["continuations"] <= 2
                            or type(self.data.get("request_sent")) is not bool):
                        raise ValueError("invalid session counters")
                    for key in ("elapsed_seconds", "budget_seconds"):
                        value = self.data.get(key, 0)
                        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                            raise ValueError("invalid session budget")
                    if any(self.data.get(k) != v for k, v in self.binding.items()):
                        raise ValueError("session binding changed")
                else:
                    self.data = dict(**self.binding, agent_name="v4-evidence-only-" + uuid4().hex,
                                     status="created", request_sent=False, continuations=0)
                    self.save()
                yield self
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self, **changes):
        self.data.update(changes)
        write_json(self.path, self.data)

    def interrupted_system_step(self):
        # Compatibility for an older reader that stopped on the CLI's typed
        # conversation-restoration notice. Never permit an unknown/tool step.
        if self.data.get("last_error") != "invalid_stream":
            return False
        try:
            rows = [json.loads(line) for line in (self.directory / "events.jsonl").read_text().splitlines() if line.strip()]
            last = rows[-1]
            step = last.get("step_update", {})
            return (last.get("v4_attempt_id") == self.data.get("active_attempt_id")
                    and step.get("step_type") == "system_message"
                    and not any(k in step for k in ("tool_name", "tool_info", "subagent_info")))
        except (OSError, ValueError, IndexError, AttributeError):
            return False

    def recover_terminal(self):
        if self.data.get("last_error") not in (None, "timeout"):
            return False
        path = self.directory / "events.jsonl"
        if not path.exists():
            return False
        try:
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            raise ValueError("unreadable session events; refuse paid replay") from None
        if any(not isinstance(r, dict) for r in rows):
            raise ValueError("invalid session event")
        rows = [r for r in rows if r.get("v4_attempt_id") == self.data.get("active_attempt_id")]
        terminal = [r for r in rows if r.get("event") == "result"]
        if len(terminal) != 1 or rows[-1] != terminal[0]:
            return False
        result = terminal[0].get("result", {})
        response = result.get("response")
        if result.get("status") == "SUCCESS" and isinstance(response, str) and response.strip():
            self.save(status="complete", response=response, last_error=None)
            return True
        return False

    def event(self, value):
        # Append and fsync before exposing progress. Partial output is evidence,
        # never an accepted verdict in the absence of a successful result.
        import os
        received_at = datetime.now(timezone.utc).isoformat()
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**value, "v4_attempt_id": self.data.get("active_attempt_id"),
                                     "v4_received_at": received_at}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        step = value.get("step_update", {})
        if (value.get("event") == "step_update" and isinstance(step, dict)
                and step.get("step_type") == "agent_response" and step.get("text_delta")
                and not self.data.get("first_response_at")):
            self.save(first_response_at=received_at)
        if value.get("event") == "result":
            self.save(result_received_at=received_at)
