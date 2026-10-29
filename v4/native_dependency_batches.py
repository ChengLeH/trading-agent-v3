"""Leased, independently executable chunks for global dependency review.

The queue records ownership and raw candidates only. The real V4 runner still
validates every response and does not complete the global gate until every
Claim has a semantic dependency review.
"""
from copy import deepcopy
import time
from uuid import uuid4

from .agent_queue import claim_items_in_state
from .runner import require_model_identity

DEPENDENCY_ROLE = "global_dependency_completion"
DEPENDENCY_PACKET_KIND = "global"


def dependency_completion_contract(runner, target_ids, *, parallel=False):
    """Expose exact pending coverage and a format guide, never a verdict."""
    target_ids = _target_ids(runner, target_ids)
    coverage = runner._global_coverage_status()
    return dict(target_claim_ids=target_ids, parallel=parallel,
        missing_claim_ids=deepcopy(coverage.get('missing_dependency_claim_ids', [])),
        response_format=dict(type='object', required=['findings', 'dependency_reviews'],
            optional=['resolved_external_findings'], row_fields=dict(
                claim_id='one exact target Claim ID',
                required_dependencies='array of exact Claim IDs determined by semantic review',
                unresolved='boolean', reason='brief semantic reason')),
        instruction=('Review every target; findings and resolved_external_findings must be empty arrays. '
                     if parallel else 'Review every target; report only new findings and resolutions. ')
                    + 'Do not treat missing results or an empty dependency list as a default PASS.')


def _target_ids(runner, target_ids):
    if not isinstance(target_ids, (list, tuple)) or not target_ids:
        raise ValueError("target_claim_ids must be a nonempty list or tuple")
    claims = {claim["claim_id"] for claim in runner.run.claims}
    values = list(target_ids)
    if any(not isinstance(cid, str) or not cid.strip() for cid in values):
        raise ValueError("target_claim_ids must contain nonempty Claim IDs")
    if len(set(values)) != len(values) or set(values) - claims:
        raise ValueError("target_claim_ids contain duplicate or unknown Claim IDs")
    return values


def dependency_identity(runner, target_ids):
    """Return stable sweep identity plus the exact scoped Claim IDs.

    Coverage round is deliberately omitted so an accepted sibling chunk does
    not invalidate another packet that is still in flight.
    """
    pending = runner.pending_global
    if (not isinstance(pending, dict) or "review" not in pending
            or not isinstance(pending.get("sweep_response_hash"), str)
            or not pending["sweep_response_hash"]):
        raise ValueError("a primary full-global response is required before dependency chunks")
    return dict(input_hash=pending["input_hash"], report_hash=pending["report_hash"],
                history_hash=pending["history_hash"],
                verification_epoch=runner._verification_epoch,
                sweep_response_hash=pending["sweep_response_hash"],
                target_claim_ids=_target_ids(runner, target_ids))


def _identity_root(identity):
    return {key: identity[key] for key in (
        "input_hash", "report_hash", "history_hash", "verification_epoch",
        "sweep_response_hash")}


def _live_dependency_leases(state, root, now=None):
    now = time.time() if now is None else now
    leased = set()
    for packet in state.get("packets", {}).values():
        context = packet.get("global_context")
        if (packet.get("kind") != DEPENDENCY_PACKET_KIND
                or packet.get("parallel_dependency") is not True
                or not isinstance(context, dict)
                or _identity_root(context) != root
                or packet.get("retired")):
            continue
        role = next(iter(packet.get("items", {})), None)
        if role is None or role in packet.get("received", {}):
            continue
        lease = packet.get("item_leases", {}).get(role)
        if isinstance(lease, dict) and isinstance(lease.get("expires_at"), (int, float)) \
                and lease["expires_at"] > now:
            leased.update(context.get("target_claim_ids", []))
    return leased


def _retire_legacy_active_completion(state):
    packet_id = state.get("active_packet_id")
    packet = state.get("packets", {}).get(packet_id)
    if (not isinstance(packet, dict) or packet.get("kind") != DEPENDENCY_PACKET_KIND
            or packet.get("parallel_dependency") is True or packet.get("retired")):
        return False
    role = next(iter(packet.get("items", {})), None)
    if role != DEPENDENCY_ROLE or role in packet.get("received", {}):
        return False
    lease = packet.get("item_leases", {}).get(role)
    if isinstance(lease, dict) and lease.get("expires_at", 0) > time.time():
        return False
    packet.update(retired=True, retirement_reason="parallel_dependency_batches_enabled")
    state["active_packet_id"] = None
    return True


class NativeDependencyBatchMixin:
    """Build immutable, independently leased global dependency packets."""

    def claim_dependency_reviews(self, executor, max_items=12, lease_seconds=3600,
                                 exclude_claim_ids=()):
        require_model_identity(executor)
        if type(max_items) is not int or not 1 <= max_items <= 64:
            raise ValueError("max_items must be 1..64")
        if not isinstance(exclude_claim_ids, (list, tuple)):
            raise ValueError("exclude_claim_ids must be a list or tuple")
        exclusions = list(exclude_claim_ids)
        if any(not isinstance(cid, str) or not cid.strip() for cid in exclusions):
            raise ValueError("exclude_claim_ids must contain nonempty Claim IDs")
        if len(set(exclusions)) != len(exclusions):
            raise ValueError("exclude_claim_ids must not contain duplicates")
        with self.queue.transaction() as state:
            runner = self._load(state)
            recovered = None
            # Direct claim callers must drain the same durable candidates as
            # next(). A received answer is not unclaimed work after a crash.
            for candidate in state.get("packets", {}).values():
                if (candidate.get("parallel_dependency") and not candidate.get("retired")
                        and candidate.get("input_hash") == state["input_hash"]
                        and candidate.get("global_context", {}).get("verification_epoch") == runner._verification_epoch
                        and DEPENDENCY_ROLE in candidate.get("received", {})
                        and candidate["packet_id"] not in state.get("global_outcomes", {})):
                    recovered = self._ingest_global(state, runner, candidate)
                    if runner.pending_global is None:
                        break
            blocked = state.get("global_blocked")
            if blocked:
                self._save(state, runner)
                return dict(status="blocked:global_contract", blocked=True, reason=deepcopy(blocked),
                            next_action="retry-global with an operator reason before claiming more work")
            if recovered is not None and runner.pending_global is None:
                self._save(state, runner)
                return deepcopy(recovered)
            pending = runner.pending_global
            if not isinstance(pending, dict) or "review" not in pending:
                self._save(state, runner)
                return dict(status="global_dependency_not_ready", missing_dependency_claim_ids=[])
            if pending.get("resolution_completion"):
                self._save(state, runner)
                return dict(status="global_dependency_not_ready",
                            reason="global resolution completion remains owned by the primary reviewer",
                            missing_dependency_claim_ids=[])

            coverage = runner._global_coverage_status()
            missing = coverage["missing_dependency_claim_ids"]
            if not missing or not coverage["needs_retry"]:
                self._save(state, runner)
                return deepcopy(coverage)

            # A live legacy owner/candidate must finish before switching the
            # serial request into parallel chunks of the same missing IDs.
            active = state.get("packets", {}).get(state.get("active_packet_id"))
            if (active and active.get("kind") == "global" and not active.get("parallel_dependency")
                    and not active.get("retired") and DEPENDENCY_ROLE in active.get("items", {})):
                lease = active.get("item_leases", {}).get(DEPENDENCY_ROLE, {})
                if lease.get("expires_at", 0) > time.time() or DEPENDENCY_ROLE in active.get("received", {}):
                    self._save(state, runner)
                    return dict(status="global_dependency_waiting", kind="global",
                        reason="legacy dependency completion is owned or awaiting ingestion; call next to collect it",
                        missing_dependency_claim_ids=deepcopy(missing))
            # Unowned attempts are audit history, not hot outstanding work.
            # Retire their complete prompts so NativeArchive can cold-store them.
            now = time.time()
            for previous in state.get("packets", {}).values():
                if (not previous.get("parallel_dependency") or previous.get("retired")
                        or DEPENDENCY_ROLE in previous.get("received", {})):
                    continue
                lease = previous.get("item_leases", {}).get(DEPENDENCY_ROLE, {})
                if lease.get("expires_at", 0) <= now:
                    previous.update(retired=True, retirement_reason="dependency_lease_expired_or_released")
            _retire_legacy_active_completion(state)
            root = _identity_root(dependency_identity(runner, missing[:1]))
            state["parallel_dependency_mode"] = deepcopy(root)
            leased = _live_dependency_leases(state, root)
            unknown_exclusions = set(exclusions) - {claim["claim_id"] for claim in runner.run.claims}
            if unknown_exclusions:
                raise ValueError(f"exclude_claim_ids contain unknown Claims: {sorted(unknown_exclusions)}")
            excluded = set(exclusions)
            available = [cid for cid in missing if cid not in leased and cid not in excluded]
            if not available:
                self._save(state, runner)
                return dict(status="global_dependency_waiting", kind="global",
                            missing_dependency_claim_ids=deepcopy(missing),
                            leased_claim_ids=[cid for cid in missing if cid in leased],
                            excluded_claim_ids=[cid for cid in missing if cid in excluded])

            chunk_size = min(max_items, runner.global_dependency_batch_size)
            target_ids = available[:chunk_size]
            role, prompt = runner.prepare_phase6_global_sweep(
                dependency_claim_ids=target_ids)
            if role != DEPENDENCY_ROLE or not isinstance(prompt, str) or not prompt:
                raise ValueError("runner did not prepare a scoped dependency completion prompt")
            context = dependency_identity(runner, target_ids)
            packet_id = uuid4().hex
            packet = dict(packet_id=packet_id, kind=DEPENDENCY_PACKET_KIND,
                          input_hash=state["input_hash"], parallel_dependency=True,
                          global_context=context,
                          items={role: dict(prompt=prompt)}, received={})
            state["packets"][packet_id] = packet
            lease = claim_items_in_state(state, packet_id, executor, [role], lease_seconds)
            self._save(state, runner)
            return dict(status="dependency_reviews_claimed", kind=DEPENDENCY_PACKET_KIND,
                        packet_id=packet_id, input_hash=state["input_hash"],
                        target_claim_ids=deepcopy(target_ids), global_context=deepcopy(context),
                        completion_contract=dependency_completion_contract(runner, target_ids, parallel=True),
                        lease=lease, items=deepcopy(packet["items"]))


def collect_dependency_packet(runner, packet, row):
    """Pass one archived parallel candidate through the canonical P6 collector."""
    if (not isinstance(packet, dict) or packet.get("kind") != DEPENDENCY_PACKET_KIND
            or packet.get("parallel_dependency") is not True):
        raise ValueError("not a parallel dependency packet")
    context = packet.get("global_context")
    if not isinstance(context, dict):
        raise ValueError("parallel dependency packet has no global context")
    target_ids = _target_ids(runner, context.get("target_claim_ids"))
    role = next(iter(packet.get("items", {})), None)
    if role != DEPENDENCY_ROLE or not isinstance(row, dict):
        raise ValueError("parallel dependency candidate is malformed")
    require_model_identity(row.get("executor"))
    if not isinstance(row.get("response"), dict):
        raise ValueError("global dependency response must be a JSON object")
    if packet.get("input_hash") != runner._hash():
        raise ValueError("stale parallel dependency packet input")
    runner.prepare_phase6_global_sweep(dependency_claim_ids=target_ids)
    if packet.get("global_context") != dependency_identity(runner, target_ids):
        raise ValueError("parallel dependency packet no longer matches its scoped request")
    primary = runner.pending_global.get("review", {}).get("model")
    require_model_identity(primary)
    return runner.collect_phase6_global_sweep(
        deepcopy(row["response"]), model_identity=primary,
        dependency_reviewer=row["executor"], dependency_claim_ids=target_ids)
