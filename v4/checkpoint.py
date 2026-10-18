"""Atomic canonical-state persistence; reload revalidates durable responses instead of trusting receipt snapshots."""
from dataclasses import asdict
from copy import deepcopy
from pathlib import Path
import json
from uuid import UUID
from v3.pipeline import RunState
from v3.facts import Fact
from v3.evidence import EvidencePacket
from v3.sources import SourceRecord, SourceError
from .runner import PipelineRunnerV4
from .schema import digest


from .storage import write_json


def _load_review_row(run_dir, source, *, run_id, model, layer, claim_id, task_hash):
    if (not isinstance(source, dict)
            or set(source) != {"attempt_id", "metadata_hash", "response_hash", "claim_id", "layer"}
            or source.get("claim_id") != claim_id or source.get("layer") != layer):
        return None
    attempt_id = source.get("attempt_id")
    try:
        if not isinstance(attempt_id, str) or UUID(attempt_id).hex != attempt_id:
            return None
        record = json.loads((run_dir / "responses" / "archive" / (attempt_id + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    metadata_keys = ("format", "run_id", "model", "task_id", "prompt_hash", "review",
                     "agent_policy_sha256", "response_schema_hash")
    metadata = {key: record[key] for key in metadata_keys if key in record}
    review = record.get("review")
    task_hashes = review.get("task_hashes") if isinstance(review, dict) else None
    try:
        metadata_hash = digest(metadata)
        response_hash = digest(record.get("response"))
    except (TypeError, ValueError):
        return None
    if (record.get("status") != "received" or record.get("attempt_id") != attempt_id
            or record.get("run_id") != run_id or record.get("model") != model
            or not isinstance(task_hashes, dict)
            or metadata_hash != source.get("metadata_hash")
            or response_hash != source.get("response_hash")
            or not isinstance(review, dict) or review.get("layer") != layer
            or review.get("contract") != "v4-semantic-review/2"
            or task_hashes.get(claim_id) != task_hash):
        return None
    response = record.get("response")
    try:
        if isinstance(response, str):
            response = json.loads(response)
        if not isinstance(response, dict):
            return None
        if isinstance(response.get("results"), list):
            rows = [row for row in response["results"]
                    if isinstance(row, dict) and row.get("claim_id") == claim_id]
            return rows[0] if len(rows) == 1 else None
        return response if response.get("claim_id") == claim_id else None
    except (ValueError, TypeError):
        return None


def _verified_reference_supplement(run_dir, run_id, cid, task, supplement, layer):
    from .atomic_phase6 import normalize_atomic_result
    from .semantic_contracts import normalize_reference_checks, review_ref_ids
    expected = review_ref_ids(task.claim)
    sources = supplement.get("reference_sources")
    base_source = supplement.get("base_result_source")
    if not isinstance(sources, list) or not sources or not isinstance(base_source, dict):
        return False
    checks_by_ref, base_checks_by_ref, rows_by_attempt = {}, None, {}
    for source in sources:
        row = _load_review_row(run_dir, source, run_id=run_id, model=supplement["model"],
                               layer=layer, claim_id=cid, task_hash=digest(task.to_dict()))
        if row is None or source["attempt_id"] in rows_by_attempt:
            return False
        rows_by_attempt[source["attempt_id"]] = row
        raw_checks = row.get("reference_checks", [])
        try:
            checks = normalize_reference_checks(expected, {"reference_checks": raw_checks, "verdict": "FAIL"},
                                               allow_missing=True)
        except (ValueError, TypeError, KeyError):
            return False
        for check in checks:
            if check["ref_id"] in checks_by_ref:
                return False
            checks_by_ref[check["ref_id"]] = check
        if source["attempt_id"] == base_source.get("attempt_id"):
            base_checks_by_ref = dict(checks_by_ref)
    if base_source.get("attempt_id") not in rows_by_attempt:
        return False
    base = supplement.get("base_result")
    if not isinstance(base, dict):
        return False
    try:
        saved_checks = normalize_reference_checks(expected, {**base, "verdict": "FAIL"}, allow_missing=True)
        if ({row["ref_id"]: row for row in saved_checks} != base_checks_by_ref
                or (not supplement.get("contract_repair")
                    and {row["ref_id"]: row for row in saved_checks} != checks_by_ref)):
            return False
        base_row = deepcopy(rows_by_attempt[base_source["attempt_id"]])
        base_row["reference_checks"] = saved_checks
        normalized_base = normalize_atomic_result(cid, base_row, expected_ref_ids=expected,
                                                   allow_missing_references=True)
        if normalized_base != base:
            return False
        if supplement.get("contract_repair"):
            accepted = normalize_reference_checks(expected, {
                "reference_checks": supplement.get("accepted_reference_checks"), "verdict": "FAIL"})
            return {row["ref_id"]: row for row in accepted} == checks_by_ref
    except (ValueError, TypeError, KeyError):
        return False
    return not supplement.get("accepted_reference_checks")


def save_checkpoint(runner, path):
    write_json(path, dict(format="v4-checkpoint/1", state=runner.orch.snapshot(runner.run_id),
                          status=runner.status, verification_epoch=runner._verification_epoch,
                          contract_attempts=runner._contract_attempts,
                          records=[dict(kind=type(r).__name__, value=asdict(r)) for r in runner.run.records],
                          revision_round=runner.revision_round, history=runner.history,
                          pending_patch=runner.pending_patch,
                          reference_supplements=runner.reference_supplements,
                          global_review_progress=runner.global_review_progress,
                          deferred_global_findings=runner.deferred_global_findings,
                          max_revisions=runner.max_revisions, max_workers=runner.max_workers,
                          max_contract_attempts=runner.max_contract_attempts,
                          review_batch_size=runner.review_batch_size, review_batch_max_bytes=runner.review_batch_max_bytes,
                          audit=dict(atomic=runner.receipts, deep=runner.deep_receipts, global_review=runner.global_receipt)))


def load_checkpoint(path, *, continue_verification=True):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "v4-checkpoint/1":
        raise ValueError("unsupported V4 checkpoint")
    state = data["state"]
    r = PipelineRunnerV4(max_revisions=data["max_revisions"], max_workers=data["max_workers"],
                         review_batch_size=data.get("review_batch_size", 1),
                         max_contract_attempts=data.get("max_contract_attempts", 3),
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
    r.reference_supplements = {"atomic": {}, "deep": {}}
    supplements = data.get("reference_supplements", {"atomic": {}, "deep": {}})
    if not isinstance(supplements, dict) or set(supplements) != {"atomic", "deep"}:
        raise ValueError("invalid reference supplement state")
    if continue_verification:
        from .atomic_phase6 import build_atomic_tasks, normalize_atomic_result
        from .semantic_contracts import review_ref_ids, normalize_reference_checks
        tasks = {task.claim_id: task for task in build_atomic_tasks(run)}
        for layer in ("atomic", "deep"):
            rows = supplements[layer]
            if not isinstance(rows, dict):
                raise ValueError("invalid reference supplement layer")
            for cid, supplement in rows.items():
                try:
                    if cid not in tasks or not isinstance(supplement, dict):
                        raise ValueError("unknown Claim or malformed state")
                    if set(supplement) - {"input_hash", "model", "base_result", "missing_ref_ids", "last_error",
                                          "contract_repair", "accepted_reference_checks", "reference_sources",
                                          "base_result_source"}:
                        raise ValueError("unexpected state fields")
                    contract_repair = supplement.get("contract_repair", False)
                    if type(contract_repair) is not bool:
                        raise ValueError("invalid contract repair state")
                    expected_hash = digest(tasks[cid].to_dict())
                    if supplement.get("input_hash") != expected_hash:
                        raise ValueError("stale Claim input")
                    if not isinstance(supplement.get("model"), str) or not supplement["model"].strip():
                        raise ValueError("missing model identity")
                    # A serialized partial receipt is reusable only when its
                    # accepted rows can be reconstructed from the exact
                    # durable response archive. Otherwise re-review this
                    # Claim from the original prompt instead of trusting the
                    # checkpoint's semantic prose.
                    if not _verified_reference_supplement(
                            Path(path).parent, r.run_id, cid, tasks[cid], supplement, layer):
                        continue
                    base = normalize_atomic_result(
                        cid, supplement.get("base_result"), expected_ref_ids=review_ref_ids(tasks[cid].claim),
                        allow_missing_references=True)
                    missing = supplement.get("missing_ref_ids")
                    expected = review_ref_ids(tasks[cid].claim)
                    if not isinstance(missing, list) or len(set(missing)) != len(missing):
                        raise ValueError("missing reference IDs must be a unique list")
                    if contract_repair:
                        if missing or not isinstance(supplement.get("accepted_reference_checks"), list):
                            raise ValueError("verdict repair requires complete accepted reference checks")
                        accepted = normalize_reference_checks(
                            expected,
                            {"reference_checks": supplement["accepted_reference_checks"], "verdict": "FAIL"})
                    else:
                        if not missing or "accepted_reference_checks" in supplement:
                            raise ValueError("missing reference IDs must be nonempty outside verdict repair")
                        checked = {row["ref_id"] for row in base["reference_checks"]}
                        actual_missing = [ref for ref in expected if ref not in checked]
                        if missing != actual_missing or not actual_missing:
                            raise ValueError("missing reference IDs differ from saved checks")
                        accepted = None
                    if "last_error" in supplement and (not isinstance(supplement["last_error"], str)
                                                        or not supplement["last_error"].strip()):
                        raise ValueError("invalid last supplement error")
                    normalized = {**supplement, "base_result": base}
                    if contract_repair:
                        normalized["accepted_reference_checks"] = accepted
                    r.reference_supplements[layer][cid] = normalized
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError(f"invalid {layer} reference supplement for {cid}: {exc}") from exc
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
    from .schema import claim_index
    known_claims = claim_index(run.claims)
    for finding in r.deferred_global_findings:
        finding_ids = r._finding_claim_ids(finding)
        if set(finding_ids) - known_claims.keys() or (not finding_ids and finding.get("unresolved") is not True):
            raise ValueError("invalid deferred global finding targets")
        if finding.get("resolution_route", "claim_patch") == "claim_patch":
            continue
        state_hash = finding.get("resolution_state_hash")
        if state_hash is None:
            # Older checkpoints did not persist the affected Claim/evidence
            # state behind a non-Claim route. Use the loaded objects as baseline.
            finding["resolution_state_hash"] = r._resolution_state_hash(finding)
        elif (not isinstance(state_hash, str) or len(state_hash) != 64
              or any(ch not in "0123456789abcdef" for ch in state_hash)):
            raise ValueError("invalid deferred resolution state hash")
        baseline = finding.get("resolution_baseline")
        if baseline is None:
            finding["resolution_baseline"] = dict(trusted=False, source_ids=[], claim_hashes={},
                                                   transport_attempt_ids=[])
        elif (not isinstance(baseline, dict) or set(baseline) != {
                "trusted", "source_ids", "claim_hashes", "transport_attempt_ids"}
                or type(baseline.get("trusted")) is not bool
                or not isinstance(baseline.get("source_ids"), list)
                or any(not isinstance(value, str) or not value for value in baseline["source_ids"])
                or len(set(baseline["source_ids"])) != len(baseline["source_ids"])
                or not isinstance(baseline.get("claim_hashes"), dict)
                or set(baseline["claim_hashes"]) - known_claims.keys()
                or any(not isinstance(value, str) or len(value) != 64
                       or any(ch not in "0123456789abcdef" for ch in value)
                       for value in baseline["claim_hashes"].values())
                or not isinstance(baseline.get("transport_attempt_ids"), list)):
            raise ValueError("invalid deferred resolution baseline")
        else:
            try:
                if (len(set(baseline["transport_attempt_ids"])) != len(baseline["transport_attempt_ids"])
                        or any(not isinstance(attempt, str) or UUID(attempt).hex != attempt
                               for attempt in baseline["transport_attempt_ids"])):
                    raise ValueError("invalid transport attempts")
            except (TypeError, ValueError, AttributeError):
                raise ValueError("invalid deferred resolution baseline") from None
    from .semantic_contracts import validate_revision_history
    validate_revision_history(run.claims, r.history)
    progress = data.get("global_review_progress")
    if progress is not None and continue_verification:
        from .semantic_contracts import normalize_dependency_review
        if (not isinstance(progress, dict) or progress.get("format") != "v4-global-progress/1"
                or progress.get("verification_epoch") != r._verification_epoch
                or progress.get("input_hash") != r._hash() or progress.get("history_hash") != digest(r.history)
                or type(progress.get("coverage_round")) is not int or progress["coverage_round"] < 1):
            raise ValueError("invalid global review progress")
        review = progress.get("review")
        if not isinstance(review, dict) or not isinstance(review.get("model"), str) or not review["model"].strip():
            raise ValueError("invalid global progress model")
        audit = normalize_dependency_review(run.claims, review.get("response"), allow_partial=True)
        r._validate_global_findings(review["response"]["findings"])
        if (progress.get("missing_dependency_claim_ids") != audit["missing_dependency_claim_ids"]
                or progress.get("reviewed_dependency_claim_ids") != audit["reviewed_dependency_claim_ids"]
                or not audit["missing_dependency_claim_ids"]):
            raise ValueError("invalid global progress coverage")
        # Diagnostic only. Reconstruct accepted rows by replaying exact request
        # archives through collectors, never trust serialized review receipts.
        r.global_review_progress = progress
    pending = data.get("pending_patch")
    if pending is None and data.get("status") == "blocked:revision_limit":
        # Older runners stopped before saving a repair request. A matching
        # global snapshot can restore diagnostic targets, never review receipts.
        audit = data.get("audit")
        review = audit.get("global_review") if isinstance(audit, dict) else None
        if (isinstance(review, dict) and review.get("input_hash") == r._hash()
                and review.get("history_hash") == digest(r.history)
                and isinstance(review.get("findings"), list)):
            findings = [deepcopy(f) for f in review["findings"] if isinstance(f, dict)
                        and f.get("resolution_route", "claim_patch") == "claim_patch"
                        and f.get("materiality", "material") != "clarity_only"]
            if findings and all(r._finding_claim_ids(f) for f in findings):
                pending = dict(input_hash=r._hash(), ids=list(dict.fromkeys(
                    cid for f in findings for cid in r._finding_claim_ids(f))), findings=findings)
    if pending is not None:
        from .schema import id_list
        try:
            if not isinstance(pending, dict) or set(pending) != {"input_hash", "ids", "findings"}:
                raise ValueError("invalid fields")
            ids = id_list(pending["ids"], "pending repair IDs")
            findings = pending["findings"]
            if (not ids or set(ids) - known_claims.keys()
                    or pending["input_hash"] != r._hash()
                    or not isinstance(findings, list) or not findings):
                raise ValueError("stale state or empty repairs")
            mapped = set()
            for finding in findings:
                if (not isinstance(finding, dict) or not isinstance(finding.get("issue"), str)
                        or not finding["issue"].strip() or finding.get("routing_unresolved")):
                    raise ValueError("invalid finding")
                if "claim_ids" in finding:
                    id_list(finding["claim_ids"], "pending finding IDs")
                if "claim_id" in finding and (not isinstance(finding["claim_id"], str) or not finding["claim_id"].strip()):
                    raise ValueError("invalid finding ID")
                targets = r._finding_claim_ids(finding)
                if not targets:
                    raise ValueError("unmapped finding")
                mapped.update(targets)
            if mapped != set(ids):
                raise ValueError("finding targets differ from allowed IDs")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"invalid pending claim repair: {exc}") from exc
        r.pending_patch = pending
    elif data.get("status") in {"blocked:revision_no_changes", "blocked:pending_revision"}:
        raise ValueError("missing pending claim repair for blocked revision")
    # Never restore serialized receipts. Same-execution continuation replays
    # exact request archives through all semantic-contract validators. A changed
    # canonical ancestor, source, schema, model or policy gets a different key.
    r.status = run.stage = (data["status"] if r.pending_patch and data.get("status") == "blocked:revision_no_changes"
                           else "blocked:pending_revision" if r.pending_patch else "resumed:requires_verification")
    return r
