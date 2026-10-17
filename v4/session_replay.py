"""Explicit paid-replay authorization with durable audit and crash recovery.

Normal requests never call abandon_session. Authorization archives one paused
session; the marker permits exactly one replacement generation at its old path.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import UUID, uuid4
from .storage import write_json
from .response_json import normalize_json_response

_HELD = threading.local()

_BINDING_KEYS = ('model', 'prompt_sha256', 'agent_template_sha256', 'format', 'continuation_protocol')


@contextmanager
def lifecycle_lock(directory):
    directory = Path(directory).resolve()
    directory.parent.mkdir(parents=True, exist_ok=True)
    held = getattr(_HELD, 'locks', {})
    if getattr(_HELD, 'pid', os.getpid()) != os.getpid():
        for handle in held.values():
            handle.close()
        held = {}
    _HELD.locks, _HELD.pid = held, os.getpid()
    if directory in held:
        yield directory
        return
    # This lock stays in place when the session directory is archived.
    with (directory.parent / ('.' + directory.name + '.lifecycle.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held[directory] = handle
        try:
            yield directory
        finally:
            held.pop(directory, None)
            fcntl.flock(handle, fcntl.LOCK_UN)


def _marker_path(directory):
    return directory.parent / ('.' + directory.name + '.replay.json')


def replay_marker(directory):
    directory = Path(directory).resolve()
    path = _marker_path(directory)
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('format') != 'v4-authorized-replay/1'
            or value.get('source_directory') != str(directory)
            or value.get('phase') not in {'prepared', 'committed'}
            or not isinstance(value.get('reason'), str) or not value['reason'].strip()
            or not isinstance(value.get('generation'), str) or UUID(value['generation']).hex != value['generation']):
        raise ValueError('invalid replay authorization')
    archive = Path(value['archive_directory'])
    if archive.parent != directory.parent or not archive.name.startswith(directory.name + '.abandoned.'):
        raise ValueError('invalid replay archive location')
    return value


def recover_authorized_replay(directory, *, binding=None):
    """Called with lifecycle_lock held, before loading/creating session state."""
    directory = Path(directory).resolve()
    marker = replay_marker(directory)
    if marker is None:
        return None
    if binding is not None and marker['binding'] != {k: binding[k] for k in _BINDING_KEYS}:
        raise ValueError('replay authorization belongs to another request')
    archive = Path(marker['archive_directory'])
    if marker['phase'] == 'prepared' and not archive.exists():
        # Keep compatibility with an older worker that holds the internal lock.
        with (directory / 'request.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                raw = (directory / 'session.json').read_bytes()
                if hashlib.sha256(raw).hexdigest() != marker['old_state_sha256']:
                    raise ValueError('session changed before replay archive committed')
                os.rename(directory, archive)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    raw = (archive / 'session.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != marker['old_state_sha256']:
        raise ValueError('replay audit state changed')
    if marker['phase'] == 'prepared':
        marker['phase'] = 'committed'
        write_json(_marker_path(directory), marker)  # fsync also commits rename
    return marker


def abandon_session(session_dir, *, reason):
    """Authorize ONE fresh paid request; preserve all original files for audit.

    Refuses completed sessions, live processes and unconfirmed delivery. Call
    only with explicit operator authorization. Return the preserved archive.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('explicit paid-replay reason required')
    with lifecycle_lock(session_dir) as directory:
        previous = recover_authorized_replay(directory)
        if not (directory / 'session.json').exists():
            if previous is not None:
                return Path(previous['archive_directory'])  # idempotent after crash
            raise ValueError('session state missing')
        with (directory / 'request.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                raw = (directory / 'session.json').read_bytes()
                data = json.loads(raw)
                if (not isinstance(data, dict) or data.get('format') != 'v4-agy-session/1'
                        or type(data.get('request_sent')) is not bool
                        or type(data.get('turn_acknowledged')) is not bool
                        or type(data.get('max_continuations', 2)) is not int or data.get('max_continuations', 2) < 0
                        or type(data.get('continuations')) is not int or not 0 <= data['continuations'] <= data.get('max_continuations', 2)
                        or any(not isinstance(data.get(k), str) or not data[k] for k in _BINDING_KEYS)):
                    raise ValueError('invalid session state; refuse paid replay')
                if data['request_sent']:
                    try:
                        UUID(data.get('conversation_id'))
                    except (ValueError, TypeError, AttributeError):
                        raise ValueError('conversation identity unconfirmed; refuse paid replay') from None
                if data.get('status') not in {'paused', 'failed'}:
                    raise ValueError('only paused or failed sessions may be abandoned; complete sessions are preserved')
                if data.get('request_sent') and not data.get('turn_acknowledged'):
                    raise ValueError('delivery unconfirmed; refuse paid replay')
                events = directory / 'events.jsonl'
                if events.exists():
                    rows = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
                    if any(not isinstance(row, dict) for row in rows):
                        raise ValueError('invalid session event audit')
                    active = [row for row in rows if row.get('v4_attempt_id') == data.get('active_attempt_id')]
                    terminal = [row for row in active if row.get('event') == 'result']
                    if terminal:
                        if len(terminal) != 1 or active[-1] != terminal[0]:
                            raise ValueError('ambiguous terminal audit; refuse paid replay')
                        result = terminal[0].get('result', {})
                        if isinstance(result, dict) and result.get('status') == 'SUCCESS':
                            try:
                                normalize_json_response(result.get('response'))
                            except (ValueError, TypeError):
                                pass  # explicit replay of an unparseable answer is allowed
                            else:
                                raise ValueError('complete result already saved; recover it instead of paid replay')
                pid = data.get('process_id')
                if pid is not None:
                    if type(pid) is not int or pid <= 0:
                        raise ValueError('invalid process identity')
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise ValueError('session process is still alive')
                generation = uuid4().hex
                archive = directory.with_name(directory.name + '.abandoned.' + generation)
                marker = dict(format='v4-authorized-replay/1', phase='prepared', generation=generation,
                              source_directory=str(directory), archive_directory=str(archive),
                              old_state_sha256=hashlib.sha256(raw).hexdigest(),
                              binding={k: data[k] for k in _BINDING_KEYS},
                              old_attempt_id=data.get('active_attempt_id'), reason=reason,
                              authorized_at=datetime.now(timezone.utc).isoformat())
                write_json(directory / 'replay-authorization.json', marker)
                write_json(_marker_path(directory), marker)
                os.rename(directory, archive)
                marker['phase'] = 'committed'
                write_json(_marker_path(directory), marker)
                return archive
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
