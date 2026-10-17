"""Antigravity NDJSON session: verify capabilities before sending evidence."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
from uuid import uuid4

AGENT_NAME = "v4-evidence-only"


class AntigravityError(RuntimeError):
    """Safe machine-readable transport failure; never includes raw diagnostics."""
    CODES = frozenset({"cli_start_failed", "cli_exit", "cli_io_failed", "timeout", "empty_response",
                       "invalid_stream", "missing_init", "tool_policy_unverified", "workspace_unverified", "model_unverified",
                       "unexpected_tool_use", "permission_denied", "result_failed", "unexpected_result",
                       "invalid_json", "unexpected_error"})

    def __init__(self, code, *, retryable=False, exit_code=None, attempts=1):
        if (not isinstance(code, str) or code not in self.CODES or type(retryable) is not bool
                or (exit_code is not None and type(exit_code) is not int)
                or type(attempts) is not int or attempts < 1):
            raise ValueError("invalid Antigravity failure metadata")
        self.code, self.retryable = code, retryable
        self.exit_code, self.attempts = exit_code, attempts
        super().__init__(f"Antigravity {code} (attempts={attempts}, exit={exit_code}); no API fallback")

    def details(self):
        return dict(error_code=self.code, retryable=self.retryable,
                    exit_code=self.exit_code, attempts=self.attempts)


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


def run_session(executable, model, prompt, timeout, response_schema=None):
    """No prompt bytes leave stdin until init binds the controlled agent/model/cwd.

    prompt=None performs a startup check only, without any user turn.
    The temporary workspace contains only the bundled agent definition, never
    run state or responses. Durable evidence/results remain in RunJournal.
    """
    content, template_hash = load_agent_profile()
    agent_name = AGENT_NAME + "-" + uuid4().hex
    config = json.loads(content.split("---", 2)[1])
    config["name"] = agent_name
    selected_profile = "---\n" + json.dumps(config, indent=2) + "\n---" + content.split("---", 2)[2]
    policy_hash = hashlib.sha256(selected_profile.encode("utf-8")).hexdigest()
    with tempfile.TemporaryDirectory(prefix="v4-agy-") as directory:
        workspace = Path(directory).resolve()
        profile = workspace / ".agents" / "agents" / agent_name / "agent.md"
        profile.parent.mkdir(parents=True)
        profile.write_text(selected_profile, encoding="utf-8")
        args = [executable, "--model", model, "--agent", agent_name, "--add-dir", str(workspace),
                "--disable-slash-commands", "--sandbox",
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--print-timeout", f"{timeout}s"]
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
        advertised_count = None

        def event(line):
            nonlocal initialized, terminal, request, advertised_count
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
                initialized = True
                if prompt is None:
                    process.stdin.close()
                    return
                request = (json.dumps(dict(event="user", message=dict(role="user", content=prompt)),
                                      ensure_ascii=False) + "\n").encode("utf-8")
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            elif kind == "step_update":
                step = item.get("step_update")
                if not isinstance(step, dict):
                    raise AntigravityError("invalid_stream")
                if step.get("step_type") in ("tool", "subagent") or any(
                        key in step for key in ("tool_name", "tool_info", "subagent_info")):
                    raise AntigravityError("unexpected_tool_use")
                if step.get("step_type") not in ("user_input", "agent_response", "checkpoint"):
                    raise AntigravityError("invalid_stream")
            elif kind == "result":
                if prompt is None:
                    raise AntigravityError("unexpected_result")
                terminal = item.get("result")
                if not isinstance(terminal, dict):
                    raise AntigravityError("invalid_stream")
                if permission_denied(str(terminal.get("error", "")).encode("utf-8")):
                    raise AntigravityError("permission_denied")
                if terminal.get("status") != "SUCCESS":
                    raise AntigravityError("result_failed", retryable=True)
                if not isinstance(terminal.get("response"), str) or offset != len(request):
                    raise AntigravityError("invalid_stream")
            else:
                raise AntigravityError("invalid_stream")

        failed = True
        try:
            for pipe, label in ((process.stdin, "stdin"), (process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                if label != "stdin":
                    selector.register(pipe, selectors.EVENT_READ, label)
            while selector.get_map():
                remaining = (deadline if initialized else init_deadline) - time.monotonic()
                if remaining <= 0:
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
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise AntigravityError("timeout", retryable=True) from None
            if code != 0:
                raise AntigravityError("cli_exit", retryable=True, exit_code=code)
            if initialized and prompt is None:
                failed = False
                return dict(agent=agent_name, declared_tools=[], agent_policy_verified=True,
                            agent_policy_sha256=policy_hash, agent_template_sha256=template_hash,
                            advertised_tool_count=advertised_count,
                            user_prompt_sent=False)
            if terminal is None:
                raise AntigravityError("empty_response", retryable=True)
            failed = False
            return terminal["response"]
        except (BrokenPipeError, OSError):
            raise AntigravityError("cli_io_failed", retryable=True) from None
        finally:
            selector.close()
            if failed or process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
