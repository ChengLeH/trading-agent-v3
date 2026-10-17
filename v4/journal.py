"""Durable run progress and per-request responses, outside process temp storage."""
import json
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4
from pathlib import Path
from .checkpoint import save_checkpoint, write_json
from .schema import digest
from .cloud import request_completion
from .agy_session import AntigravityError
from .session_replay import replay_marker, lifecycle_lock


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

    def complete(self, runner, client, task_id, prompt, *, reuse_role_output=False, response_schema=None, review_context=None):
        metadata = dict(format="v4-response/1", run_id=runner.run_id, model=client.identity,
                        task_id=task_id, prompt_hash=digest(prompt))
        if review_context is not None:
            from .agy_session import load_agent_profile
            metadata["review"] = review_context
            metadata["agent_policy_sha256"] = (load_agent_profile()[1] if callable(getattr(client, "complete_in_session", None)) else "external")
        if response_schema is not None:
            metadata["response_schema_hash"] = digest(response_schema)
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
                                               conversation_id=saved.get("conversation_id"))
                    origin = Path(saved.get("session_directory", directory / "sessions" / digest(metadata)))
                    if can_use_session and (origin / "session.json").is_file() and failure.code in {"session_pending", "missing_init", "cli_start_failed", "invalid_stream", "invalid_json", "continuation_exhausted"}:
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
            failed = {**metadata, **timing, "session_directory": str(session_directory.resolve()), "elapsed_seconds": round(time.monotonic() - started, 3),
                      "attempt_id": attempt, "status": "failed", **exc.details()}
            write_json(self.directory / "responses" / "archive" / (attempt + ".json"), failed)
            write_json(self.directory / relative, failed)
            raise
        received = {**metadata, **timing, "session_directory": str(session_directory.resolve()), "elapsed_seconds": round(time.monotonic() - started, 3),
                    "attempt_id": attempt, "status": "received", "response": result}
        # Keep previous attempts for audit, including explicitly retried roles.
        write_json(self.directory / "responses" / "archive" / (attempt + ".json"), received)
        write_json(self.directory / relative, received)
        return result
