"""Durable run progress and per-request responses, outside process temp storage."""
import json
import hashlib
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4
from pathlib import Path
from .checkpoint import save_checkpoint, write_json
from .schema import digest
from .cloud import request_completion, structured_prompt
from .agy_session import AntigravityError
from .session_replay import replay_marker, lifecycle_lock, abandon_session


class RunJournal:
    def __init__(self, directory, *, resume_from=None, retry_roles=(), continue_verification=True):
        self.directory = Path(directory)
        self.read_directories = [self.directory]
        self.retry_roles = set(retry_roles)
        self.continue_verification = continue_verification
        if resume_from is not None:
            self.read_directories.append(Path(resume_from).parent)

    def checkpoint(self, runner):
        save_checkpoint(runner, self.directory / "checkpoint.json")
        write_json(self.directory / "progress.json", dict(run_id=runner.run_id, status=runner.status,
            updated_at=datetime.now(timezone.utc).isoformat(), total_claims=len(runner.run.claims),
            atomic_verified=len(runner.receipts), deep_verified=len(runner.deep_receipts),
            max_workers=runner.max_workers, review_batch_size=runner.review_batch_size))

    def _response_metadata(self, runner, client, task_id, prompt, response_schema, review_context):
        metadata = dict(format="v4-response/1", run_id=runner.run_id, model=client.identity,
                        task_id=task_id, prompt_hash=digest(prompt))
        if review_context is not None:
            from .agy_session import load_agent_profile
            metadata["review"] = review_context
            metadata["agent_policy_sha256"] = (load_agent_profile()[1] if callable(getattr(client, "complete_in_session", None)) else "external")
        if response_schema is not None:
            metadata["response_schema_hash"] = digest(response_schema)
        return metadata

    def response_provenance(self, runner, client, task_id, prompt, response, *, response_schema=None,
                            review_context=None):
        """Return the durable received-attempt identity for this exact response."""
        metadata = self._response_metadata(runner, client, task_id, prompt, response_schema, review_context)
        path = self.directory / "responses" / (digest(metadata) + ".json")
        if not path.is_file():
            return None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            attempt_id = saved.get("attempt_id")
            if (saved.get("status") != "received" or any(saved.get(k) != v for k, v in metadata.items())
                    or saved.get("response") != response or not isinstance(attempt_id, str)
                    or UUID(attempt_id).hex != attempt_id):
                return None
            archive_path = self.directory / "responses" / "archive" / (attempt_id + ".json")
            archived = json.loads(archive_path.read_text(encoding="utf-8"))
            if archived != saved:
                return None
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return dict(attempt_id=attempt_id, metadata_hash=digest(metadata), response_hash=digest(response))

    def response_attempt(self, attempt_id, run_id):
        """Read a received response archive only when it belongs to this run."""
        try:
            if not isinstance(attempt_id, str) or UUID(attempt_id).hex != attempt_id:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        for directory in self.read_directories:
            path = directory / "responses" / "archive" / (attempt_id + ".json")
            if not path.is_file():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if (record.get("attempt_id") == attempt_id and record.get("status") == "received"
                    and record.get("run_id") == run_id):
                return record
        return None

    def received_attempts(self, run_id):
        attempts = {}
        for directory in self.read_directories:
            archive_dir = directory / "responses" / "archive"
            if not archive_dir.is_dir():
                continue
            for path in archive_dir.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                attempt_id = record.get("attempt_id")
                if (record.get("status") == "received" and record.get("run_id") == run_id
                        and isinstance(attempt_id, str) and UUID(attempt_id).hex == attempt_id):
                    attempts[attempt_id] = record
        return list(attempts.values())

    def complete(self, runner, client, task_id, prompt, *, reuse_role_output=False, response_schema=None, review_context=None):
        metadata = self._response_metadata(runner, client, task_id, prompt, response_schema, review_context)
        relative = Path("responses") / (digest(metadata) + ".json")
        session_directory = self.directory / "sessions" / digest(metadata)
        if task_id in self.retry_roles:
            session_directory = self.directory / "sessions" / digest(dict(metadata=metadata, explicit_retry=uuid4().hex))
        explicit_resume = self.continue_verification and task_id not in self.retry_roles
        can_use_session = callable(getattr(client, "complete_in_session", None))
        if (reuse_role_output or (self.continue_verification and review_context is not None)) and task_id not in self.retry_roles:
            for directory in self.read_directories:
                path = directory / relative
                if not path.exists():
                    continue
                saved = json.loads(path.read_text(encoding="utf-8"))
                if any(saved.get(k) != v for k, v in metadata.items()):
                    raise ValueError("saved response does not match current request")
                attempt = saved.get("attempt_id")
                if not isinstance(attempt, str) or UUID(attempt).hex != attempt:
                    raise ValueError("invalid saved response attempt_id")
                origin = Path(saved.get("session_directory", directory / "sessions" / digest(metadata)))
                authorization = replay_marker(origin) if can_use_session else None
                if authorization and saved.get("replay_generation") != authorization["generation"]:
                    session_directory = origin
                    break  # explicit one-generation replay authorization
                if saved.get("status") == "in_flight":
                    archive = directory / "responses" / "archive" / (attempt + ".json")
                    if not archive.exists():
                        origin = Path(saved.get("session_directory", directory / "sessions" / digest(metadata)))
                        if can_use_session and (origin / "session.json").is_file():
                            session_directory = origin
                            break
                        raise AntigravityError("delivery_unconfirmed")
                    saved = json.loads(archive.read_text(encoding="utf-8"))
                    if any(saved.get(k) != v for k, v in {**metadata, "attempt_id": attempt}.items()):
                        raise ValueError("saved archive does not match current attempt")
                if saved.get("status") == "received":
                    write_json(self.directory / "responses" / "archive" / (attempt + ".json"), saved)
                    write_json(self.directory / relative, saved)
                    return saved["response"]
                if saved.get("status") == "failed":
                    failure = AntigravityError(saved.get("error_code"), retryable=saved.get("retryable"),
                                               exit_code=saved.get("exit_code"), attempts=saved.get("attempts"),
                                               conversation_id=saved.get("conversation_id"), session_budget=saved.get("session_budget"),
                                               request_context=saved.get("request_context"))
                    origin = Path(saved.get("session_directory", directory / "sessions" / digest(metadata)))
                    if can_use_session and (origin / "session.json").is_file() and failure.code in {"session_pending", "stream_interrupted", "missing_init", "cli_start_failed", "invalid_stream", "invalid_json", "continuation_exhausted"}:
                        session_directory = origin
                        break
                    if not failure.retryable:
                        # An operator may authorize a new generation after the
                        # earlier snapshot. Recheck under the stable lock before
                        # applying a failure belonging to the archived generation.
                        with lifecycle_lock(origin):
                            authorization = replay_marker(origin) if can_use_session else None
                            if authorization and saved.get("replay_generation") != authorization["generation"]:
                                session_directory = origin
                                break
                            raise failure
                    if failure.code != "cli_start_failed":
                        raise AntigravityError("delivery_unconfirmed")
                    break  # proven not sent: safe startup retry
                raise ValueError("invalid saved response status")
        try:
            with lifecycle_lock(session_directory):
                return self._request_locked(client, prompt, response_schema, explicit_resume, session_directory, metadata, relative)
        except BlockingIOError:
            raise AntigravityError("session_busy") from None

    def authorize_session_replay(self, runner, conversation_id, *, reason):
        """Explicitly archive one exhausted request belonging to this run."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("explicit paid-replay reason required")
        if not isinstance(conversation_id, str):
            raise ValueError("conversation ID required")
        conversation_id = str(UUID(conversation_id))
        allowed_roots = {(root.resolve() / "sessions").resolve() for root in self.read_directories}
        matches = {}
        for directory in self.read_directories:
            for path in (directory / "responses").glob("*.json"):
                saved = json.loads(path.read_text(encoding="utf-8"))
                if (saved.get("run_id") != runner.run_id or str(saved.get("conversation_id", "")).lower() != conversation_id
                        or saved.get("status") != "failed" or saved.get("error_code") != "continuation_exhausted"):
                    continue
                review = saved.get("review", {})
                if review and review.get("verification_epoch") != runner._verification_epoch:
                    continue
                if not isinstance(saved.get("session_directory"), str) or not saved["session_directory"]:
                    raise ValueError("exhausted request lacks a persistent session directory")
                target = Path(saved["session_directory"]).resolve()
                if target.parent not in allowed_roots:
                    raise ValueError("session directory is outside this run's allowed sessions roots")
                keys = ("format", "run_id", "model", "task_id", "prompt_hash", "review", "agent_policy_sha256", "response_schema_hash")
                metadata = {k: saved[k] for k in keys if k in saved}
                attempt = saved.get("attempt_id")
                if (saved.get("format") != "v4-response/1" or path.name != digest(metadata) + ".json"
                        or not isinstance(attempt, str) or UUID(attempt).hex != attempt):
                    raise ValueError("invalid exhausted request identity")
                matches[target] = saved
        if len(matches) != 1:
            raise ValueError("expected exactly one exhausted request for this run and conversation")
        directory, saved = next(iter(matches.items()))
        with lifecycle_lock(directory):
            state_path = directory / "session.json"
            if not state_path.exists():
                marker = replay_marker(directory)
                if marker is None:
                    raise ValueError("exhausted session state is missing")
                state_path = Path(marker["archive_directory"]) / "session.json"
            if state_path.exists():
                state = json.loads(state_path.read_text())
                self._validate_replay_binding(saved, state, state_path.parent)
                if str(state.get("conversation_id", "")).lower() != conversation_id:
                    raise ValueError("session generation changed; refuse a second replay")
                if saved["model"] != "antigravity/" + str(state.get("model")):
                    raise ValueError("exhausted session model differs from saved request")
                used, maximum = state.get("continuations"), state.get("max_continuations", 2)
                elapsed, budget = state.get("elapsed_seconds"), state.get("budget_seconds")
                count_exhausted = type(used) is int and type(maximum) is int and used >= maximum
                time_exhausted = (type(elapsed) in (int, float) and type(budget) in (int, float)
                                  and elapsed >= budget)
                if not count_exhausted and not time_exhausted:
                    raise ValueError("session limits are no longer exhausted; continue the existing conversation")
            archive = abandon_session(directory, reason=reason)
            authorization = replay_marker(directory)
        return dict(conversation_id=conversation_id, task_id=saved["task_id"], model=saved["model"],
                    session_directory=str(directory), archive_directory=str(archive),
                    generation=authorization["generation"], reason=authorization["reason"])

    def _validate_replay_binding(self, saved, state, directory):
        request = json.loads((directory / "request.json").read_text())
        wire = request.get("prompt")
        if (not isinstance(wire, str) or request.get("model") != state.get("model")
                or hashlib.sha256(wire.encode()).hexdigest() != state.get("prompt_sha256")
                or (saved.get("agent_policy_sha256") is not None
                    and saved["agent_policy_sha256"] != state.get("agent_template_sha256"))):
            raise ValueError("exhausted session request binding differs from saved request")
        # A schema footer changes the wire hash, but not the journal's original
        # prompt hash. Validate the exact framework footer, never infer prose.
        if digest(wire) == saved.get("prompt_hash"):
            return
        if saved.get("response_schema_hash") and wire.endswith("\n</response-schema>"):
            _, separator, encoded = wire.rpartition("\n<response-schema>\n")
            if separator:
                encoded = encoded.removesuffix("\n</response-schema>")
                schema = json.loads(encoded)
                footer = structured_prompt("", encoded)
                if (wire.endswith(footer) and digest(schema) == saved["response_schema_hash"]
                        and digest(wire[:-len(footer)]) == saved.get("prompt_hash")):
                    return
        raise ValueError("exhausted session prompt differs from saved request")

    def _request_locked(self, client, prompt, response_schema, explicit_resume, session_directory, metadata, relative):
        can_use_session = callable(getattr(client, "complete_in_session", None))
        # Fail before starting paid work if the output directory cannot be
        # written. An in-flight record never counts as a completed response.
        attempt = uuid4().hex
        started = time.monotonic()
        prepare = getattr(client, "prepare_session", None)
        if callable(prepare):
            prepare(prompt, response_schema, session_dir=session_directory)
        authorization = replay_marker(session_directory) if can_use_session else None
        timing = dict(started_at=datetime.now(timezone.utc).isoformat(),
                      replay_generation=authorization["generation"] if authorization else None)
        write_json(self.directory / relative, {**metadata, **timing, "session_directory": str(session_directory.resolve()), "attempt_id": attempt, "status": "in_flight"})
        try:
            result = request_completion(client, prompt, response_schema, session_dir=session_directory, resume=explicit_resume)
        except AntigravityError as exc:
            exc = exc.with_context(request_context=dict(task_id=metadata["task_id"],
                session_directory=str(session_directory.resolve()), layer=metadata.get("review", {}).get("layer", "role"),
                claim_ids=list(metadata.get("review", {}).get("task_hashes", {}))))
            failed = {**metadata, **timing, "session_directory": str(session_directory.resolve()), "elapsed_seconds": round(time.monotonic() - started, 3),
                      "attempt_id": attempt, "status": "failed", **exc.details()}
            write_json(self.directory / "responses" / "archive" / (attempt + ".json"), failed)
            write_json(self.directory / relative, failed)
            raise exc from None
        received = {**metadata, **timing, "session_directory": str(session_directory.resolve()), "elapsed_seconds": round(time.monotonic() - started, 3),
                    "attempt_id": attempt, "status": "received", "response": result}
        # Keep previous attempts for audit, including explicitly retried roles.
        write_json(self.directory / "responses" / "archive" / (attempt + ".json"), received)
        write_json(self.directory / relative, received)
        return result
