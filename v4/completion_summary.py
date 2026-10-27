"""Small, non-destructive completion receipts for host and subagent handoffs."""
import hashlib
import json
import os
from pathlib import Path
import stat
from collections.abc import Mapping


def _read_artifact(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"result artifact must be a regular file: {path}")
            raw = stream.read()
    except OSError as exc:
        raise ValueError(f"result artifact is missing or unsafe: {path}") from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"result artifact is not valid JSON: {path}") from exc
    if not isinstance(result, dict):
        raise ValueError("result artifact must contain a JSON object")
    return result, str(path.resolve()), raw


def _nonnegative_count(value):
    return value if type(value) is int and value >= 0 else None


def _reported_count(result, field, id_fields, error_fields=()):
    value = _nonnegative_count(result.get(field))
    if value is not None:
        return value
    counts = result.get("counts")
    if isinstance(counts, Mapping):
        value = _nonnegative_count(counts.get(field))
        if value is not None:
            return value
    for key in id_fields:
        rows = result.get(key)
        if isinstance(rows, (list, tuple, set)):
            if any(not isinstance(row, str) or not row for row in rows):
                raise ValueError(f"{key} must contain nonempty string IDs")
            return len(set(rows))
    for key in error_fields:
        rows = result.get(key)
        if isinstance(rows, Mapping):
            return len(rows)
        if isinstance(rows, list):
            return len(rows)
    return None


def _receipt_row(value, *, global_receipt=False):
    if not isinstance(value, Mapping):
        return False
    input_hash = value.get("input_hash")
    model = value.get("model")
    if (not isinstance(input_hash, str) or len(input_hash) != 64
            or any(char not in "0123456789abcdef" for char in input_hash)
            or not isinstance(model, str) or not model.strip()):
        return False
    if global_receipt:
        report_hash = value.get("report_hash")
        return (isinstance(report_hash, str) and len(report_hash) == 64
                and all(char in "0123456789abcdef" for char in report_hash)
                and type(value.get("coverage_rounds")) is int
                and value["coverage_rounds"] > 0
                and isinstance(value.get("findings"), list))
    return isinstance(value.get("result"), Mapping)


def _accepted_receipt_count(receipts):
    """Count receipt rows supplied by the caller after canonical validation."""
    grouped = (isinstance(receipts, Mapping) and bool(receipts)
               and set(receipts).issubset({"atomic", "deep", "global", "global_review"}))
    if grouped:
        for key, value in receipts.items():
            if key in ("atomic", "deep"):
                if (not isinstance(value, Mapping)
                        or any(not _receipt_row(row) for row in value.values())):
                    grouped = False
                    break
            elif value is not None and not _receipt_row(value, global_receipt=True):
                grouped = False
                break
    if grouped:
        total = 0
        for key in ("atomic", "deep"):
            rows = receipts.get(key, {})
            if rows is None:
                continue
            if not isinstance(rows, Mapping):
                raise ValueError(f"accepted {key} receipts must be an object")
            for row in rows.values():
                if not _receipt_row(row):
                    raise ValueError(f"accepted {key} receipt has an invalid shape")
                total += 1
        global_row = receipts.get("global", receipts.get("global_review"))
        if global_row is not None:
            if not _receipt_row(global_row, global_receipt=True):
                raise ValueError("accepted global receipt has an invalid shape")
            total += 1
        return total
    if isinstance(receipts, Mapping):
        rows = receipts.values()
    elif isinstance(receipts, (list, tuple)):
        rows = receipts
    else:
        raise ValueError("accepted_receipts must be an object or list")
    total = 0
    for row in rows:
        if not _receipt_row(row):
            raise ValueError("accepted receipt has an invalid shape")
        total += 1
    return total


def _intervention_count(result):
    value = _nonnegative_count(result.get("intervention_count"))
    if value is not None:
        return value
    rows = result.get("interventions")
    if isinstance(rows, (list, tuple)):
        return len(rows)
    history = result.get("revision_history")
    if history is None and isinstance(result.get("sidecar"), Mapping):
        history = result["sidecar"].get("revision_history")
    if isinstance(history, (list, tuple)):
        return len(history)
    return None


def _has_supplied_verdict(result):
    if "verdict" in result or "response" in result:
        return True
    rows = result.get("results")
    if isinstance(rows, list) and any(isinstance(row, Mapping) and "verdict" in row for row in rows):
        return True
    sidecar = result.get("sidecar")
    if isinstance(sidecar, Mapping):
        for layer in ("atomic_reviews", "deep_reviews"):
            receipts = sidecar.get(layer)
            if isinstance(receipts, Mapping) and any(
                    isinstance(row, Mapping) and isinstance(row.get("result"), Mapping)
                    and "verdict" in row["result"] for row in receipts.values()):
                return True
    return False


def _safe_identity(value):
    if isinstance(value, str) and 1 <= len(value) <= 128 and all(
            c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-" for c in value):
        return value
    return None


def _safe_status(value):
    if isinstance(value, str) and 1 <= len(value) <= 64 and all(
            c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for c in value):
        return value
    return "unrecognized" if isinstance(value, str) else None


def summarize_completion(result, *, artifact_path=None, accepted_receipts=None):
    """Return counts and artifact identity, omitting prompts, findings, and evidence.

    ``accepted_receipts`` must come from the canonical workflow after it has
    rebuilt receipts from the raw response archives. Without that argument,
    result fields are reported as unverified and never imply accepted work.
    """
    if isinstance(result, (str, os.PathLike)):
        if artifact_path is not None:
            raise ValueError("artifact_path is implicit when result is a path")
        result, saved_path, raw = _read_artifact(result)
    else:
        if not isinstance(result, Mapping):
            raise ValueError("result must be an object or a path to a JSON object")
        try:
            raw = json.dumps(result, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("result object must be JSON serializable") from exc
        saved_path = None
        if artifact_path is not None:
            on_disk, saved_path, artifact_raw = _read_artifact(artifact_path)
            if dict(result) != on_disk:
                raise ValueError("result object differs from the supplied result artifact")
            raw = artifact_raw

    accepted_count = None if accepted_receipts is None else _accepted_receipt_count(accepted_receipts)
    completed_count = (accepted_count if accepted_count is not None else
                       _reported_count(result, "completed_count",
                                       ("completed_item_ids", "completed_claim_ids", "completed_ids"),
                                       ("accepted_receipts",)))
    failed_count = _reported_count(result, "failed_count",
                                   ("failed_item_ids", "failed_claim_ids", "failed_ids"),
                                   ("role_errors", "review_errors", "blocked_claims"))
    has_verdict = _has_supplied_verdict(result)
    receipt_status = ("accepted" if accepted_count else "none") if accepted_count is not None else "unverified"
    if has_verdict:
        verdict_status = "unverified_supplied_verdict"
    elif accepted_count:
        verdict_status = "accepted_receipts"
    else:
        verdict_status = "not_reported"
    completion_basis = ("accepted_receipts" if accepted_count is not None else
                        "unverified_result_fields" if completed_count is not None else "not_recorded")

    return dict(
        format="v4-completion-summary/1",
        identity=dict(task_id=_safe_identity(result.get("task_id")),
                      packet_id=_safe_identity(result.get("packet_id")),
                      run_id=_safe_identity(result.get("run_id"))),
        completed_count=completed_count,
        failed_count=failed_count,
        intervention_count=_intervention_count(result),
        accepted_receipt_count=accepted_count,
        completion_basis=completion_basis,
        failed_count_basis=("unverified_result_fields" if failed_count is not None else "not_recorded"),
        receipt_status=receipt_status,
        verdict_status=verdict_status,
        reported_status=_safe_status(result.get("status")),
        result_artifact=dict(path=saved_path, sha256=hashlib.sha256(raw).hexdigest(),
                             sha256_basis="artifact_bytes" if saved_path else "canonical_result_json"),
    )
