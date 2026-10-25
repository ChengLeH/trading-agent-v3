"""Append-only evidence recovery for the native main Agent; no model or network calls."""
from copy import deepcopy
import json
from pathlib import Path
from v3.evidence import EvidencePacket
from v3.facts import Fact
from v3.sources import SourceError
from .schema import digest, claim_index


def augment_documents(run, report):
    """Retain full supplied text as evidence, not an inferred financial fact."""
    existing = run.evidence_store.to_dict()
    for rec in run.records:
        if getattr(rec, 'type', None) != 'generic_text':
            continue
        text = rec.payload['text']
        eid = 'evidence.document.' + digest(rec.id)
        packet = EvidencePacket(eid, text, rec.id, rec.type, 'full supplied text',
            company=rec.subject_ticker, entity=rec.subject_ticker,
            source_hash=rec.source_hash, extraction_method='verbatim_document')
        if eid in existing:
            if existing[eid] != packet.to_dict():
                raise ValueError('document evidence ID collision')
            continue
        run.evidence_store.add(packet)
        report['evidence'] = report.get('evidence', 0) + 1


def _evidence_request(runner, request_id):
    from .evidence_requests import is_request_stale, validate_work_log
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError('request_id must be a nonempty exact ID')
    records = validate_work_log(runner.evidence_requests)
    record = next((row for row in records if row['request_id'] == request_id), None)
    if record is None:
        raise ValueError('unknown evidence request_id')
    if is_request_stale(record, runner.run.claims):
        raise ValueError('evidence request is stale for the current Claim versions')
    return record


def record_evidence_search(workflow, request_id, outcome, *, query='', details=''):
    """Persist an operator search result without changing canonical evidence."""
    with workflow.queue.transaction() as state:
        runner = workflow._load(state)
        runner.record_evidence_search(request_id, outcome, query=query, details=details)
        workflow._save(state, runner)
        record = next(row for row in runner.evidence_requests if row['request_id'] == request_id)
        return dict(status='evidence_search_recorded', request_id=request_id,
                    search_result=deepcopy(record['search_results'][-1]),
                    resolution_status='unresolved', input_hash=runner._hash())


def append_sources(workflow, manifest_path, *, reason, request_id=None):
    """Validate new sources separately, then append in one canonical transaction.

    Existing IDs/values are never overwritten. The operator supplies a manifest
    with the same subject/as-of, and new source IDs. New information is not a
    semantic finding resolution: the main Agent must link it in a Claim patch
    and run all affected reviews plus Global again.
    """
    from .pipeline import OrchestratorV4, source_catalog
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('operator reason required')
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not isinstance(manifest.get('sources'), list) or not manifest['sources']:
        raise ValueError('evidence manifest requires nonempty sources')
    for source in manifest['sources']:
        if isinstance(source, dict) and isinstance(source.get('path'), str):
            source['path'] = str((path.parent / source['path']).resolve())
    if manifest.get('issuer_registry_path'):
        manifest['issuer_registry_path'] = str((path.parent / manifest['issuer_registry_path']).resolve())
    with workflow.queue.transaction() as state:
        runner = workflow._load(state)
        request = _evidence_request(runner, request_id) if request_id is not None else None
        run = runner.run
        if manifest.get('subject') != run.manifest['subject'] or manifest.get('as_of') != run.manifest['as_of']:
            raise ValueError('new evidence must retain exact subject and as_of; start a new run to change them')
        old_ids = {rec.id for rec in run.records if hasattr(rec, 'id')}
        new_ids = [source.get('id') for source in manifest['sources']]
        if len(new_ids) != len(set(new_ids)) or old_ids.intersection(new_ids):
            raise ValueError('new evidence requires distinct new source IDs')
        manifest.setdefault('issuer_registry_path', run.manifest.get('issuer_registry_path'))
        if manifest.get('issuer_registry_path') is None:
            manifest.pop('issuer_registry_path', None)
        orch = OrchestratorV4()
        rid = orch.start_run(run.ticker, manifest)
        records = orch.ingest(rid)
        if any(isinstance(rec, SourceError) for rec in records):
            raise ValueError('new evidence ingestion failed; canonical state unchanged')
        orch.require_issuer_identity(rid)
        orch.build_facts(rid)
        added_run = orch.runs[rid]
        additions = source_catalog(added_run)
        if not additions:
            raise ValueError('new evidence yielded no canonical sources')
        collisions = set(source_catalog(run)).intersection(additions)
        if collisions:
            raise ValueError(f'canonical source IDs already exist; do not overwrite: {sorted(collisions)}')
        findings, blocked = orch.validate_stage(rid, 'ingest')
        if blocked:
            raise ValueError(f'new evidence failed deterministic validation: {findings}')
        before_hash = runner._hash()
        shadow = deepcopy(run)
        shadow.records.extend(deepcopy(records))
        shadow.manifest.setdefault('sources', []).extend(deepcopy(manifest['sources']))
        for raw in added_run.fact_store.to_dict().values():
            shadow.fact_store.add(Fact(**raw))
        for cid, raw in added_run.calc_store.to_dict().items():
            shadow.calc_store.add(cid, raw['value'], raw['calc_type'], raw['input_refs'], raw['unit'], raw['meta'])
        for raw in added_run.evidence_store.to_dict().values():
            shadow.evidence_store.add(EvidencePacket(**raw))
        shadow.events.update(deepcopy(added_run.events))
        runner.orch.runs[runner.run_id] = shadow
        _reopen_evidence_reviews(workflow, state, runner, 'canonical_evidence_added',
                                 preserve_role_state=True)
        if request is not None:
            for source_id in new_ids:
                runner.record_evidence_source(request['request_id'], source_id)
        event = dict(source_ids=new_ids, canonical_refs=sorted(additions), reason=reason.strip(),
                     before_hash=before_hash, after_hash=runner._hash())
        if request is not None:
            event['request_id'] = request['request_id']
        state.setdefault('evidence_additions', []).append(event)
        workflow._save(state, runner)
        return dict(status='evidence_added', **event,
                    next_action='complete_outstanding_reviews_then_repair_evidence_claims',
                    findings_resolved=False)


def _reopen_evidence_reviews(workflow, state, runner, retirement_reason, *, preserve_role_state=False):
    """Preserve unresolved findings and archives while replacing their context."""
    carry = deepcopy(runner.deferred_global_findings)
    if runner.pending_patch:
        carry.extend(deepcopy(runner.pending_patch['findings']))
    by_id = {}
    for finding in carry:
        finding.setdefault('finding_id', 'recovery.' + digest(finding))
        by_id[finding['finding_id']] = finding
    runner.deferred_global_findings = list(by_id.values())
    runner.pending_patch = runner.pending_global = runner.global_receipt = runner.global_review_progress = None
    runner.pending_atomic = runner.pending_deep = None
    runner.reference_supplements = {'atomic': {}, 'deep': {}}
    runner._tasks()
    runner.status = runner.run.stage = 'atomic'
    for packet in state.get('packets', {}).values():
        if not packet.get('retired'):
            packet.update(retired=True, retirement_reason=retirement_reason)
    state['active_packet_id'] = None
    keys = ('patch_prompt', 'accepted_links', 'link_blocked', 'patch_blocked', 'review_blocked', 'global_blocked')
    if not preserve_role_state:
        keys += ('role_candidates', 'role_errors', 'group_error')
    for key in keys:
        state.pop(key, None)


def declare_financial_source(workflow, source_id, declarations, *, reason):
    """Explicit extraction defaults, separate from the immutable source payload.

    Row-level declarations still take precedence. This command cannot change
    values, currencies, dates, or a fact's explicitly declared entity.
    """
    from .financial_statements import augment_financial_statements
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('operator reason required')
    if (not isinstance(declarations, dict) or not declarations or set(declarations) - {'entity', 'basis'}
            or any(not isinstance(v, str) or not v.strip() for v in declarations.values())):
        raise ValueError('declare nonempty entity and/or accounting basis only')
    if declarations.get('basis', '').strip().casefold() in {'parent', 'consolidated', 'single_company', 'segment', 'unknown'}:
        raise ValueError('declare an accounting framework, not a scope or unknown basis')
    with workflow.queue.transaction() as state:
        runner = workflow._load(state)
        matches = [r for r in runner.run.records if getattr(r, 'id', None) == source_id
                   and getattr(r, 'type', None) == 'openbb_fundamentals']
        if len(matches) != 1:
            raise ValueError('source_id must identify one financial statement source')
        record = matches[0]
        before_hash = runner._hash()
        before = deepcopy(record.meta.get('financial_statement_declarations', {}))
        after = {**before, **declarations}
        if before == after:
            return dict(status='financial_declarations_unchanged', input_hash=before_hash)
        record.meta['financial_statement_declarations'] = after
        augment_financial_statements(runner.run, dict(facts=0, limitations=[]))
        # Declarations enter canonical/task hashes and the LLM context, even
        # when a source has no newly extracted equity values.
        _reopen_evidence_reviews(workflow, state, runner, 'financial_source_declarations_changed')
        event = dict(source_id=source_id, before=before, after=after, reason=reason.strip(),
                     before_hash=before_hash, after_hash=runner._hash(), verification_epoch=runner._verification_epoch)
        state.setdefault('financial_declaration_changes', []).append(event)
        workflow._save(state, runner)
        return dict(status='financial_declarations_updated', findings_resolved=False, **event)


def register_event_catalog(workflow, events, *, reason):
    """Replace an explicit typed catalog; registration is not semantic acceptance."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('operator reason required')
    if not isinstance(events, list):
        raise ValueError('event catalog must be a JSON array')
    with workflow.queue.transaction() as state:
        runner = workflow._load(state)
        before_hash = runner._hash()
        referenced = {ref for claim in runner.run.claims for ref in claim['refs']}
        supplied = {row.get('event_id') for row in events if isinstance(row, dict)}
        removed = (set(runner.run.events) - supplied).intersection(referenced)
        if removed:
            raise ValueError(f'cannot remove referenced events; patch their Claims first: {sorted(removed)}')
        runner.orch.register_events(runner.run_id, events)
        if runner._hash() == before_hash:
            return dict(status='events_unchanged', input_hash=before_hash)
        _reopen_evidence_reviews(workflow, state, runner, 'event_catalog_updated')
        event = dict(event_ids=[row['event_id'] for row in events], reason=reason.strip(),
                     before_hash=before_hash, after_hash=runner._hash())
        state.setdefault('event_catalog_changes', []).append(event)
        workflow._save(state, runner)
        return dict(status='events_registered', findings_resolved=False, **event)


def prepare_evidence_patch(workflow, claim_ids, *, reason, request_id=None, source_refs=None):
    """Explicit main-Agent repair scope; never guess Claim IDs from prose."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('operator reason required')
    ids = list(dict.fromkeys(claim_ids))
    if not ids or any(not isinstance(cid, str) or not cid for cid in ids):
        raise ValueError('explicit nonempty claim IDs required')
    with workflow.queue.transaction() as state:
        runner = workflow._load(state)
        if set(ids) - claim_index(runner.run.claims).keys():
            raise ValueError('unknown evidence repair Claim IDs')
        request = _evidence_request(runner, request_id) if request_id is not None else None
        if request is not None and ids != request['claim_ids']:
            raise ValueError('Claim IDs must exactly match the evidence request targets')
        from .pipeline import source_catalog
        from .schema import id_list
        catalog = source_catalog(runner.run)
        existing_refs = id_list(source_refs, 'existing source refs') if source_refs is not None else []
        if source_refs is not None:
            if not existing_refs or request is None:
                raise ValueError('existing-source repair requires nonempty source refs and an exact request_id')
            missing = set(existing_refs) - catalog.keys()
            if missing:
                raise ValueError(f'unknown canonical source refs: {sorted(missing)}')
        addition = None
        if existing_refs:
            selected_refs = existing_refs
        elif request is not None:
            addition = next((row for row in reversed(state.get('evidence_additions', []))
                             if row.get('request_id') == request['request_id']), None)
            if addition is None or not set(request['submitted_source_ids']).intersection(addition['source_ids']):
                raise ValueError('append canonical sources for this evidence request before requesting a patch')
        elif state.get('evidence_additions'):
            addition = state['evidence_additions'][-1]
        if not existing_refs:
            if addition is None:
                raise ValueError('append evidence or select existing canonical source refs before requesting an evidence repair')
            selected_refs = addition['canonical_refs']
        if runner.pending_patch is not None:
            raise ValueError('finish the current pending patch first')
        tasks = runner._tasks()
        missing_atomic = [cid for cid, task in tasks.items()
                          if cid not in runner.receipts
                          or runner.receipts[cid]['input_hash'] != digest(task.to_dict())]
        if missing_atomic:
            workflow._save(state, runner)
            return dict(status='reviews_pending', blocked=False, layer='atomic',
                        pending_claim_ids=missing_atomic, next_action='next')
        missing_deep = [cid for cid, task in tasks.items()
                        if runner.receipts[cid]['result']['verdict'] != 'PASS'
                        and (cid not in runner.deep_receipts
                             or runner.deep_receipts[cid]['input_hash'] != digest(task.to_dict()))]
        if missing_deep:
            workflow._save(state, runner)
            return dict(status='reviews_pending', blocked=False, layer='deep',
                        pending_claim_ids=missing_deep, next_action='next')
        findings = [dict(claim_id=cid, category='evidence_recovery', resolution_route='claim_patch',
            issue='主 Agent 要求核對所選證據對此主張的影響；來源存在不代表支持主張',
            evidence=dict(addition=deepcopy(addition), source_refs=list(selected_refs),
                          request_id=request_id, repair_mode='existing_evidence' if existing_refs else 'new_evidence'),
            suggested_fix=reason.strip()) for cid in ids]
        findings[0]['evidence']['sources'] = {ref: deepcopy(catalog[ref])
            for ref in selected_refs}
        outcome = runner.prepare_phase6_revision_v4(findings)
        if 'revision_prompts' in outcome:
            runner.pending_global = runner.global_review_progress = None
            active = state.get('packets', {}).get(state.get('active_packet_id'))
            if active and not active.get('retired'):
                active.update(retired=True, retirement_reason='explicit_evidence_claim_repair')
            state['active_packet_id'] = None
        workflow._save(state, runner)
        return {"status": runner.status, **outcome}
