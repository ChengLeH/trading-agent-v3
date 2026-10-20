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


def append_sources(workflow, manifest_path, *, reason):
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
        carry = deepcopy(runner.deferred_global_findings)
        if runner.pending_patch:
            carry.extend(deepcopy(runner.pending_patch['findings']))
        by_id = {f.get('finding_id') or 'recovery.' + digest(f): f for f in carry}
        for fid, finding in by_id.items():
            finding.setdefault('finding_id', fid)
        runner.deferred_global_findings = list(by_id.values())
        runner.pending_patch = runner.pending_global = runner.global_receipt = runner.global_review_progress = None
        runner.pending_atomic = runner.pending_deep = None
        runner.reference_supplements = {'atomic': {}, 'deep': {}}
        runner._tasks()
        runner.status = runner.run.stage = 'atomic'
        for packet in state.get('packets', {}).values():
            if not packet.get('retired'):
                packet.update(retired=True, retirement_reason='canonical_evidence_added')
        state['active_packet_id'] = None
        for key in ('patch_prompt', 'accepted_links', 'link_blocked', 'patch_blocked', 'review_blocked', 'global_blocked'):
            state.pop(key, None)
        event = dict(source_ids=new_ids, canonical_refs=sorted(additions), reason=reason.strip(),
                     before_hash=before_hash, after_hash=runner._hash())
        state.setdefault('evidence_additions', []).append(event)
        workflow._save(state, runner)
        return dict(status='evidence_added', **event,
                    next_action='complete_outstanding_reviews_then_repair_evidence_claims',
                    findings_resolved=False)


def prepare_evidence_patch(workflow, claim_ids, *, reason):
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
        if not state.get('evidence_additions'):
            raise ValueError('append evidence before requesting an evidence repair')
        if runner.pending_patch is not None:
            raise ValueError('finish the current pending patch first')
        findings = [dict(claim_id=cid, category='evidence_recovery', resolution_route='claim_patch',
            issue='主 Agent 要求核對新增證據對此主張的影響；不是已認定錯誤',
            evidence=dict(addition=deepcopy(state['evidence_additions'][-1])),
            suggested_fix=reason.strip()) for cid in ids]
        from .pipeline import source_catalog
        catalog = source_catalog(runner.run)
        findings[0]['evidence']['sources'] = {ref: deepcopy(catalog[ref])
            for ref in state['evidence_additions'][-1]['canonical_refs']}
        outcome = runner.prepare_phase6_revision_v4(findings)
        if 'revision_prompts' in outcome:
            runner.pending_global = runner.global_review_progress = None
            active = state.get('packets', {}).get(state.get('active_packet_id'))
            if active and not active.get('retired'):
                active.update(retired=True, retirement_reason='explicit_evidence_claim_repair')
            state['active_packet_id'] = None
        workflow._save(state, runner)
        return dict(status=runner.status, **outcome)
