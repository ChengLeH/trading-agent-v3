"""Explicit, LLM-reviewed migration of legacy catalog-sensitive Claims."""
from copy import deepcopy
import json

from .schema import claim_index, id_list


def prepare_catalog_scope_migration(workflow, claim_ids, *, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('operator reason required')
    ids = id_list(claim_ids, 'claim IDs')
    if not ids:
        raise ValueError('at least one Claim ID is required')
    with workflow.queue.transaction() as state:
        runner = workflow._load(state)
        if runner.pending_patch is not None:
            raise ValueError('finish the current pending patch first')
        index = claim_index(runner.run.claims)
        if set(ids) - index.keys():
            raise ValueError('unknown Claim ID')
        if any('catalog_scope' in index[cid]['assertion'] for cid in ids):
            raise ValueError('migration is only for Claims without an existing scope')
        findings = [dict(claim_id=cid, category='catalog_scope_migration',
            resolution_route='claim_patch',
            issue='判斷此 Claim 是否依賴整體來源目錄或缺席資訊；不得猜測範圍。',
            evidence=dict(selection_reason=reason.strip()),
            suggested_fix='只在 assertion 增加 catalog_scope；完全依引用與前提才選 referenced_sources，'
                          '否則選 full_catalog。保留其他欄位與所有依賴，不得刪除內容。') for cid in ids]
        outcome = runner.prepare_phase6_revision_v4(findings)
        if 'revision_prompts' in outcome:
            role, prompt = outcome['revision_prompts'][0]
            prompt += ('\n\n這次只允許修訂以下選定 Claim 的 assertion.catalog_scope：'
                       + json.dumps(ids, ensure_ascii=False)
                       + '。每個選定 Claim 都必須回傳一次，patch 只能有 claim_id 和完整 assertion，'
                         'assertion 其他欄位必須原樣保留；不要提交其他 Claim。')
            outcome['revision_prompts'] = [(role, prompt)]
            state['patch_prompt'] = prompt
            state['catalog_scope_migration'] = dict(input_hash=runner.pending_patch['input_hash'], ids=ids)
            active = state.get('packets', {}).get(state.get('active_packet_id'))
            if active and not active.get('retired'):
                active.update(retired=True, retirement_reason='explicit_catalog_scope_migration')
            state['active_packet_id'] = None
        workflow._save(state, runner)
        return {'status': runner.status, **outcome}


def validate_scope_patches(runner, patches, migration):
    """Prevent scope migration from silently editing the underlying analysis."""
    if migration.get('input_hash') != runner._hash():
        raise ValueError('stale catalog scope migration')
    index = claim_index(runner.run.claims)
    allowed = set(migration.get('ids', []))
    submitted_ids = [patch.get('claim_id') for patch in patches if isinstance(patch, dict)]
    if len(submitted_ids) != len(allowed) or set(submitted_ids) != allowed:
        raise ValueError('scope migration must cover all selected Claim IDs exactly once')
    for patch in patches:
        if set(patch) != {'claim_id', 'assertion'} or patch.get('claim_id') not in allowed:
            raise ValueError('scope migration can change only assertion.catalog_scope')
        old = index[patch['claim_id']]['assertion']
        new = patch['assertion']
        if not isinstance(new, dict) or new.get('catalog_scope') not in ('referenced_sources', 'full_catalog'):
            raise ValueError('scope migration requires an explicit valid catalog_scope')
        rest = deepcopy(new)
        rest.pop('catalog_scope')
        if rest != old:
            raise ValueError('scope migration cannot change other assertion fields')
