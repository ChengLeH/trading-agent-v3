"""Antigravity NDJSON session: verify capabilities before sending evidence."""
from contextlib import nullcontext
import hashlib
import json
import math
from copy import deepcopy
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
from uuid import uuid4, UUID
from .session_state import SessionState, validate_limits, is_stream_interruption
from .storage import write_json
from .session_replay import abandon_session  # public, explicitly authorized escape hatch

AGENT_NAME = "v4-evidence-only"
_RESULT_DRAIN_SECONDS = 0.25  # bounded trailing-protocol validation, never model time


class AntigravityError(RuntimeError):
    """Safe machine-readable transport failure; never includes raw diagnostics."""
    CODES = frozenset({"cli_start_failed", "cli_exit", "cli_io_failed", "timeout", "empty_response",
                       "invalid_stream", "missing_init", "tool_policy_unverified", "workspace_unverified", "model_unverified",
                       "unexpected_tool_use", "permission_denied", "result_failed", "stream_interrupted", "unexpected_result",
                       "invalid_json", "unexpected_error", "session_pending", "session_busy",
                       "session_binding_changed", "conversation_unverified", "continuation_exhausted", "delivery_unconfirmed"})

    def __init__(self, code, *, retryable=False, exit_code=None, attempts=1, conversation_id=None,
                 session_budget=None, request_context=None):
        if (not isinstance(code, str) or code not in self.CODES or type(retryable) is not bool
                or (exit_code is not None and type(exit_code) is not int)
                or type(attempts) is not int or attempts < 1):
            raise ValueError("invalid Antigravity failure metadata")
        if conversation_id is not None:
            try:
                UUID(conversation_id)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("invalid conversation ID") from None
        if session_budget is not None:
            fields = {"continuations_used", "max_continuations", "elapsed_seconds", "budget_seconds", "last_session_error", "exhausted_by"}
            if (not isinstance(session_budget, dict) or set(session_budget) != fields
                    or any(type(session_budget[k]) is not int or session_budget[k] < 0 for k in ("continuations_used", "max_continuations"))
                    or any(type(session_budget[k]) not in (int, float) or not math.isfinite(session_budget[k]) or session_budget[k] < 0 for k in ("elapsed_seconds", "budget_seconds"))
                    or session_budget["last_session_error"] not in (None, *self.CODES)
                    or not isinstance(session_budget["exhausted_by"], list)
                    or any(k not in ("continuations", "time") for k in session_budget["exhausted_by"])):
                raise ValueError("invalid session budget diagnostics")
        if request_context is not None and (not isinstance(request_context, dict)
                or set(request_context) != {"task_id", "session_directory", "layer", "claim_ids"}
                or any(not isinstance(request_context[k], str) or not request_context[k] for k in ("task_id", "session_directory", "layer"))
                or not isinstance(request_context["claim_ids"], list)
                or any(not isinstance(cid, str) or not cid for cid in request_context["claim_ids"])
                or len(request_context["claim_ids"]) != len(set(request_context["claim_ids"]))):
            raise ValueError("invalid request diagnostics")
        self.session_budget, self.request_context = deepcopy(session_budget), deepcopy(request_context)
        self.conversation_id = conversation_id
        self.code, self.retryable = code, retryable
        self.exit_code, self.attempts = exit_code, attempts
        super().__init__(f"Antigravity {code} (attempts={attempts}, exit={exit_code}); no API fallback")

    def details(self):
        result = dict(error_code=self.code, retryable=self.retryable,
                      exit_code=self.exit_code, attempts=self.attempts)
        if self.conversation_id is not None:
            result.update(conversation_id=self.conversation_id,
                          resumable=self.code in {"session_pending", "continuation_exhausted", "invalid_json", "stream_interrupted"})
        if self.session_budget is not None:
            result["session_budget"] = deepcopy(self.session_budget)
        if self.request_context is not None:
            result["request_context"] = deepcopy(self.request_context)
        if self.code == "continuation_exhausted":
            result["recovery_options"] = ["raise_same_session_limits", "explicitly_authorize_one_new_session"]
        return result

    def with_context(self, *, attempts=None, request_context=None):
        return AntigravityError(self.code, retryable=self.retryable, exit_code=self.exit_code,
                                attempts=self.attempts if attempts is None else attempts,
                                conversation_id=self.conversation_id, session_budget=self.session_budget,
                                request_context=self.request_context if request_context is None else request_context)


    def blocked_status(self, default):
        return {"invalid_json": "blocked:response_format", "session_pending": "blocked:session_pending",
                "continuation_exhausted": "blocked:session_exhausted"}.get(
                    self.code, "blocked:cloud_policy" if not self.retryable else default)


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


def run_session(executable, model, prompt, timeout, response_schema=None, *, session_dir=None, resume=False, require_json=False, repair_json=False, max_continuations=None, budget_seconds=None):
    validate_limits(max_continuations, budget_seconds)
    if (max_continuations is not None or budget_seconds is not None) and (session_dir is None or prompt is None):
        raise ValueError("persistent session_dir and prompt required for configurable limits")
    if (require_json or repair_json) and (session_dir is None or prompt is None):
        raise ValueError("persistent session_dir and prompt required for JSON validation or correction")
    if session_dir is None or prompt is None:
        return _run_session(executable, model, prompt, timeout, response_schema)
    _, template_hash = load_agent_profile()
    store = SessionState(session_dir, model, prompt, template_hash)
    try:
        with store.lock():
            state = store.data
            require_json = require_json or state.get("json_required", False)
            if require_json and not state.get("json_required"):
                store.save(json_required=True)
            store.recover_terminal(require_json=require_json)
            if state["status"] == "complete":
                if not require_json:
                    return state["response"]
                normalized = store.validate_json_response(state["response"])
                if normalized is not None:
                    if normalized != state["response"]:
                        store.save(response=normalized)
                    return normalized
            if state.get("last_error") == "invalid_json":
                if not repair_json and not resume:
                    raise AntigravityError("invalid_json", retryable=True, conversation_id=state.get("conversation_id"))
                repair_json = True
            if repair_json and not state["request_sent"]:
                raise AntigravityError("conversation_unverified")
            legacy_step = store.interrupted_application_step()
            if legacy_step and permission_denied(json.dumps(legacy_step).encode()):
                raise AntigravityError("permission_denied", conversation_id=state.get("conversation_id"))
            if (not state["request_sent"] and state.get("last_error") in
                    {"invalid_stream", "tool_policy_unverified", "workspace_unverified", "model_unverified",
                     "unexpected_tool_use", "permission_denied", "unexpected_result", "unexpected_error"}):
                raise AntigravityError(state["last_error"])
            store.configure_limits(timeout, max_continuations=max_continuations, budget_seconds=budget_seconds)
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
                if (state.get("last_error") not in {"timeout", "empty_response", "cli_exit", "cli_io_failed", "result_failed", "stream_interrupted", "invalid_json"}
                        and not (state.get("last_error") is None and state.get("status") == "in_flight")
                        and not legacy_step):
                    raise AntigravityError("conversation_unverified", conversation_id=cid)
                if not cid:
                    raise AntigravityError("conversation_unverified")
            remaining = state["budget_seconds"] - state["elapsed_seconds"]
            exhausted = []
            if (state["request_sent"] and state.get("last_error") != "stream_interrupted"
                    and state["continuations"] >= state["max_continuations"]):
                exhausted.append("continuations")
            if remaining <= 0:
                exhausted.append("time")
            if exhausted:
                raise AntigravityError("continuation_exhausted", conversation_id=state.get("conversation_id"),
                                       session_budget=dict(continuations_used=state["continuations"],
                                           max_continuations=state["max_continuations"], elapsed_seconds=state["elapsed_seconds"],
                                           budget_seconds=state["budget_seconds"], exhausted_by=exhausted,
                                           last_session_error=state.get("last_error") if state.get("last_error") in AntigravityError.CODES else None))
            started = time.monotonic()
            prior_error, prior_status = state.get("last_error"), state["status"]
            prior_continuations, previously_sent = state["continuations"], state["request_sent"]
            prior_elapsed = state["elapsed_seconds"]
            # Reserve the full allowance before spawn: a hard crash cannot
            # reset the cross-process budget. Normal completion refunds unused time.
            store.save(active_attempt_id=uuid4().hex,
                       elapsed_seconds=prior_elapsed + min(timeout, remaining))
            try:
                return _run_session(executable, model, prompt, min(timeout, remaining), response_schema, store=store, require_json=require_json, repair_json=repair_json)
            except AntigravityError as exc:
                sent = store.data["request_sent"]
                if (previously_sent and store.data["continuations"] == prior_continuations
                        and exc.code in {"missing_init", "cli_start_failed"}):
                    # No continuation input was reserved, so keep the previous
                    # resumable turn instead of replacing it with a startup error.
                    store.save(status=prior_status, last_error=prior_error)
                    raise AntigravityError(exc.code, retryable=True,
                                           conversation_id=store.data.get("conversation_id")) from None
                store.save(status="paused" if sent else "failed", last_error=exc.code)
                if not sent and exc.code == "missing_init":
                    raise AntigravityError("missing_init", retryable=True) from None
                if sent and exc.code == "stream_interrupted":
                    raise AntigravityError("stream_interrupted", retryable=True,
                                           conversation_id=store.data.get("conversation_id")) from None
                if sent and exc.code in {"timeout", "empty_response", "cli_exit", "cli_io_failed", "result_failed"}:
                    raise AntigravityError("session_pending", conversation_id=store.data.get("conversation_id")) from None
                raise
            finally:
                store.save(elapsed_seconds=prior_elapsed + time.monotonic() - started)
    except BlockingIOError:
        raise AntigravityError("session_busy") from None
    except ValueError:
        raise AntigravityError("session_binding_changed") from None


def _run_session(executable, model, prompt, timeout, response_schema=None, *, store=None, require_json=False, repair_json=False):
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
                content = ("上一輪回覆不是合法 JSON。請依同一對話中的原任務、證據及回覆規格，重新完整輸出單一合法 JSON。"
                           "不得重送原始任務、重新搜尋、增刪必要欄位、用預設空陣列取代逐引用語義驗證。"
                           "保留原任務要求的物件或陣列形式；不要 Markdown、code fence 或任何前後說明。"
                           if repair_json else ("接續同一對話中尚未完成的上一個驗證任務，使用已有來源與推理進度。"
                           "不要重新搜尋或從頭分析。前次輸出可能只是片段，本回合請重新完整輸出符合原契約的最終 JSON。"
                           "即使先前已輸出部分欄位或物件，本次也必須包含全部必要內容；不要只回剩餘片段或省略已做項目。"
                           "原任務要求物件就回完整物件，要求陣列就回完整陣列；批次任務須包含全部指定 Claim。"
                           if resuming else prompt))
                if store:
                    # Persist the uncertain delivery boundary before any bytes:
                    # after a crash it is unsafe to assume nothing was sent.
                    transport_retry = resuming and store.data.get("last_error") == "stream_interrupted"
                    store.save(request_sent=True, request_written=False, turn_acknowledged=False, status="in_flight", last_error=None,
                               continuations=store.data["continuations"] + int(resuming and not transport_retry),
                               transport_retries=store.data.get("transport_retries", 0) + int(transport_retry))
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
                if step.get("step_type") == "error_message":
                    if permission_denied(json.dumps(step).encode()):
                        raise AntigravityError("permission_denied")
                    # A typed application error is not stream corruption. The
                    # wrapper pauses this turn; only explicit, bounded same-CID
                    # continuation is allowed, not an automatic fresh request.
                    raise AntigravityError("result_failed", retryable=True)
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
                if is_stream_interruption(terminal):
                    raise AntigravityError("stream_interrupted", retryable=True)
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
                            if store:
                                store.save(request_written=True)
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
            response = terminal["response"]
            if store:
                if not response.strip():
                    raise AntigravityError("empty_response")
                if require_json:
                    response = store.validate_json_response(response)
                    if response is None:
                        raise AntigravityError("invalid_json", retryable=True, conversation_id=store.data.get("conversation_id"))
                store.save(status="complete", response=response, last_error=None, turn_acknowledged=True,
                           result_to_completion_seconds=round(time.monotonic() - (terminal_deadline - _RESULT_DRAIN_SECONDS), 3))
            return response
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
