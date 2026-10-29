"""Supervise one native secondary dispatch without assuming model liveness."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from .agent_dispatch import dispatch_status
from .storage import is_ephemeral_directory, write_json


def supervise_secondary(work_dir, model, *, max_workers=4, max_items=16, batch_size=4,
                        lease_seconds=3600, startup_lease_seconds=60,
                        poll_seconds=30, attention_after_seconds=300,
                        allow_ephemeral=False, single_packet=False):
    """Run the existing dispatcher as a child and persist its launch and exit.

    A stale heartbeat is an observation, not proof a model has stopped. This
    function never kills or reissues a child. If the supervisor itself dies,
    its last persisted state remains evidence and an external monitor decides
    how to proceed after checking the child and provider.
    """
    root = Path(work_dir).expanduser().resolve()
    if not (root / 'agent-state.json').is_file():
        raise ValueError('an initialized native work directory is required')
    if is_ephemeral_directory(root) and not allow_ephemeral:
        raise ValueError('supervisor requires a persistent work directory')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('secondary model is required')
    if type(max_workers) is not int or not 1 <= max_workers <= 8:
        raise ValueError('max_workers must be 1..8')
    if type(max_items) is not int or max_items < 1 or type(batch_size) is not int or not 1 <= batch_size <= 16:
        raise ValueError('invalid max_items or batch_size')
    if type(single_packet) is not bool:
        raise ValueError('single_packet must be boolean')
    import math
    for value in (lease_seconds, startup_lease_seconds, poll_seconds, attention_after_seconds):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('supervisor time settings must be finite and positive')
    run_id = uuid4().hex
    directory = root / 'secondary-supervision' / run_id
    directory.mkdir(parents=True, mode=0o700)
    state_path = directory / 'status.json'
    stdout_path, stderr_path = directory / 'stdout.json', directory / 'stderr.log'
    started_at = time.time()
    state = dict(supervision_id=run_id, status='starting', started_at=started_at,
                 updated_at=started_at, child_pid=None, exit_code=None,
                 status_path=str(state_path), attention_events=[],
                 stdout_path=str(stdout_path), stderr_path=str(stderr_path),
                 needs_attention=False, reason='launch outcome not yet known')
    write_json(state_path, state)
    command = [sys.executable, '-B', '-m', 'v4.agent_cli', 'dispatch-secondary', '--direct',
        '--work-dir', str(root), '--agy-model', model, '--max-workers', str(max_workers),
        '--max-items', str(max_items), '--batch-size', str(batch_size),
        '--lease-seconds', str(lease_seconds), '--startup-lease-seconds', str(startup_lease_seconds),
        '--progress-interval-seconds', str(poll_seconds)]
    if single_packet:
        command.append('--single-packet')
    try:
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            os.close(stdout_fd)
            raise
    except OSError as exc:
        state.update(status='start_failed', updated_at=time.time(), reason=type(exc).__name__)
        write_json(state_path, state)
        return dict(status='fallback_to_main', reason='secondary dispatcher logs could not be created', supervision=state)
    try:
        with os.fdopen(stdout_fd, 'w', encoding='utf-8') as stdout, os.fdopen(stderr_fd, 'w', encoding='utf-8') as stderr:
            try:
                child = subprocess.Popen(command, stdout=stdout, stderr=stderr, text=True)
            except OSError as exc:
                state.update(status='start_failed', updated_at=time.time(), reason=type(exc).__name__)
                write_json(state_path, state)
                return dict(status='fallback_to_main', reason='secondary dispatcher did not start', supervision=state)
            state.update(status='running', child_pid=child.pid, updated_at=time.time(),
                         reason='child started; provider acceptance is not confirmed')
            print(json.dumps(dict(status='supervisor_started', supervision_id=run_id,
                                  child_pid=child.pid, status_path=str(state_path))),
                  file=sys.stderr, flush=True)
            write_json(state_path, state)
            try:
                while True:
                    try:
                        exit_code = child.wait(timeout=poll_seconds)
                        break
                    except subprocess.TimeoutExpired:
                        now = time.time()
                        previous_attention = state.get('needs_attention', False)
                        try:
                            observed = dispatch_status(root, attention_after_seconds=attention_after_seconds)
                            matching = [row for row in observed['dispatchers'] if row.get('process_id') == child.pid
                                        and row.get('updated_at', 0) >= started_at]
                            heartbeat = max((row.get('updated_at', 0) for row in matching), default=started_at)
                            attention = now - heartbeat >= attention_after_seconds
                            state.update(updated_at=now, last_dispatch_heartbeat=heartbeat,
                                         needs_attention=attention,
                                         reason=('dispatcher heartbeat is stale; inspect process and provider'
                                                 if attention else 'child is running; provider liveness is unknown'))
                        except (OSError, ValueError, KeyError):
                            state.update(updated_at=now, needs_attention=True,
                                         reason='dispatch observations are unavailable')
                        if state['needs_attention'] and not previous_attention:
                            state['attention_events'].append(dict(at=now, reason=state['reason']))
                        write_json(state_path, state)
                        if state['needs_attention']:
                            print(json.dumps(dict(status='attention', supervision_id=run_id,
                                                  child_pid=child.pid, reason=state['reason'])),
                                  file=sys.stderr, flush=True)
            except BaseException:
                state.update(status='supervisor_interrupted', updated_at=time.time(),
                             needs_attention=True, reason='child outcome unknown; verify before retry')
                write_json(state_path, state)
                raise
    except BaseException:
        # The child is intentionally not killed: it may have already sent a
        # request, and destroying it could discard a response or cause repeats.
        raise
    try:
        result = json.loads(stdout_path.read_text(encoding='utf-8'))
        if not isinstance(result, dict) or not isinstance(result.get('status'), str):
            raise ValueError('dispatcher result has no status')
    except (OSError, ValueError):
        result = dict(status='fallback_to_main', reason='dispatcher result is unavailable or invalid')
    if exit_code != 0:
        result = dict(status='fallback_to_main', reason='secondary dispatcher exited unsuccessfully',
                      child_result=result)
    state.update(status='exited', updated_at=time.time(), exit_code=exit_code,
                 needs_attention=exit_code != 0 or result['status'] in ('fallback_to_main', 'reviews_in_flight'),
                 reason='child exited; inspect the dispatch result')
    write_json(state_path, state)
    result['supervision'] = state
    return result
