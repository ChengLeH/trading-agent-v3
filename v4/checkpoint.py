"""Atomic canonical-state persistence; reload always requires fresh reviews."""
from dataclasses import asdict
from pathlib import Path
import json
import os
import tempfile
from v3.pipeline import RunState
from v3.facts import Fact
from v3.evidence import EvidencePacket
from v3.sources import SourceRecord, SourceError
from .runner import PipelineRunnerV4


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


def save_checkpoint(runner, path):
    write_json(path, dict(format="v4-checkpoint/1", state=runner.orch.snapshot(runner.run_id),
                          status=runner.status,
                          records=[dict(kind=type(r).__name__, value=asdict(r)) for r in runner.run.records],
                          revision_round=runner.revision_round, history=runner.history,
                          deferred_global_findings=runner.deferred_global_findings,
                          max_revisions=runner.max_revisions, max_workers=runner.max_workers,
                          review_batch_size=runner.review_batch_size, review_batch_max_bytes=runner.review_batch_max_bytes,
                          audit=dict(atomic=runner.receipts, deep=runner.deep_receipts, global_review=runner.global_receipt)))


def load_checkpoint(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "v4-checkpoint/1":
        raise ValueError("unsupported V4 checkpoint")
    state = data["state"]
    r = PipelineRunnerV4(max_revisions=data["max_revisions"], max_workers=data["max_workers"],
                         review_batch_size=data.get("review_batch_size", 1),
                         review_batch_max_bytes=data.get("review_batch_max_bytes", 65536))
    r.run_id = state["run_id"]
    run = RunState(run_id=r.run_id, ticker=state["ticker"], manifest=state["manifest"])
    run.events = {}
    r.orch.runs[r.run_id] = run
    for value in state["facts"].values():
        run.fact_store.add(Fact(**value))
    for cid, c in state["calcs"].items():
        run.calc_store.add(cid, c["value"], c["calc_type"], c["input_refs"], c["unit"], c["meta"])
    for e in state["evidence"].values():
        run.evidence_store.add(EvidencePacket(**e))
    r.orch.register_events(r.run_id, list(state.get("events", {}).values()))
    for rec in data["records"]:
        types = {"SourceRecord": SourceRecord, "SourceError": SourceError}
        run.records.append(types[rec["kind"]](**rec["value"]))
    run.trade = state.get("trade", {})
    run.trade_generated = state.get("trade_generated", ([], []))
    grouped = {}
    for c in state["claims"]:
        grouped.setdefault(c["author_role"], []).append(c)
    r.orch.submit_batch(r.run_id, grouped)
    r.revision_round = data["revision_round"]
    r.history = data["history"]
    r.deferred_global_findings = data.get("deferred_global_findings", [])
    if (not isinstance(r.deferred_global_findings, list)
            or any(not isinstance(f, dict) or not isinstance(f.get("issue"), str)
                   or not f["issue"].strip() or not isinstance(f.get("finding_id"), str)
                   or not f["finding_id"].strip() for f in r.deferred_global_findings)):
        raise ValueError("invalid deferred global findings")
    if len({f["finding_id"] for f in r.deferred_global_findings}) != len(r.deferred_global_findings):
        raise ValueError("duplicate deferred finding_id")
    from .semantic_contracts import validate_revision_history
    validate_revision_history(run.claims, r.history)
    # Serialized receipts are audit records, not proof of a new execution.
    # Never trust a stale/mutated checkpoint to bypass atomic/global review.
    r.status = run.stage = "resumed:requires_verification"
    return r
