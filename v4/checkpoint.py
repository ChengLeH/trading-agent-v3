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
from .financial_review import FINANCIAL_REVIEW_VERSION
from .source_lineage import validate_run_source_lineage


from .storage import write_json


def _load_review_row(run_dir, source, *, run_id, model, layer, claim_id, task_hash,
                     verification_epoch=None, allowed_agent_policies=None,
                     first_review_hash=None, reference_parents=None):
    from .semantic_contracts import SEMANTIC_REVIEW_CONTRACT
    if isinstance(source, dict) and source.get("format") == "v4-reference-composition/1":
        if (set(source) != {"format", "claim_id", "layer", "sources", "verdict_source"}
                or source["claim_id"] != claim_id or source["layer"] != layer
                or not isinstance(source["sources"], list) or not source["sources"]
                or source["verdict_source"] != source["sources"][-1]):
            return None
        rows, checks, seen = {}, [], set()
        for index, part in enumerate(source["sources"]):
            # Only raw attempts may contribute; recursive compositions and
            # duplicate attempts cannot manufacture additional reference checks.
            if (not isinstance(part, dict) or set(part) != {
                    "attempt_id", "metadata_hash", "response_hash", "claim_id", "layer"}
                    or not isinstance(part["attempt_id"], str) or part["attempt_id"] in rows):
                return None
            row = _load_review_row(run_dir, part, run_id=run_id, model=model, layer=layer,
                claim_id=claim_id, task_hash=task_hash, verification_epoch=verification_epoch,
                allowed_agent_policies=allowed_agent_policies, first_review_hash=first_review_hash,
                reference_parents=source["sources"][:index] if index == len(source["sources"]) - 1 else None)
            if row is None or not isinstance(row.get("reference_checks", []), list):
                return None
            rows[part["attempt_id"]] = row
            for check in row.get("reference_checks", []):
                if not isinstance(check, dict) or not isinstance(check.get("ref_id"), str) or check["ref_id"] in seen:
                    return None
                seen.add(check["ref_id"])
                checks.append(deepcopy(check))
        result = deepcopy(rows[source["verdict_source"]["attempt_id"]])
        result["reference_checks"] = checks
        return result
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
    if (record.get("format") != "v4-response/1"
            or record.get("status") != "received" or record.get("attempt_id") != attempt_id
            or record.get("run_id") != run_id or record.get("model") != model
            or not isinstance(task_hashes, dict)
            or metadata_hash != source.get("metadata_hash")
            or response_hash != source.get("response_hash")
            or not isinstance(review, dict) or review.get("layer") != layer
            or review.get("contract") != SEMANTIC_REVIEW_CONTRACT
            or (verification_epoch is not None
                and review.get("verification_epoch") != verification_epoch)
            or (allowed_agent_policies is not None
                and record.get("agent_policy_sha256") not in allowed_agent_policies)
            or task_hashes.get(claim_id) != task_hash):
        return None
    if first_review_hash is not None:
        first_review_hashes = review.get("first_review_hashes")
        if (not isinstance(first_review_hashes, dict)
                or first_review_hashes.get(claim_id) != first_review_hash):
            return None
    if reference_parents is not None:
        parents = review.get("reference_parent_sources", {})
        if not isinstance(parents, dict) or parents.get(claim_id, []) != reference_parents:
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


def _verified_reference_supplement(run_dir, run_id, cid, task, supplement, layer, *,
                                  verification_epoch=None, allowed_agent_policies=None, first_review_hash=None):
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
                               layer=layer, claim_id=cid, task_hash=digest(task.to_dict()),
                               verification_epoch=verification_epoch, allowed_agent_policies=allowed_agent_policies,
                               first_review_hash=first_review_hash)
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
    write_json(path, dict(format="v4-checkpoint/1", financial_review_contract=FINANCIAL_REVIEW_VERSION,
                          contract_migrations=getattr(runner, "contract_migrations", []), state=runner.orch.snapshot(runner.run_id),
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
                          global_dependency_batch_size=runner.global_dependency_batch_size,
                          review_sources=runner._latest_review_sources,
                          audit=dict(atomic=runner.receipts, deep=runner.deep_receipts, global_review=runner.global_receipt)))


def _current_agent_policies():
    """Return only policy identities that this process can currently verify."""
    policies = {"external"}
    try:
        from .agy_session import load_agent_profile
        _, policy_hash, *_ = load_agent_profile()
    except Exception:
        return policies
    if isinstance(policy_hash, str) and len(policy_hash) == 64:
        policies.add(policy_hash)
    return policies


def _restore_review_receipts(runner, data, run_dir, tasks):
    """Rebuild accepted receipts exclusively from hash-verified response archives."""
    audit = data.get("audit")
    sources = data.get("review_sources")
    if (not isinstance(audit, dict) or not isinstance(sources, dict)
            or set(sources) != {"atomic", "deep"}):
        return
    allowed_policies = _current_agent_policies()
    from .atomic_phase6 import normalize_atomic_result
    from .semantic_contracts import review_ref_ids
    for layer, target, audit_key in (("atomic", runner.receipts, "atomic"),
                                     ("deep", runner.deep_receipts, "deep")):
        layer_sources = sources.get(layer)
        saved_receipts = audit.get(audit_key)
        if not isinstance(layer_sources, dict) or not isinstance(saved_receipts, dict):
            continue
        for cid, saved in saved_receipts.items():
            task = tasks.get(cid)
            source = layer_sources.get(cid)
            try:
                if (task is None or not isinstance(saved, dict)
                        or set(saved) != {"input_hash", "result", "model"}
                        or saved.get("input_hash") != digest(task.to_dict())):
                    continue
                model = saved.get("model")
                from .runner import require_model_identity
                require_model_identity(model)
                first_hash = None
                if layer == "deep":
                    atomic_receipt = runner.receipts.get(cid)
                    if (not isinstance(atomic_receipt, dict)
                            or atomic_receipt.get("result", {}).get("verdict") not in {"FAIL", "SUSPICIOUS"}):
                        continue
                    first_hash = digest(atomic_receipt["result"])
                row = _load_review_row(
                    run_dir, source, run_id=runner.run_id, model=model, layer=layer,
                    claim_id=cid, task_hash=digest(task.to_dict()),
                    verification_epoch=runner._verification_epoch,
                    allowed_agent_policies=allowed_policies, first_review_hash=first_hash)
                if row is None:
                    continue
                normalized = normalize_atomic_result(
                    cid, row, expected_ref_ids=review_ref_ids(task.claim))
                if normalized != saved.get("result"):
                    continue
                target[cid] = dict(input_hash=digest(task.to_dict()),
                                   result=normalized, model=model)
                runner._latest_review_sources[layer][cid] = deepcopy(source)
                runner._restored_receipt_ids[layer].add(cid)
            except (ValueError, TypeError, KeyError, AttributeError):
                # A stale or malformed archive disables reuse for this row only.
                continue


def load_checkpoint(path, *, continue_verification=True):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "v4-checkpoint/1":
        raise ValueError("unsupported V4 checkpoint")
    state = data["state"]
    r = PipelineRunnerV4(max_revisions=data["max_revisions"], max_workers=data["max_workers"],
                         review_batch_size=data.get("review_batch_size", 1),
                         max_contract_attempts=data.get("max_contract_attempts", 3),
                         review_batch_max_bytes=data.get("review_batch_max_bytes", 65536),
                         global_dependency_batch_size=data.get("global_dependency_batch_size", 12))
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
    try:
        run.source_lineage = validate_run_source_lineage(
            run, state.get("source_lineage", {}))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid checkpoint source_lineage: {exc}") from None
    run.trade = state.get("trade", {})
    run.trade_generated = state.get("trade_generated", ([], []))
    grouped = {}
    for c in state["claims"]:
        grouped.setdefault(c["author_role"], []).append(c)
    # Legacy malformed event bindings remain loadable for explicit Claim
    # repair; deterministic validation still blocks Global and delivery.
    r.orch.submit_batch(r.run_id, grouped, validate_events=False)
    r.reference_supplements = {"atomic": {}, "deep": {}}
    supplements = data.get("reference_supplements", {"atomic": {}, "deep": {}})
    if not isinstance(supplements, dict) or set(supplements) != {"atomic", "deep"}:
        raise ValueError("invalid reference supplement state")
    if continue_verification:
        from .atomic_phase6 import build_atomic_tasks, normalize_atomic_result
        from .semantic_contracts import review_ref_ids, normalize_reference_checks
        tasks = {task.claim_id: task for task in build_atomic_tasks(run)}
        _restore_review_receipts(r, data, Path(path).parent, tasks)
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
                    atomic = r.receipts.get(cid)
                    if layer == "deep" and (atomic is None or atomic["result"]["verdict"] == "PASS"):
                        continue
                    if not _verified_reference_supplement(
                            Path(path).parent, r.run_id, cid, tasks[cid], supplement, layer,
                            verification_epoch=r._verification_epoch, allowed_agent_policies=_current_agent_policies(),
                            first_review_hash=digest(atomic["result"]) if layer == "deep" else None):
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
    previous_state = r._state()
    prior_contract = data.get('financial_review_contract')
    if prior_contract is None:
        previous_state.pop('financial_review_contract', None)
    else:
        previous_state['financial_review_contract'] = prior_contract
    prior_hash = digest(previous_state)
    migrating = prior_contract != FINANCIAL_REVIEW_VERSION
    r.contract_migrations = deepcopy(data.get('contract_migrations', []))
    progress = data.get("global_review_progress")
    if migrating and isinstance(progress, dict) and progress.get('input_hash') == prior_hash:
        r.contract_migrations.append(dict(previous_contract=prior_contract,
            current_contract=FINANCIAL_REVIEW_VERSION, retired_global_progress=deepcopy(progress)))
        progress = None
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
    retire_pending = (migrating and isinstance(pending, dict) and pending.get('input_hash') == prior_hash)
    if retire_pending:
        pending = deepcopy(pending)
        pending['input_hash'] = r._hash()
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
        if retire_pending:
            r.contract_migrations.append(dict(previous_contract=prior_contract,
                current_contract=FINANCIAL_REVIEW_VERSION, retired_pending_patch=deepcopy(data['pending_patch'])))
            known_findings = {f.get('finding_id') for f in r.deferred_global_findings}
            for finding in deepcopy(findings):
                finding.setdefault('finding_id', 'upgrade.' + digest(finding))
                if finding['finding_id'] not in known_findings:
                    r.deferred_global_findings.append(finding)
                    known_findings.add(finding['finding_id'])
        else:
            r.pending_patch = pending
    elif data.get("status") in {"blocked:revision_no_changes", "blocked:pending_revision"}:
        raise ValueError("missing pending claim repair for blocked revision")
    # Review receipts above are reconstructed from exact response archives;
    # other audit snapshots remain diagnostic and are not accepted as proof.
    r.status = run.stage = (data["status"] if r.pending_patch and data.get("status") == "blocked:revision_no_changes"
                           else "blocked:pending_revision" if r.pending_patch else "resumed:requires_verification")
    return r
