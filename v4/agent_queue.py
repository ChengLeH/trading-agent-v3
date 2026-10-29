"""Durable candidate work queue for native host-agent exchanges.

Stores coarse work packets and the exact structured responses received from
any current host runtime. Candidates are never semantic verdicts: this module
imports no runner, model, or network code, and never declares PASS.
"""
import fcntl
import json
import math
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .storage import write_json
from .native_archive import NativeArchive

STATE_FORMAT = "v4-native-work/2"
LEGACY_STATE_FORMAT = "v4-native-work/1"


def _require_identity(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _require_lease_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("lease_seconds must be finite and positive")
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        raise ValueError("lease_seconds must be finite and positive") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("lease_seconds must be finite and positive")
    return seconds


def _active_packet(state, packet_id):
    packets = state.get("packets")
    packet = packets.get(packet_id) if isinstance(packets, dict) else None
    if not isinstance(packet, dict):
        raise ValueError("unknown packet")
    if packet.get("retired"):
        if packet.get("parallel_dependency"):
            raise ValueError("retired dependency packet has no active lease; cannot receive further candidates")
        raise ValueError("retired packet cannot receive further candidates")
    if state.get("input_hash") != packet.get("input_hash"):
        raise ValueError("stale packet input version; retire the packet before rescheduling")
    return packet


def _item_leases(packet, create=False):
    if "item_leases" not in packet:
        if not create:
            return {}
        packet["item_leases"] = {}
    rows = packet["item_leases"]
    if not isinstance(rows, dict):
        raise ValueError("packet item lease state is malformed")
    return rows


def _lease_row(row, item_id):
    if not isinstance(row, dict):
        raise ValueError(f"item lease for {item_id} is malformed")
    lease_id = _require_identity(row.get("lease_id"), "lease_id")
    owner = _require_identity(row.get("owner"), "lease owner")
    expires_at = row.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError(f"item lease for {item_id} has an invalid expiry")
    try:
        expires_at = float(expires_at)
    except (OverflowError, ValueError):
        raise ValueError(f"item lease for {item_id} has an invalid expiry") from None
    if not math.isfinite(expires_at):
        raise ValueError(f"item lease for {item_id} has an invalid expiry")
    claimed_at = row.get('claimed_at')
    if claimed_at is not None and (isinstance(claimed_at, bool) or
            not isinstance(claimed_at, (int, float)) or not math.isfinite(claimed_at)):
        raise ValueError(f"item lease for {item_id} has an invalid claim time")
    return lease_id, owner, expires_at


def _lease_result(packet_id, lease_id, owner, item_ids, expires_at):
    return dict(packet_id=packet_id, lease_id=lease_id, owner=owner,
                item_ids=list(item_ids), expires_at=expires_at)


def _remember_released_owner(packet, item_id, owner):
    owners = packet.setdefault("released_item_owners", {}).setdefault(item_id, [])
    if owner not in owners:
        owners.append(owner)


def claim_items_in_state(state, packet_id, executor, item_ids=None, lease_seconds=3600):
    """Claim exact unreceived packet items while the caller holds queue.transaction()."""
    _require_identity(packet_id, "packet_id")
    _require_identity(executor, "executor")
    seconds = _require_lease_seconds(lease_seconds)
    packet = _active_packet(state, packet_id)
    items = packet.get("items")
    received = packet.get("received")
    if not isinstance(items, dict) or not isinstance(received, dict):
        raise ValueError("packet item state is malformed")

    requested = None
    if item_ids is not None:
        if not isinstance(item_ids, (list, tuple)) or not item_ids:
            raise ValueError("item_ids must be a nonempty list or tuple of exact item IDs")
        requested = []
        seen = set()
        for item_id in item_ids:
            _require_identity(item_id, "item ID")
            if item_id in seen:
                raise ValueError(f"duplicate item ID {item_id}")
            seen.add(item_id)
            requested.append(item_id)

    now = time.time()
    expires_at = now + seconds
    if not math.isfinite(now) or not math.isfinite(expires_at):
        raise ValueError("lease expiry must be finite")
    leases = _item_leases(packet)
    candidates = list(items) if requested is None else requested
    selected = []
    for item_id in candidates:
        if item_id not in items:
            raise ValueError(f"unknown item ID {item_id} for packet {packet_id}")
        if item_id in received:
            if requested is not None:
                raise ValueError(f"item {item_id} has already been received")
            continue
        row = leases.get(item_id)
        if row is not None:
            _, _, existing_expiry = _lease_row(row, item_id)
            if existing_expiry > now:
                if requested is not None:
                    raise ValueError(f"item {item_id} is already leased")
                continue
        selected.append(item_id)

    if not selected:
        raise ValueError("no unreceived items are available to claim")

    lease_id = uuid4().hex
    leases = _item_leases(packet, create=True)
    for item_id in selected:
        if item_id in leases:
            _, previous_owner, _ = _lease_row(leases[item_id], item_id)
            _remember_released_owner(packet, item_id, previous_owner)
        leases[item_id] = dict(lease_id=lease_id, owner=executor, claimed_at=now, expires_at=expires_at)
    return _lease_result(packet_id, lease_id, executor, selected, expires_at)


def renew_items_in_state(state, packet_id, executor, lease_id, lease_seconds=3600):
    _require_identity(packet_id, "packet_id")
    _require_identity(executor, "executor")
    _require_identity(lease_id, "lease_id")
    seconds = _require_lease_seconds(lease_seconds)
    packets = state.get("packets")
    packet = packets.get(packet_id) if isinstance(packets, dict) else None
    if not isinstance(packet, dict):
        raise ValueError("unknown packet")
    leases = _item_leases(packet)
    now = time.time()
    if not math.isfinite(now):
        raise ValueError("lease expiry must be finite")
    matching_rows = []
    for item_id, row in leases.items():
        row_lease_id, owner, expiry = _lease_row(row, item_id)
        if row_lease_id == lease_id:
            if owner != executor:
                raise ValueError("lease owner does not match executor")
            matching_rows.append((item_id, expiry))
    if not matching_rows:
        return _lease_result(packet_id, lease_id, executor, [], None)
    if packet.get("retired"):
        raise ValueError("retired packet cannot renew item leases")
    if state.get("input_hash") != packet.get("input_hash"):
        raise ValueError("stale packet input version; retire the packet before rescheduling")
    matches = [item_id for item_id, expiry in matching_rows if expiry > now]
    if not matches:
        return _lease_result(packet_id, lease_id, executor, [], None)
    expires_at = now + seconds
    if not math.isfinite(expires_at):
        raise ValueError("lease expiry must be finite")
    for item_id in matches:
        leases[item_id]["expires_at"] = expires_at
    return _lease_result(packet_id, lease_id, executor, matches, expires_at)


def release_items_in_state(state, packet_id, executor, lease_id):
    _require_identity(packet_id, "packet_id")
    _require_identity(executor, "executor")
    _require_identity(lease_id, "lease_id")
    packets = state.get("packets")
    packet = packets.get(packet_id) if isinstance(packets, dict) else None
    if not isinstance(packet, dict):
        raise ValueError("unknown packet")
    leases = _item_leases(packet)
    matches = []
    for item_id, row in leases.items():
        row_lease_id, owner, _ = _lease_row(row, item_id)
        if row_lease_id == lease_id:
            if owner != executor:
                raise ValueError("lease owner does not match executor")
            matches.append(item_id)
    for item_id in matches:
        _remember_released_owner(packet, item_id, executor)
        del leases[item_id]
    if not leases and "item_leases" in packet:
        del packet["item_leases"]
    return _lease_result(packet_id, lease_id, executor, matches, None)


def submit_items_in_state(state, packet_id, responses, executor, input_hash):
    """Validate and store candidates inside an already-open queue transaction."""
    _require_identity(packet_id, "packet_id")
    _require_identity(executor, "executor")
    _require_identity(input_hash, "input_hash")
    if not isinstance(responses, dict) or not responses:
        raise ValueError("responses must be a nonempty object keyed by item IDs")
    for item_id, response in responses.items():
        _require_identity(item_id, "response item ID")
        if not isinstance(response, (dict, list)):
            raise ValueError(f"response for {item_id} must be a JSON object or array")

    packet = _active_packet(state, packet_id)
    if packet.get("input_hash") != input_hash:
        raise ValueError("stale packet input version; retire the packet before rescheduling")
    items = packet.get("items")
    received = packet.get("received")
    if not isinstance(items, dict) or not isinstance(received, dict):
        raise ValueError("packet item state is malformed")
    for item_id in responses:
        if item_id not in items:
            raise ValueError(f"unknown item ID {item_id} for packet {packet_id}")

    leases = _item_leases(packet)
    now = time.time()
    for item_id in responses:
        row = leases.get(item_id)
        if row is None:
            if packet.get("parallel_dependency") and item_id not in received:
                raise ValueError(f"parallel dependency item {item_id} requires an active lease")
            if item_id not in received and executor in packet.get("released_item_owners", {}).get(item_id, []):
                raise ValueError(f"released review item {item_id} requires a fresh lease before submitting")
            continue
        _, owner, expires_at = _lease_row(row, item_id)
        if expires_at <= now:
            raise ValueError(f"item lease for {item_id} has expired")
        if owner != executor:
            raise ValueError(f"item {item_id} is leased to another executor")

    for item_id, response in responses.items():
        existing = received.get(item_id)
        if existing is None:
            continue
        if existing["response"] == response and existing["executor"] == executor:
            continue  # idempotent redelivery keeps the original candidate row
        raise ValueError(f"conflicting duplicate response for item {item_id}")
    for item_id, response in responses.items():
        if item_id not in received:
            received[item_id] = dict(attempt_id=uuid4().hex, executor=executor,
                                     response=deepcopy(response))
        leases.pop(item_id, None)
    if not leases and "item_leases" in packet:
        del packet["item_leases"]
    received_item_ids = sorted(received)
    missing_item_ids = [item_id for item_id in items if item_id not in received]
    return dict(packet_id=packet_id, received_item_ids=received_item_ids,
                missing_item_ids=missing_item_ids)


class NativeWorkQueue:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.state_path = self.directory / "agent-state.json"
        self.lock_path = self.directory / "agent-state.lock"

    @contextmanager
    def transaction(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("format") not in (STATE_FORMAT, LEGACY_STATE_FORMAT):
                raise ValueError("unsupported native work queue state")
            archive = NativeArchive(self.directory)
            state = archive.hydrate(state)
            # Upgrade inline /1 states during the successful transaction only.
            # Old binaries reject /2 before interpreting any cold payload marker.
            state["format"] = STATE_FORMAT
            yield state
            write_json(self.state_path, archive.compact(state))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def create(self, checkpoint, input_hash):
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be a JSON object")
        _require_identity(input_hash, "input_hash")
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if self.state_path.exists():
                raise ValueError("native work queue is already initialised")
            write_json(self.state_path, dict(format=STATE_FORMAT, checkpoint=deepcopy(checkpoint),
                                             input_hash=input_hash, packets={}))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def issue(self, kind, items, input_hash):
        _require_identity(kind, "kind")
        _require_identity(input_hash, "input_hash")
        if not isinstance(items, dict) or not items:
            raise ValueError("items must be a nonempty object keyed by exact item IDs")
        for item_id, item in items.items():
            _require_identity(item_id, "item ID")
            if not isinstance(item, dict) or not isinstance(item.get("prompt"), str) or not item["prompt"]:
                raise ValueError(f"item {item_id} requires a nonempty prompt string")
            if "schema" in item and not isinstance(item["schema"], (dict, list, str)):
                raise ValueError(f"item {item_id} schema must be a JSON object, array, or string")
        with self.transaction() as state:
            if state["input_hash"] != input_hash:
                raise ValueError("input_hash differs from the canonical state input")
            packet_id = uuid4().hex
            state["packets"][packet_id] = dict(packet_id=packet_id, kind=kind, input_hash=input_hash,
                                               items=deepcopy(items), received={})
            return deepcopy(state["packets"][packet_id])

    def submit(self, packet_id, responses, executor, input_hash):
        with self.transaction() as state:
            return submit_items_in_state(state, packet_id, responses, executor, input_hash)

    def claim_items(self, packet_id, executor, item_ids=None, lease_seconds=3600):
        with self.transaction() as state:
            return claim_items_in_state(state, packet_id, executor, item_ids, lease_seconds)

    def renew_items(self, packet_id, executor, lease_id, lease_seconds=3600):
        with self.transaction() as state:
            return renew_items_in_state(state, packet_id, executor, lease_id, lease_seconds)

    def release_items(self, packet_id, executor, lease_id):
        with self.transaction() as state:
            return release_items_in_state(state, packet_id, executor, lease_id)
