"""Durable run progress and per-request responses, outside process temp storage."""
import json
from uuid import UUID, uuid4
from pathlib import Path
from .checkpoint import save_checkpoint, write_json
from .schema import digest
from .cloud import request_completion
from .agy_session import AntigravityError


class RunJournal:
    def __init__(self, directory, *, resume_from=None, retry_roles=()):
        self.directory = Path(directory)
        self.read_directories = [self.directory]
        self.retry_roles = set(retry_roles)
        if resume_from is not None:
            self.read_directories.append(Path(resume_from).parent)

    def checkpoint(self, runner):
        save_checkpoint(runner, self.directory / "checkpoint.json")

    def complete(self, runner, client, task_id, prompt, *, reuse_role_output=False, response_schema=None):
        metadata = dict(format="v4-response/1", run_id=runner.run_id, model=client.identity,
                        task_id=task_id, prompt_hash=digest(prompt))
        if response_schema is not None:
            metadata["response_schema_hash"] = digest(response_schema)
        relative = Path("responses") / (digest(metadata) + ".json")
        if reuse_role_output and task_id not in self.retry_roles:
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
                if saved.get("status") == "in_flight":
                    archive = directory / "responses" / "archive" / (attempt + ".json")
                    if not archive.exists():
                        break  # this newer attempt must not replay an older source directory
                    saved = json.loads(archive.read_text(encoding="utf-8"))
                    if any(saved.get(k) != v for k, v in {**metadata, "attempt_id": attempt}.items()):
                        raise ValueError("saved archive does not match current attempt")
                if saved.get("status") == "received":
                    write_json(self.directory / "responses" / "archive" / (attempt + ".json"), saved)
                    write_json(self.directory / relative, saved)
                    return saved["response"]
                if saved.get("status") == "failed":
                    failure = AntigravityError(saved.get("error_code"), retryable=saved.get("retryable"),
                                               exit_code=saved.get("exit_code"), attempts=saved.get("attempts"))
                    if not failure.retryable:
                        raise failure
                    break  # transient failure; never replay an older answer
                raise ValueError("invalid saved response status")
        # Fail before starting paid work if the output directory cannot be
        # written. An in-flight record never counts as a completed response.
        attempt = uuid4().hex
        write_json(self.directory / relative, {**metadata, "attempt_id": attempt, "status": "in_flight"})
        try:
            result = request_completion(client, prompt, response_schema)
        except AntigravityError as exc:
            failed = {**metadata, "attempt_id": attempt, "status": "failed", **exc.details()}
            write_json(self.directory / "responses" / "archive" / (attempt + ".json"), failed)
            write_json(self.directory / relative, failed)
            raise
        received = {**metadata, "attempt_id": attempt, "status": "received", "response": result}
        # Keep previous attempts for audit, including explicitly retried roles.
        write_json(self.directory / "responses" / "archive" / (attempt + ".json"), received)
        write_json(self.directory / relative, received)
        return result
