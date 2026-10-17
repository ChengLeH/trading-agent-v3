"""Antigravity NDJSON session: verify capabilities before sending evidence."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
from uuid import uuid4, UUID
from .session_state import SessionState
from .storage import write_json

AGENT_NAME = "v4-evidence-only"
_RESULT_DRAIN_SECONDS = 0.25  # bounded trailing-protocol validation, never model time


class AntigravityError(RuntimeError):
    """Safe machine-readable transport failure; never includes raw diagnostics."""
    CODES = frozenset({"cli_start_failed", "cli_exit", "cli_io_failed", "timeout", "empty_response",
                       "invalid_stream", "missing_init", "tool_policy_unverified", "workspace_unverified", "model_unverified",
                       "unexpected_tool_use", "permission_denied", "result_failed", "unexpected_result",
                       "invalid_json", "unexpected_error", "session_pending", "session_busy",
                       "session_binding_changed", "conversation_unverified", "continuation_exhausted", "delivery_unconfirmed"})

    def __init__(self, code, *, retryable=False, exit_code=None, attempts=1, conversation_id=None):
        if (not isinstance(code, str) or code not in self.CODES or type(retryable) is not bool
                or (exit_code is not None and type(exit_code) is not int)
                or type(attempts) is not int or attempts < 1):
            raise ValueError("invalid Antigravity failure metadata")
        if conversation_id is not None:
            try:
                UUID(conversation_id)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("invalid conversation ID") from None
        self.conversation_id = conversation_id
        self.code, self.retryable = code, retryable
        self.exit_code, self.attempts = exit_code, attempts
        super().__init__(f"Antigravity {code} (attempts={attempts}, exit={exit_code}); no API fallback")

    def details(self):
        result = dict(error_code=self.code, retryable=self.retryable,
                      exit_code=self.exit_code, attempts=self.attempts)
        if self.conversation_id is not None:
            result.update(conversation_id=self.conversation_id, resumable=self.code == "session_pending")
        return result


def permission_denied(diagnostic):
    # Match a CLI protocol diagnostic, never claim/finding/source prose.
    return b"permission that headless mode cannot prompt for" in diagnostic.lower()


def load_agent_profile():
    """Validate JSON frontmatter (a YAML subset), never infer policy from prose."""
    try:
        content = Path(__file__).with_name("evidence_agent.md").read_text(encoding="utf-8")
        opening, frontmatter, body = content.split("---", 2)
        config = json.loads(frontmatter)
        required = dict(name=AGENT_NAME, mainAgent=True, subagent=False, model="inherit",
                        tools=[], excludeDefaultComponents=True, inheritCustomizations=False,
                        commandExecutionPolicy="off", mcpServers=[], skills=[], plugins=[])
        if (opening.strip() or not body.strip() or not isinstance(config, dict)
                or set(config) != set(required) | {"description"}
                or not isinstance(config["description"], str)
                or any(type(config[k]) is not type(v) or config[k] != v for k, v in required.items())):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise AntigravityError("tool_policy_unverified") from None
    return content, hashlib.sha256(content.encode("utf-8")).hexdigest()


def run_session(executable, model, prompt, timeout, response_schema=None, *, session_dir=None, resume=False):
    if session_dir is None or prompt is None:
        return _run_session(executable, model, prompt, timeout, response_schema)
    _, template_hash = load_agent_profile()
    store = SessionState(session_dir, model, prompt, template_hash)
    try:
        with store.lock():
            state = store.data
            store.recover_terminal()
            if state["status"] == "complete":
                return state["response"]
            previous_pid = state.get("process_id")
            if previous_pid is not None:
                if type(previous_pid) is not int or previous_pid <= 0:
                    raise AntigravityError("session_binding_changed")
                try:
                    os.kill(previous_pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise AntigravityError("session_busy") from None
                else:
                    # Never start a second paid turn while an orphan may still
                    # own the first. Do not kill a PID that may have been reused.
                    raise AntigravityError("session_busy")
            if state["request_sent"]:
                cid = state.get("conversation_id")
                if not state.get("turn_acknowledged"):
                    raise AntigravityError("delivery_unconfirmed", conversation_id=cid)
                if not resume:
                    raise AntigravityError("session_pending", conversation_id=cid)
                if (state.get("last_error") not in {"timeout", "empty_response", "cli_exit", "cli_io_failed", "result_failed"}
                        and not (state.get("last_error") is None and state.get("status") == "in_flight")
                        and not store.interrupted_system_step()):
                    raise AntigravityError("conversation_unverified", conversation_id=cid)
                if not cid:
                    raise AntigravityError("conversation_unverified")
                if state["continuations"] >= 2:
                    raise AntigravityError("continuation_exhausted", conversation_id=cid)
            if "budget_seconds" not in state:
                store.save(budget_seconds=timeout * 3, elapsed_seconds=0)
            remaining = state["budget_seconds"] - state["elapsed_seconds"]
            if remaining <= 0:
                raise AntigravityError("continuation_exhausted", conversation_id=state.get("conversation_id"))
            started = time.monotonic()
            prior_elapsed = state["elapsed_seconds"]
            # Reserve the full allowance before spawn: a hard crash cannot
            # reset the cross-process budget. Normal completion refunds unused time.
            store.save(active_attempt_id=uuid4().hex, last_error=None,
                       elapsed_seconds=prior_elapsed + min(timeout, remaining))
            try:
                return _run_session(executable, model, prompt, min(timeout, remaining), response_schema, store=store)
            except AntigravityError as exc:
                sent = store.data["request_sent"]
                store.save(status="paused" if sent else "failed", last_error=exc.code)
                if not sent and exc.code == "missing_init":
                    raise AntigravityError("missing_init", retryable=True) from None
                if sent and exc.code in {"timeout", "empty_response", "cli_exit", "cli_io_failed", "result_failed"}:
                    raise AntigravityError("session_pending", conversation_id=store.data.get("conversation_id")) from None
                raise
            finally:
                store.save(elapsed_seconds=prior_elapsed + time.monotonic() - started)
    except BlockingIOError:
        raise AntigravityError("session_busy") from None
    except ValueError:
        raise AntigravityError("session_binding_changed") from None


def _run_session(executable, model, prompt, timeout, response_schema=None, *, store=None):
    """No prompt bytes leave stdin until init binds the controlled agent/model/cwd.

    prompt=None performs a startup check only, without any user turn.
    Regular requests retain their isolated workspace under SessionState; only
    prompt-free capability checks use temporary storage.
    """
    content, template_hash = load_agent_profile()
    agent_name = store.data["agent_name"] if store else AGENT_NAME + "-" + uuid4().hex
    if not isinstance(agent_name, str) or not agent_name.startswith(AGENT_NAME + "-"):
        raise AntigravityError("tool_policy_unverified")
    try:
        UUID(agent_name[len(AGENT_NAME) + 1:])
    except ValueError:
        raise AntigravityError("tool_policy_unverified") from None
    config = json.loads(content.split("---", 2)[1])
    config["name"] = agent_name
    selected_profile = "---\n" + json.dumps(config, indent=2) + "\n---" + content.split("---", 2)[2]
    policy_hash = hashlib.sha256(selected_profile.encode("utf-8")).hexdigest()
    resuming = bool(store and store.data["request_sent"])
    conversation = store.data.get("conversation_id") if resuming else None
    workspace_context = nullcontext(str(store.directory / "workspace")) if store else tempfile.TemporaryDirectory(prefix="v4-agy-")
    with workspace_context as directory:
        workspace = Path(directory).resolve()
        profile = workspace / ".agents" / "agents" / agent_name / "agent.md"
        profile.parent.mkdir(parents=True, exist_ok=True)
        if profile.exists() and profile.read_text(encoding="utf-8") != selected_profile:
            raise AntigravityError("tool_policy_unverified")
        profile.write_text(selected_profile, encoding="utf-8")
        if store:
            if not (store.directory / "request.json").exists():
                write_json(store.directory / "request.json", dict(prompt=prompt, model=model))
        args = [executable, "--model", model, "--agent", agent_name, "--add-dir", str(workspace),
                "--disable-slash-commands", "--sandbox",
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--print-timeout", f"{timeout}s"]
        if conversation:
            args += ["--conversation", conversation]
        if response_schema is not None:
            args += ["--json-schema", response_schema]
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=workspace, start_new_session=True)
        except OSError:
            raise AntigravityError("cli_start_failed", retryable=True) from None
        deadline = time.monotonic() + timeout
        init_deadline = time.monotonic() + min(timeout, 60)
        selector = selectors.DefaultSelector()
        stdout, stderr_tail = b"", b""
        request, offset, initialized, terminal = None, 0, False, None
        terminal_deadline = None
        advertised_count = None

        def event(line):
            nonlocal initialized, terminal, request, advertised_count, terminal_deadline
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                raise AntigravityError("invalid_stream") from None
            if not isinstance(item, dict) or terminal is not None:
                raise AntigravityError("invalid_stream")
            kind = item.get("event")
            if not initialized:
                init = item.get("init")
                if kind != "init" or not isinstance(init, dict):
                    raise AntigravityError("missing_init")
                if init.get("agent") != agent_name:
                    raise AntigravityError("tool_policy_unverified")
                # init.tools advertises the CLI global registry, not this agent's
                # effective allowlist. Nonempty lists are normal (agy 1.4.0).
                tools = init.get("tools")
                if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
                    raise AntigravityError("invalid_stream")
                advertised_count = len(tools)
                if init.get("model") != model:
                    raise AntigravityError("model_unverified")
                if not isinstance(init.get("cwd"), str) or Path(init["cwd"]).resolve() != workspace:
                    raise AntigravityError("workspace_unverified")
                if store:
                    cid = item.get("conversation_id")
                    try:
                        UUID(cid)
                    except (ValueError, TypeError, AttributeError):
                        raise AntigravityError("conversation_unverified") from None
                    if conversation is not None and cid != conversation:
                        raise AntigravityError("conversation_unverified")
                    store.save(conversation_id=cid, status="started")
                    store.event(item)
                initialized = True
                if prompt is None:
                    process.stdin.close()
                    return
                content = ("接續同一對話中尚未完成的上一個驗證任務，使用已有來源與推理進度。"
                           "不要重新搜尋或從頭分析，也不要重複輸出先前物件；請完成並回傳符合原契約的單一最終 JSON。"
                           if resuming else prompt)
                if store:
                    # Persist the uncertain delivery boundary before any bytes:
                    # after a crash it is unsafe to assume nothing was sent.
                    store.save(request_sent=True, turn_acknowledged=False, status="in_flight",
                               continuations=store.data["continuations"] + int(resuming))
                request = (json.dumps(dict(event="user", message=dict(role="user", content=content)),
                                      ensure_ascii=False) + "\n").encode("utf-8")
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            elif kind == "step_update":
                if store:
                    store.event(item)
                step = item.get("step_update")
                if not isinstance(step, dict):
                    raise AntigravityError("invalid_stream")
                if step.get("step_type") in ("tool", "subagent") or any(
                        key in step for key in ("tool_name", "tool_info", "subagent_info")):
                    raise AntigravityError("unexpected_tool_use")
                if store and step.get("step_type") == "user_input" and step.get("state") == "DONE":
                    store.save(turn_acknowledged=True)
                if step.get("step_type") not in ("user_input", "agent_response", "checkpoint") and not (resuming and step.get("step_type") == "system_message"):
                    raise AntigravityError("invalid_stream")
            elif kind == "result":
                if prompt is None:
                    raise AntigravityError("unexpected_result")
                if store:
                    store.event(item)
                terminal = item.get("result")
                if not isinstance(terminal, dict):
                    raise AntigravityError("invalid_stream")
                if permission_denied(str(terminal.get("error", "")).encode("utf-8")):
                    raise AntigravityError("permission_denied")
                if terminal.get("status") != "SUCCESS":
                    raise AntigravityError("result_failed", retryable=True)
                if not isinstance(terminal.get("response"), str) or offset != len(request):
                    raise AntigravityError("invalid_stream")
                # A result completes this turn. EOF belongs to process teardown,
                # and may never arrive if a child retains stdout/stderr.
                terminal_deadline = time.monotonic() + _RESULT_DRAIN_SECONDS
            else:
                raise AntigravityError("invalid_stream")

        try:
            if store:
                store.save(process_id=process.pid)
            for pipe, label in ((process.stdin, "stdin"), (process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                if label != "stdin":
                    selector.register(pipe, selectors.EVENT_READ, label)
            while selector.get_map():
                limit = terminal_deadline if terminal is not None else (deadline if initialized else init_deadline)
                remaining = limit - time.monotonic()
                if remaining <= 0:
                    if terminal is not None:
                        break
                    raise AntigravityError("timeout" if initialized else "missing_init", retryable=initialized)
                for key, _ in selector.select(remaining):
                    if key.data == "stdin":
                        count = os.write(key.fd, request[offset:offset + 65536])
                        offset += count
                        if offset == len(request):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.data == "stdout" and stdout.strip():
                            event(stdout)
                            stdout = b""
                        continue
                    if key.data == "stderr":
                        diagnostic = stderr_tail + chunk
                        if permission_denied(diagnostic):
                            raise AntigravityError("permission_denied")
                        stderr_tail = diagnostic[-8192:]
                    else:
                        stdout += chunk
                        while b"\n" in stdout:
                            line, stdout = stdout.split(b"\n", 1)
                            if line.strip():
                                event(line)
            if terminal is not None:
                if stdout.strip():
                    event(stdout)  # reject a trailing partial/second frame too
                code = process.poll()
            else:
                try:
                    code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise AntigravityError("timeout", retryable=True) from None
            if code is not None and code != 0:
                raise AntigravityError("cli_exit", retryable=True, exit_code=code)
            if initialized and prompt is None:
                return dict(agent=agent_name, declared_tools=[], agent_policy_verified=True,
                            agent_policy_sha256=policy_hash, agent_template_sha256=template_hash,
                            advertised_tool_count=advertised_count,
                            user_prompt_sent=False)
            if terminal is None:
                raise AntigravityError("empty_response", retryable=True)
            if store:
                if not terminal["response"].strip():
                    raise AntigravityError("empty_response")
                store.save(status="complete", response=terminal["response"], last_error=None,
                           result_to_completion_seconds=round(time.monotonic() - (terminal_deadline - _RESULT_DRAIN_SECONDS), 3))
            return terminal["response"]
        except (BrokenPipeError, OSError):
            raise AntigravityError("cli_io_failed", retryable=True) from None
        finally:
            selector.close()
            # Only signal our still-live child. Once its leader has exited,
            # close our pipe ends without risking a reused process/group ID.
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            if store:
                store.save(process_id=None)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
