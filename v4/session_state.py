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
from .session_replay import lifecycle_lock, recover_authorized_replay
from .response_json import normalize_json_response


def validate_limits(max_continuations=None, budget_seconds=None):
    if max_continuations is not None and (type(max_continuations) is not int or max_continuations < 0):
        raise ValueError("max_continuations must be a nonnegative integer")
    if budget_seconds is not None and (type(budget_seconds) not in (int, float)
                                      or not math.isfinite(budget_seconds) or budget_seconds <= 0):
        raise ValueError("budget_seconds must be positive and finite")


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
        with lifecycle_lock(self.directory):
            replay = recover_authorized_replay(self.directory, binding=self.binding)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (self.directory / "request.lock").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    if self.path.exists():
                        self.data = json.loads(self.path.read_text())
                        if not isinstance(self.data, dict):
                            raise ValueError("invalid session state")
                        maximum = self.data.get("max_continuations", 2)
                        if (type(maximum) is not int or maximum < 0
                                or type(self.data.get("continuations")) is not int or not 0 <= self.data["continuations"] <= maximum
                                or type(self.data.get("request_sent")) is not bool):
                            raise ValueError("invalid session counters")
                        for key in ("turn_acknowledged", "request_written", "json_required"):
                            if key in self.data and type(self.data[key]) is not bool:
                                raise ValueError("invalid session delivery/format flags")
                        for key in ("elapsed_seconds", "budget_seconds"):
                            value = self.data.get(key, 0)
                            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                                raise ValueError("invalid session budget")
                        if any(self.data.get(k) != v for k, v in self.binding.items()):
                            raise ValueError("session binding changed")
                    else:
                        self.data = dict(**self.binding, replay_generation=replay["generation"] if replay else None, agent_name="v4-evidence-only-" + uuid4().hex,
                                         status="created", request_sent=False, continuations=0)
                        self.save()
                    yield self
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self, **changes):
        self.data.update(changes)
        write_json(self.path, self.data)

    def configure_limits(self, timeout, *, max_continuations=None, budget_seconds=None):
        """Raise durable ceilings without erasing consumed work or identity."""
        validate_limits(max_continuations, budget_seconds)
        old_max = self.data.get("max_continuations", 2)
        old_budget = self.data.get("budget_seconds", timeout * 3)
        new_max = max(old_max, max_continuations) if max_continuations is not None else old_max
        new_budget = max(old_budget, budget_seconds) if budget_seconds is not None else old_budget
        changes = dict(max_continuations=new_max, budget_seconds=new_budget,
                       elapsed_seconds=self.data.get("elapsed_seconds", 0))
        if new_max != old_max or new_budget != old_budget:
            history = list(self.data.get("limit_changes", []))
            history.append(dict(at=datetime.now(timezone.utc).isoformat(),
                                previous_max_continuations=old_max, max_continuations=new_max,
                                previous_budget_seconds=old_budget, budget_seconds=new_budget))
            changes["limit_changes"] = history
        self.save(**changes)

    def interrupted_application_step(self):
        # Compatibility for an older reader that stopped on the CLI's typed
        # conversation-restoration notice. Never permit an unknown/tool step.
        if self.data.get("last_error") != "invalid_stream":
            return False
        try:
            rows = [json.loads(line) for line in (self.directory / "events.jsonl").read_text().splitlines() if line.strip()]
            if any(not isinstance(row, dict) for row in rows):
                return None
            rows = [row for row in rows if row.get("v4_attempt_id") == self.data.get("active_attempt_id")]
            if any(row.get("event") not in ("init", "step_update") for row in rows):
                return None
            for row in rows:
                item = row.get("step_update", {})
                if (not isinstance(item, dict) or item.get("step_type") in ("tool", "subagent")
                        or any(k in item for k in ("tool_name", "tool_info", "subagent_info"))):
                    return None
            last = rows[-1]
            step = last.get("step_update", {})
            if (last.get("event") == "step_update" and last.get("v4_attempt_id") == self.data.get("active_attempt_id")
                    and step.get("step_type") in ("system_message", "error_message")
                    and not any(k in step for k in ("tool_name", "tool_info", "subagent_info"))):
                return step
            return None
        except (OSError, ValueError, IndexError, AttributeError):
            return False

    def validate_json_response(self, response):
        try:
            return normalize_json_response(response)
        except (ValueError, TypeError):
            self.save(status="paused", response=response, last_error="invalid_json",
                      json_required=True, turn_acknowledged=True)
            return None

    def recover_terminal(self, *, require_json=False):
        if self.data.get("last_error") not in (None, "timeout", "cli_io_failed", "invalid_json", "continuation_exhausted"):
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
        if not terminal:
            return False
        if len(terminal) != 1 or rows[-1] != terminal[0]:
            raise ValueError("ambiguous terminal stream")
        result = terminal[0].get("result", {})
        if not isinstance(result, dict):
            raise ValueError("invalid terminal envelope")
        if not (self.data.get("request_written") or self.data.get("turn_acknowledged")
                or self.data.get("status") == "complete"):
            raise ValueError("unconfirmed terminal delivery")
        for row in rows:
            step = row.get("step_update", {})
            if (not isinstance(step, dict) or step.get("step_type") in ("tool", "subagent")
                    or any(k in step for k in ("tool_name", "tool_info", "subagent_info"))):
                raise ValueError("unsafe terminal stream")
        response = result.get("response")
        if result.get("status") == "SUCCESS" and isinstance(response, str) and response.strip():
            if require_json or self.data.get("json_required"):
                response = self.validate_json_response(response)
                if response is None:
                    return False
            self.save(status="complete", response=response, last_error=None, turn_acknowledged=True,
                      result_received_at=terminal[0].get("v4_received_at", self.data.get("result_received_at")))
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
            self.data["result_received_at"] = received_at  # persisted with the validated terminal state
