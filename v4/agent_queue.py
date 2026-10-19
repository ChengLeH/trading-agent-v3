"""Durable candidate work queue for native host-agent exchanges.

Stores coarse work packets and the exact structured responses received from
any current host runtime. Candidates are never semantic verdicts: this module
imports no runner, model, or network code, and never declares PASS.
"""
import fcntl
import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .storage import write_json

STATE_FORMAT = "v4-native-work/1"


def _require_identity(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


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
            if not isinstance(state, dict) or state.get("format") != STATE_FORMAT:
                raise ValueError("unsupported native work queue state")
            yield state
            write_json(self.state_path, state)
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
        _require_identity(packet_id, "packet_id")
        _require_identity(executor, "executor")
        _require_identity(input_hash, "input_hash")
        if not isinstance(responses, dict) or not responses:
            raise ValueError("responses must be a nonempty object keyed by item IDs")
        for item_id, response in responses.items():
            _require_identity(item_id, "response item ID")
            if not isinstance(response, (dict, list)):
                raise ValueError(f"response for {item_id} must be a JSON object or array")
        with self.transaction() as state:
            packet = state["packets"].get(packet_id)
            if not isinstance(packet, dict):
                raise ValueError("unknown packet")
            if packet.get("retired"):
                raise ValueError("retired packet cannot receive further candidates")
            if state["input_hash"] != input_hash or packet["input_hash"] != input_hash:
                raise ValueError("stale packet input version; retire the packet before rescheduling")
            for item_id in responses:
                if item_id not in packet["items"]:
                    raise ValueError(f"unknown item ID {item_id} for packet {packet_id}")
            for item_id, response in responses.items():
                existing = packet["received"].get(item_id)
                if existing is None:
                    continue
                if existing["response"] == response and existing["executor"] == executor:
                    continue  # idempotent redelivery keeps the original candidate row
                raise ValueError(f"conflicting duplicate response for item {item_id}")
            for item_id, response in responses.items():
                if item_id not in packet["received"]:
                    packet["received"][item_id] = dict(attempt_id=uuid4().hex, executor=executor,
                                                       response=deepcopy(response))
            received = sorted(packet["received"])
            missing = [item_id for item_id in packet["items"] if item_id not in packet["received"]]
            return dict(packet_id=packet_id, received_item_ids=received, missing_item_ids=missing)
