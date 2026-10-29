"""Atomic, fsynced JSON writes shared by state and cloud transport."""
from pathlib import Path
import json
import os
import tempfile


def is_ephemeral_directory(path):
    """Identify known OS temporary roots; other paths are not proof of durability."""
    target = Path(path).expanduser().resolve()
    roots = {Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve(), Path('/var/tmp').resolve()}
    return any(target == root or root in target.parents for root in roots)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = f.name
            json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)

