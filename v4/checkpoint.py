"""Atomic canonical-state persistence; reload revalidates durable responses instead of trusting receipt snapshots."""
from dataclasses import asdict
from pathlib import Path
import json
from v3.pipeline import RunState
from v3.facts import Fact
from v3.evidence import EvidencePacket
from v3.sources import SourceRecord, SourceError
from .runner import PipelineRunnerV4


from .storage import write_json


def save_checkpoint(runner, path):
    write_json(path, dict(format="v4-checkpoint/1", state=runner.orch.snapshot(runner.run_id),
                          status=runner.status, verification_epoch=runner._verification_epoch,
                          contract_attempts=runner._contract_attempts,
                          records=[dict(kind=type(r).__name__, value=asdict(r)) for r in runner.run.records],
                          revision_round=runner.revision_round, history=runner.history,
                          deferred_global_findings=runner.deferred_global_findings,
                          max_revisions=runner.max_revisions, max_workers=runner.max_workers,
                          review_batch_size=runner.review_batch_size, review_batch_max_bytes=runner.review_batch_max_bytes,
                          audit=dict(atomic=runner.receipts, deep=runner.deep_receipts, global_review=runner.global_receipt)))


def load_checkpoint(path, *, continue_verification=True):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "v4-checkpoint/1":
        raise ValueError("unsupported V4 checkpoint")
    state = data["state"]
    r = PipelineRunnerV4(max_revisions=data["max_revisions"], max_workers=data["max_workers"],
                         review_batch_size=data.get("review_batch_size", 1),
                         review_batch_max_bytes=data.get("review_batch_max_bytes", 65536))
    if continue_verification:
        from uuid import UUID
        epoch = data.get("verification_epoch")
        if not isinstance(epoch, str) or UUID(epoch).hex != epoch:
            raise ValueError("checkpoint lacks a durable verification epoch; explicit fresh verification required")
        attempts = data.get("contract_attempts", {})
        if not isinstance(attempts, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in attempts.items()):
            raise ValueError("invalid persisted contract attempts")
        r._verification_epoch = epoch
        r._contract_attempts = attempts
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
    # Never restore serialized receipts. Same-execution continuation replays
    # exact request archives through all semantic-contract validators. A changed
    # canonical ancestor, source, schema, model or policy gets a different key.
    r.status = run.stage = "resumed:requires_verification"
    return r
