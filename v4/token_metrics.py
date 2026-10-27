"""Read-only prompt and storage accounting for native work directories.

This module never opens a workflow transaction, creates a lock, writes a
checkpoint, or calls a model. Token counters remain unknown unless runtime
artifacts actually persist them; prompt bytes are never converted to tokens.
"""
import json
import os
from pathlib import Path
import stat

from .agent_queue import LEGACY_STATE_FORMAT, STATE_FORMAT
from .native_archive import NativeArchive, _PROMPT_FRAME


def _real_directory(path, *, optional=False):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if optional:
            return False
        raise ValueError(f"native work directory is missing: {path}") from None
    except OSError as exc:
        raise ValueError(f"native work directory is unreadable: {path}") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError(f"expected a real directory, not a symlink or file: {path}")
    return True


def _read_regular_file(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"expected a regular file: {path}")
            return stream.read()
    except OSError as exc:
        raise ValueError(f"file is missing or unsafe: {path}") from exc


def _directory_file_bytes(directory):
    """Count regular-file payload bytes in one expected, flat artifact folder."""
    if not _real_directory(directory, optional=True):
        return 0, 0
    total = 0
    count = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"archive contains a non-regular entry: {entry.path}")
                total += info.st_size
                count += 1
    except OSError as exc:
        raise ValueError(f"archive directory is unreadable: {directory}") from exc
    return total, count


def _decode_state(raw, state_path):
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"native work state is not valid UTF-8 JSON: {state_path}") from exc
    if not isinstance(state, dict):
        raise ValueError("native work state must be a JSON object")
    state_format = state.get("format")
    if state_format not in (LEGACY_STATE_FORMAT, STATE_FORMAT):
        raise ValueError(f"unsupported native work state format: {state_format!r}")
    packets = state.get("packets")
    if not isinstance(packets, dict):
        raise ValueError("native work state packets must be an object")
    if state_format == LEGACY_STATE_FORMAT and "_native_archive" in state:
        raise ValueError("legacy inline state cannot contain a cold archive manifest")
    if state_format == STATE_FORMAT:
        # Hydration mutates only the parsed in-memory copy. NativeWorkQueue.transaction()
        # is deliberately avoided because it creates a lock and writes the state.
        state = NativeArchive(state_path.parent).hydrate(state)
    return state, state_format


def _count_prompt_prefixes(prompts):
    prefix_counts = {}
    eligible = 0
    for prompt in prompts:
        marker_at = prompt.find(_PROMPT_FRAME)
        if marker_at < 0:
            continue
        boundary = marker_at + len(_PROMPT_FRAME)
        prefix = prompt[:boundary].encode("utf-8")
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
        eligible += 1
    repeated = [(prefix, count) for prefix, count in prefix_counts.items() if count > 1]
    return dict(
        eligible_prompt_count=eligible,
        unique_prefix_count=len(prefix_counts),
        repeated_prefix_groups=len(repeated),
        repeated_prefix_occurrences=sum(count - 1 for _, count in repeated),
        deduplicable_prefix_bytes=sum(len(prefix) * (count - 1) for prefix, count in repeated),
    )


def _wire_request_rows(value, packet_id):
    if value is None:
        return []
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        if isinstance(value.get("requests"), list) and "prompt" not in value:
            rows = value["requests"]
        elif "prompt" in value or "request_id" in value:
            rows = [value]
        else:
            rows = list(value.values())
    else:
        raise ValueError(f"packet {packet_id!r} wire_requests must be an object or array")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"packet {packet_id!r} wire_requests entries must be objects")
    return rows


def audit_native_work(work_dir):
    """Measure an existing native work directory without modifying it.

    Prompt byte totals are UTF-8 bytes grouped by packet ``kind``. Duplicate
    prefixes count only the exact frame split used by ``NativeArchive``.
    """
    root = Path(work_dir).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    _real_directory(root)
    state_path = root / "agent-state.json"
    raw_state = _read_regular_file(state_path)
    state, state_format = _decode_state(raw_state, state_path)

    by_kind = {}
    wire_requests_by_kind = {}
    prompts = []
    packet_count = item_count = prompt_count = prompt_bytes = 0
    wire_request_prompt_bytes = 0
    for packet_id, packet in state["packets"].items():
        if not isinstance(packet, dict):
            raise ValueError(f"packet {packet_id!r} must be an object")
        items = packet.get("items")
        if not isinstance(items, dict):
            raise ValueError(f"packet {packet_id!r} items must be an object")
        kind = packet.get("kind")
        if not isinstance(kind, str) or not kind:
            kind = "unknown"
        group = by_kind.setdefault(kind, dict(packet_count=0, item_count=0,
                                              prompt_count=0, prompt_bytes=0))
        packet_count += 1
        item_count += len(items)
        group["packet_count"] += 1
        group["item_count"] += len(items)
        for item_id, item in items.items():
            if not isinstance(item, dict):
                raise ValueError(f"packet {packet_id!r} item {item_id!r} must be an object")
            prompt = item.get("prompt")
            if prompt is None:
                continue
            if not isinstance(prompt, str):
                raise ValueError(f"packet {packet_id!r} item {item_id!r} prompt must be a string")
            size = len(prompt.encode("utf-8"))
            prompts.append(prompt)
            prompt_count += 1
            prompt_bytes += size
            group["prompt_count"] += 1
            group["prompt_bytes"] += size
        wire_rows = _wire_request_rows(packet.get("wire_requests", []), packet_id)
        wire_stats = wire_requests_by_kind.setdefault(
            kind, dict(request_count=0, prompt_count=0, prompt_bytes=0))
        wire_stats["request_count"] += len(wire_rows)
        for request in wire_rows:
            wire_prompt = request.get("prompt")
            if wire_prompt is None:
                continue
            if not isinstance(wire_prompt, str):
                raise ValueError(f"packet {packet_id!r} wire request prompt must be a string")
            size = len(wire_prompt.encode("utf-8"))
            wire_stats["prompt_count"] += 1
            wire_stats["prompt_bytes"] += size
            wire_request_prompt_bytes += size

    native_archive_bytes, native_archive_files = _directory_file_bytes(root / "native-archive")
    response_archive_bytes, response_archive_files = _directory_file_bytes(root / "responses" / "archive")
    task_delivery_bytes, task_delivery_files = _directory_file_bytes(root / "task-delivery" / "objects")
    archive_bytes = native_archive_bytes + response_archive_bytes
    return dict(
        format="v4-token-metrics/1",
        work_dir=str(root.resolve()),
        state_format=state_format,
        counts=dict(packet_count=packet_count, item_count=item_count, prompt_count=prompt_count),
        prompt_bytes=prompt_bytes,
        by_kind=by_kind,
        wire_requests_by_kind=wire_requests_by_kind,
        wire_request_prompt_bytes=wire_request_prompt_bytes,
        physical_bytes=dict(
            state_file_bytes=len(raw_state),
            native_archive_bytes=native_archive_bytes,
            native_archive_file_count=native_archive_files,
            response_archive_bytes=response_archive_bytes,
            response_archive_file_count=response_archive_files,
            task_delivery_object_bytes=task_delivery_bytes,
            task_delivery_object_file_count=task_delivery_files,
            archive_bytes=archive_bytes,
            state_plus_archives_file_bytes=len(raw_state) + archive_bytes,
            state_plus_archives_and_task_delivery_file_bytes=(
                len(raw_state) + archive_bytes + task_delivery_bytes),
        ),
        duplicate_prompt_prefixes=_count_prompt_prefixes(prompts),
        usage_tokens=dict(input_tokens=None, output_tokens=None, total_tokens=None,
                          source="not_recorded_in_native_work_artifacts"),
    )
