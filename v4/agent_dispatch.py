"""Optional AGY secondary review scheduling over the Native host-owned V4 queue.

No global state lives here. AGY is only an acceleration worker for atomic/deep
review items; a failed request is left in the durable queue for the host Agent.
The host still owns all role generation, P6 global decisions, patches and report.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
import fcntl
import os
import math
import hashlib
import threading
from uuid import uuid4
import time

from .agent_queue import renew_items_in_state

from .cloud import request_completion, structured_prompt
from .response_json import normalize_json_response
from .schema import digest

MAX_SECONDARY_WORKERS = 8


def _append_dispatch_observation(path, row):
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.lseek(fd, 0, os.SEEK_END)
        if size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b'\n':
                os.write(fd, b'\n')
        with os.fdopen(fd, 'a', encoding='utf-8', closefd=False) as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def dispatch_status(directory, *, attention_after_seconds=None):
    """Summarize local observations; an outstanding call may have died or hung."""
    if not directory.is_dir():
        raise ValueError('native work directory does not exist')
    if (attention_after_seconds is not None and
            (isinstance(attention_after_seconds, bool) or not isinstance(attention_after_seconds, (int, float))
             or not math.isfinite(attention_after_seconds) or attention_after_seconds <= 0)):
        raise ValueError('attention threshold must be finite and positive')
    path = directory / 'secondary-dispatch.jsonl'
    attempts = {}
    damaged_records = 0
    if path.exists():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A crash can leave an incomplete append. Retain earlier
                    # observations but report that the log is incomplete.
                    damaged_records += 1
                    if not line.endswith('\n'):
                        break
                    continue
                if not isinstance(row, dict):
                    damaged_records += 1
                    continue
                request_id = row.get('request_id')
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError('dispatch observation missing request ID')
                attempts.setdefault(request_id, {}).update(row)
    now = time.time()
    for row in attempts.values():
        if row.get('status') == 'calling' and isinstance(row.get('started_at'), (int, float)):
            row['seconds_since_call_start'] = max(0, now - row['started_at'])
        if row.get('status') == 'scheduled' and isinstance(row.get('scheduled_at'), (int, float)):
            row['seconds_since_scheduled'] = max(0, now - row['scheduled_at'])
    attention = [row['request_id'] for row in attempts.values()
                 if attention_after_seconds is not None
                 and max(row.get('seconds_since_call_start', 0),
                         row.get('seconds_since_scheduled', 0)) >= attention_after_seconds]
    state_path = directory / 'agent-state.json'
    reservations, attention_reservations = [], []
    state_available = state_path.exists()
    if state_available:
        from .native_archive import NativeArchive
        from .native_scheduling import review_reservations
        state = NativeArchive(directory).hydrate(json.loads(state_path.read_text(encoding='utf-8')))
        reservations = [dict(layer='dependency' if layer == 'global' else layer, **row)
                        for layer in ('atomic', 'deep', 'global')
                        for row in review_reservations(state, layer)]
        for row in reservations:
            claimed = row.get('claimed_at')
            if isinstance(claimed, (int, float)) and not isinstance(claimed, bool):
                row['seconds_since_reservation'] = max(0, now - claimed)
                if attention_after_seconds is not None and row['seconds_since_reservation'] >= attention_after_seconds:
                    attention_reservations.append(row['lease_id'])
    progress_dir = directory / 'secondary-dispatch-progress'
    dispatchers = []
    for progress_path in sorted(progress_dir.glob('*.json')):
        progress = json.loads(progress_path.read_text(encoding='utf-8'))
        if not isinstance(progress, dict):
            raise ValueError('invalid dispatch progress')
        updated = progress.get('updated_at')
        if isinstance(updated, (int, float)) and not isinstance(updated, bool):
            progress['seconds_since_update'] = max(0, now - updated)
            progress['needs_attention'] = bool(attention_after_seconds is not None
                and progress.get('status') == 'running'
                and progress['seconds_since_update'] >= attention_after_seconds)
        dispatchers.append(progress)
    return dict(status='dispatch_observations', attempts=list(attempts.values()), damaged_records=damaged_records,
                dispatchers=dispatchers,
                reservations=reservations, state_available=state_available,
                attention_request_ids=attention, attention_lease_ids=attention_reservations,
                next_action=('inspect executor and provider before any retry or lease release'
                             if attention or attention_reservations or any(p.get('needs_attention') for p in dispatchers) else None),
                interpretation='calling means locally started; provider acceptance and process liveness are unknown')


def restore_secondary_availability(workflow, identity, *, reason):
    """Explicitly restore a quota-disabled secondary after operator verification."""
    if not isinstance(identity, str) or not identity.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError("identity and operator reason are required")
    with workflow.queue.transaction() as state:
        availability = state.setdefault("secondary_availability", {})
        current = availability.get(identity)
        if not current or current.get("status") != "unavailable":
            return False
        availability[identity] = dict(status="available", restored_reason=reason.strip(),
                                      generation=current.get("generation", 0) + 1)
    return True


def dispatch_secondary_reviews(workflow, client, *, max_workers=4, max_items=16,
                               batch_size=1, continuous=False, lease_seconds=3600,
                               startup_lease_seconds=60, progress_interval_seconds=60):
    """Refill bounded worker slots; durable leases prevent host/AGY duplicates.

    Library callers retain single-packet compatibility by default. The CLI
    enables bounded wire batching and refill across packet boundaries. Every
    response is archived as actually received, then normalized per Claim.
    """
    if type(max_workers) is not int or not 1 <= max_workers <= MAX_SECONDARY_WORKERS:
        raise ValueError("max_workers must be 1..8")
    if type(max_items) is not int or max_items < 1:
        raise ValueError("max_items must be a positive integer")
    if (isinstance(startup_lease_seconds, bool) or not isinstance(startup_lease_seconds, (int, float))
            or not math.isfinite(startup_lease_seconds) or startup_lease_seconds <= 0):
        raise ValueError('startup lease must be positive and finite')
    if (isinstance(progress_interval_seconds, bool) or not isinstance(progress_interval_seconds, (int, float))
            or not math.isfinite(progress_interval_seconds) or progress_interval_seconds <= 0):
        raise ValueError('progress interval must be positive and finite')
    identity = getattr(client, "identity", None)
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("secondary client requires a declared identity")
    packet = workflow.next()
    kind = packet.get("kind")
    dependency_mode = (kind == "dependency" or
        (kind == "global" and set(packet.get("items", {})) == {"global_dependency_completion"}))
    if kind not in ("atomic", "deep") and not dependency_mode:
        return dict(status="host_owned", kind=kind, packet=packet,
                    reason="this stage remains with the host Agent")
    if packet.get("blocked") and not (dependency_mode and packet.get("needs_retry")):
        return dict(status="fallback_to_main", kind=kind, packet=packet,
                    warnings=[workflow.record_secondary_event(packet, code='workflow_blocked', requested_identity=identity)],
                    reason="canonical workflow is blocked, host intervention required")
    with workflow.queue.transaction() as state:
        availability = state.get("secondary_availability", {}).get(identity, {})
        disabled = availability.get("status") == "unavailable"
        generation = availability.get("generation", 0)
        if type(generation) is not int or generation < 0:
            raise ValueError("invalid secondary restore generation")
    if disabled:
        return dict(status="fallback_to_main", kind=kind, packet=packet,
                    failures={}, outstanding=packet, warnings=[],
                    reason="secondary quota is unavailable; operator restore is required")
    accepted, failures, leases, telemetry_errors = [], {}, [], []
    attempted_ids = []
    attempt_lock = threading.Lock()
    outstanding_requests = {}
    dispatched = 0
    quota_failure = False
    initial_packet_id = packet.get("packet_id")
    dispatch_id = uuid4().hex
    legacy_limit = min(max_items, len(packet.get("items", {}))) if not dependency_mode else max_items
    limit = max_items if continuous else legacy_limit

    def save_progress(status):
        from .storage import write_json
        try:
            write_json(workflow.directory / 'secondary-dispatch-progress' / (dispatch_id + '.json'), dict(
                dispatch_id=dispatch_id, status=status, updated_at=time.time(), process_id=os.getpid(),
                scheduled_claim_count=dispatched, accepted_claim_count=len(set(accepted)),
                failed_claim_count=len(failures),
                active_request_count=sum(outstanding_requests.values()),
                provider_invocations_confirmed=None))
        except OSError as exc:
            telemetry_errors.append(type(exc).__name__)

    def write_attempt(request, status, **fields):
        # These are local lifecycle observations, not evidence that the
        # provider accepted a request or that a verdict was accepted.
        row = dict(request_id=request['request_id'], lease_id=request['lease_id'],
                   claim_ids=list(request['claim_ids']), executor=identity, status=status, **fields)
        path = workflow.directory / 'secondary-dispatch.jsonl'
        _append_dispatch_observation(path, row)

    def mark_attempt(request, status, **fields):
        try:
            write_attempt(request, status, **fields)
        except OSError as exc:
            # Diagnostics must not discard an otherwise valid model answer.
            # Surface the loss of observability without retaining OS details.
            telemetry_errors.append(type(exc).__name__)

    def answer(claim, request):
        # A short reservation before startup prevents an abandoned unsent
        # task from occupying a full model-length lease. Renew atomically
        # before any provider call; an expired reservation must never send.
        with workflow.queue.transaction() as state:
            started_lease = renew_items_in_state(state, claim['packet_id'], identity,
                                                 claim['lease']['lease_id'], lease_seconds)
            required_items = (claim['lease']['item_ids'] if dependency_mode else request['claim_ids'])
            if not set(required_items).issubset(started_lease['item_ids']):
                raise RuntimeError('review reservation expired before provider dispatch')
            claim['lease']['expires_at'] = started_lease['expires_at']
            claim['lease']['worker_started'] = True
        session_dir = (workflow.directory / 'secondary-sessions' / digest(dict(
            identity=identity, generation=generation, prompt=request['prompt'], schema=request['response_schema']))
            if generation else None)
        started = time.time()
        monotonic_start = time.monotonic()
        mark_attempt(request, 'calling', started_at=started)
        try:
            legacy_root = getattr(client, 'legacy_session_root', None)
            current_root = getattr(client, 'session_root', None)
            if generation == 0 and legacy_root is not None and current_root is not None:
                schema = (None if request['response_schema'] is None else
                          json.dumps(request['response_schema'], ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')))
                wire = structured_prompt(request['prompt'], schema)
                key = hashlib.sha256((identity + '\0' + wire).encode()).hexdigest()
                old = legacy_root / key
                new = current_root / key
                if old.resolve() != new.resolve() and old.exists() and not new.exists():
                    raise RuntimeError('legacy session exists outside the durable work directory; '
                                       'preserve it and recover explicitly before retrying')
            with attempt_lock:
                attempted_ids.extend(request['claim_ids'])
            raw = request_completion(client, request["prompt"], request["response_schema"], session_dir=session_dir)
            parsed = json.loads(normalize_json_response(raw)) if isinstance(raw, str) else raw
            if not isinstance(parsed, dict):
                raise ValueError("review response must be a JSON object")
        except Exception as exc:
            mark_attempt(request, 'call_failed', finished_at=time.time(),
                         elapsed_seconds=max(0, time.monotonic() - monotonic_start),
                         error_type=type(exc).__name__)
            raise
        mark_attempt(request, 'response_received', finished_at=time.time(),
                     elapsed_seconds=max(0, time.monotonic() - monotonic_start))
        return parsed

    def renew():
        now = time.time()
        due = [owned for owned in leases
               if owned.get('worker_started') and outstanding_requests.get(owned["lease_id"], 0) > 0
               and owned["expires_at"] - now <= lease_seconds / 3]
        if not due:
            return
        # One locked state read/write for all due leases. Fast model replies
        # need no renewal I/O; long-running requests keep their ownership.
        with workflow.queue.transaction() as state:
            for owned in due:
                renewed = renew_items_in_state(state, owned["packet_id"], identity,
                                               owned["lease_id"], lease_seconds)
                if renewed["expires_at"] is not None:
                    owned["expires_at"] = renewed["expires_at"]

    save_progress('running')
    next_progress = time.monotonic() + progress_interval_seconds
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            while True:
                while not quota_failure and len(futures) < max_workers and dispatched < limit:
                    # A small reservation per free slot leaves work available
                    # to the main Agent and refills without waiting for a tail.
                    if dependency_mode:
                        claim = workflow.claim_dependency_reviews(identity,
                            max_items=min(batch_size, limit-dispatched), lease_seconds=min(lease_seconds, startup_lease_seconds),
                            exclude_claim_ids=list(failures))
                        if claim.get("status") != "dependency_reviews_claimed":
                            break
                        role = "global_dependency_completion"
                        requests = [dict(request_id=role + ':' + claim["lease"]["lease_id"], claim_ids=claim["target_claim_ids"],
                                         prompt=claim["items"][role]["prompt"], response_schema=None,
                                         lease_id=claim["lease"]["lease_id"])]
                    else:
                        claim = workflow.claim_reviews(identity, max_items=min(batch_size, limit-dispatched),
                            batch_size=batch_size, lease_seconds=min(lease_seconds, startup_lease_seconds),
                            exclude_claim_ids=list(failures), single_request=True)
                        if claim.get("status") != "reviews_claimed":
                            break
                        requests = claim["requests"]
                    leases.append(claim["lease"])
                    outstanding_requests[claim["lease"]["lease_id"]] = len(requests)
                    for request in requests:
                        mark_attempt(request, 'scheduled', scheduled_at=time.time())
                        futures[pool.submit(answer, claim, request)] = (claim, request)
                        dispatched += len(request["claim_ids"])
                if not futures:
                    break
                completed, _ = wait(futures, timeout=min(30, lease_seconds / 3, progress_interval_seconds), return_when=FIRST_COMPLETED)
                renew()
                if time.monotonic() >= next_progress:
                    save_progress('running')
                    next_progress = time.monotonic() + progress_interval_seconds
                for future in completed:
                    claim, request = futures.pop(future)
                    ids = request["claim_ids"]
                    try:
                        result = future.result()
                        if dependency_mode:
                            outcome = workflow.submit(claim["packet_id"],
                                {"global_dependency_completion": result}, identity, claim["input_hash"])
                            if outcome.get("status") == "blocked:global_contract":
                                raise ValueError(outcome.get("reason"))
                            returned = [row["claim_id"] for row in result.get("dependency_reviews", [])]
                            outcome = {**outcome, "returned_claim_ids": returned,
                                       "omitted_claim_ids": sorted(set(ids) - set(returned))}
                        else:
                            outcome = workflow.submit_review_request(claim["packet_id"], request["request_id"],
                                result, identity, claim["input_hash"])
                        invalid = outcome.get("review_errors", {})
                        for cid in outcome["returned_claim_ids"]:
                            if cid in invalid:
                                failures[cid] = invalid[cid]["reason"]
                            else:
                                accepted.append(cid)
                        for cid in outcome["omitted_claim_ids"]:
                            failures[cid] = "missing batched Claim response; retained for main Agent"
                        mark_attempt(request, 'accepted' if not invalid and not outcome['omitted_claim_ids'] else 'partial',
                                     accepted_count=sum(cid not in invalid for cid in outcome['returned_claim_ids']),
                                     omitted_count=len(outcome['omitted_claim_ids']))
                    except Exception as exc:
                        mark_attempt(request, 'failed', error_type=type(exc).__name__)
                        code = getattr(exc, "code", None)
                        if code == "quota_exhausted":
                            if not quota_failure:
                                with workflow.queue.transaction() as state:
                                    state.setdefault("secondary_availability", {})[identity] = dict(
                                        status="unavailable", reason_code=code, generation=generation,
                                        event_reference=getattr(exc, "event_reference", None))
                                workflow.record_secondary_event(packet, code=code, requested_identity=identity,
                                                                failed_claim_ids=ids)
                            quota_failure = True
                        failures.update({cid: code or f"{type(exc).__name__}: {exc}" for cid in ids})
                    finally:
                        lease_id = claim["lease"]["lease_id"]
                        outstanding_requests[lease_id] -= 1
                        if outstanding_requests[lease_id] == 0:
                            workflow.queue.release_items(claim["packet_id"], identity, lease_id)
    finally:
        for owned in leases:
            lease_id = owned["lease_id"]
            if outstanding_requests[lease_id] > 0:
                workflow.queue.release_items(owned["packet_id"], identity, lease_id)
        save_progress('stopped')
    if not dispatched and not failures:
        outstanding = workflow.next()
        return dict(status="reviews_in_flight" if outstanding.get("status") == "reviews_in_flight"
                    else "secondary_no_work", packet_id=initial_packet_id,
                    input_hash=packet.get("input_hash"), kind=kind,
                    accepted_claim_ids=[], failures={}, scheduled_claim_count=0, dispatched_claim_count=0,
                    provider_invocations_confirmed=None,
                    outstanding=outstanding, warnings=[],
                    reason="No review request was dispatched; inspect outstanding work before waiting.")
    warning = workflow.record_secondary_event(packet,
        code='secondary_failed' if failures else 'secondary_completed', requested_identity=identity,
        accepted_claim_ids=accepted, failed_claim_ids=failures)
    outstanding = workflow.next()
    return dict(status="fallback_to_main" if failures else "secondary_complete",
                packet_id=initial_packet_id, input_hash=packet.get("input_hash"), kind=kind,
                accepted_claim_ids=sorted(set(accepted)), failures=failures,
                scheduled_claim_count=dispatched, dispatched_claim_count=len(attempted_ids),
                provider_invocations_confirmed=None, outstanding=outstanding,
                warnings=([warning] if failures else []) + ([dict(
                    code='dispatch_observability_unavailable',
                    reason='Some local dispatch observations could not be saved.',
                    error_types=sorted(set(telemetry_errors)))] if telemetry_errors else []))
