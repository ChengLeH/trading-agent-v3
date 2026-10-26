"""Typed, local review diagnostics; the complete inventory stays in the snapshot.

No Claim, finding reason, or source prose is used for routing. Global counts and
hashes deliberately live outside item fingerprints: unrelated diagnostics must
not invalidate a completed local review.
"""
from collections import Counter
from copy import deepcopy
import json

from .schema import digest

REVIEW_CONTEXT_FORMAT = 'v4-review-context/2'
DIAGNOSTICS_FORMAT = 'v4-review-diagnostics/1'
# Compatibility baseline for small projections, not a default dispatch ceiling.
# Explicit max_bytes remains a strict diagnostic-preview cap.
RELATED_DIAGNOSTICS_MAX_BYTES = 16384
DIAGNOSTICS_INSTRUCTIONS = '''
ingestion_diagnostics 是 summary+relevant 的局部診斷，不是整份 ingestion inventory。
findings 保留結構化關聯項目的完整原文；sha256／計數只描述這個局部投影。
完整全域診斷留在 run snapshot 與 global 審查，不得因本包未列出就推定沒有其他限制。
truncation 若有 omitted_count，表示診斷未完整附上，不是問題已解決；若缺漏可能影響
本 Claim 的判斷，回 SUSPICIOUS 並指明所需診斷，不能猜 PASS。禁止從診斷 prose 反搜引用。
'''.strip()


def canonical_bytes(value):
    # Same encoding as schema.digest, so the referenced full snapshot is verifiable.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def diagnostics_index(diagnostics):
    """One shared index for the full snapshot/sidecar, never copied into each item."""
    checks, statuses, sections = Counter(), Counter(), []
    for section in diagnostics:
        rows = section.get('findings', []) + section.get('requirements', [])
        checks.update(row.get('check_id', f"{section.get('section', 'unknown')}.requirements") for row in rows)
        statuses.update(row.get('status', 'unknown') for row in rows)
        sections.append(dict(section=section.get('section', 'unknown'),
                             bytes=len(canonical_bytes(section)), findings=len(rows)))
    return dict(format=DIAGNOSTICS_FORMAT, mode='full-index', sha256=digest(diagnostics),
                total_bytes=len(canonical_bytes(diagnostics)),
                counts_by_check_id=dict(sorted(checks.items())),
                counts_by_status=dict(sorted(statuses.items())), sections=sections)


_REF_FIELDS = {'ref_id', 'ref_ids', 'fact_id', 'fact_refs', 'fact_refs_used', 'source_id',
               'source_ref', 'source_refs', 'evidence_refs', 'refs', 'claim_id',
               'claim_ids', 'dependencies', 'target_claim_ids', 'unknown_entity_fact_refs',
               'unverified_fact_refs'}


def _identities(value):
    entities, refs = set(), set()
    def visit(node):
        if isinstance(node, list):
            for row in node:
                visit(row)
        elif isinstance(node, dict):
            for key, item in node.items():
                if key == 'entity' or key in _REF_FIELDS:
                    target = entities if key == 'entity' else refs
                    values = item if isinstance(item, list) else [item]
                    if key == 'entity':
                        target.update(v.strip() for v in values if isinstance(v, str)
                                      and v.strip() and v.strip().casefold() != 'unknown')
                    else:
                        target.update(v for v in values if isinstance(v, str) and v)
                if isinstance(item, (dict, list)):
                    visit(item)
    visit(value)
    return entities, refs


def project_diagnostics(diagnostics, claims, resolved, *, max_bytes=None):
    """Select by declared entity/ID and deliver complete diagnostics by default.

    The caller's actual wire budget bounds dispatch, not a hidden projection cap.
    Explicit max_bytes is a bounded preview: FAILs are never dropped and any
    omission/overflow still requires scoped review, never implies complete proof.
    """
    cap = RELATED_DIAGNOSTICS_MAX_BYTES if max_bytes is None else max_bytes
    if type(cap) is not int or cap < 256:
        raise ValueError('related diagnostics budget must be >=256')
    entities, refs = _identities(claims)
    extra_entities, extra_refs = _identities(list(resolved.values()))
    entities |= extra_entities
    refs |= extra_refs | set(resolved)
    def relevant(row, source_id=None):
        other_entities, other_refs = _identities(row)
        if source_id:
            other_refs.add(source_id)
        return bool(entities & other_entities or refs & other_refs)

    selected = []
    for section in diagnostics:
        name, source = section.get('section', 'unknown'), section.get('source_id')
        def reports_for(row):
            dimensions = ('entity', 'period', 'category')
            if not all(row.get(key) not in (None, '', 'unknown') for key in dimensions):
                return []
            return [report for report in section.get('latest', [])
                    if all(report.get(key) == row[key] for key in dimensions)]
        for collection in ('findings', 'requirements'):
            for row in section.get(collection, []):
                reports = reports_for(row)
                if relevant(row, source) or any(relevant(report) for report in reports):
                    envelope = dict(section=name, collection=collection,
                        check_id=row.get('check_id', f'{name}.{collection}'),
                        status=row.get('status', 'WARN'), finding=deepcopy(row))
                    if reports:
                        envelope['report_context'] = sorted(deepcopy(reports), key=canonical_bytes)
                    selected.append(envelope)
        # Source-level assumptions/limitations are review evidence too. Inventory
        # coverage/produced_facts are not reattached: bound objects carry dimensions.
        if source in refs:
            metadata = {key: deepcopy(value) for key, value in section.items()
                        if key not in {'findings', 'coverage', 'latest', 'requirements'} }
            if metadata:
                selected.append(dict(section=name, collection='source_context',
                    check_id=f'{name}.source_context', status='WARN', finding=metadata))
    selected.sort(key=lambda row: (0 if row['status'] == 'FAIL' else 1,
                                  row['check_id'], canonical_bytes(row)))
    failures = [row for row in selected if row['status'] == 'FAIL']
    kept = list(failures)
    warnings = [row for row in selected if row['status'] != 'FAIL']
    def marker(omitted):
        return dict(truncated=bool(omitted), omitted_count=len(omitted),
                    omitted_check_ids=sorted({row['check_id'] for row in omitted}))
    if max_bytes is None:
        # A local WARN may be nonblocking at ingestion but is still evidence for
        # semantic review. Do not omit it before the real request budget check.
        # Keep small projections byte-identical so accepted siblings stay valid.
        cap = max(cap, len(canonical_bytes(dict(findings=selected, truncation=marker([])))))
        kept, remaining = selected, []
    else:
        # Only bounded previews need prefix packing; full delivery must not
        # repeatedly serialize growing prefixes of an already complete set.
        remaining = list(warnings)
        for offset, row in enumerate(warnings):
            candidate = kept + [row]
            tail = warnings[offset + 1:]
            if len(canonical_bytes(dict(findings=candidate, truncation=marker(tail)))) > cap:
                break
            kept, remaining = candidate, tail
    truncation = marker(remaining)
    delivered_bytes = len(canonical_bytes(dict(findings=kept, truncation=truncation)))
    overflow = delivered_bytes > cap
    checks = Counter(row['check_id'] for row in kept)
    statuses = Counter(row['status'] for row in kept)
    sections = [dict(section=name, bytes=len(canonical_bytes([
                    row for row in selected if row['section'] == name])),
                    findings=sum(row['section'] == name for row in selected))
                for name in sorted({row['section'] for row in selected})]
    return dict(format=DIAGNOSTICS_FORMAT, mode='summary+relevant',
                sha256=digest(selected), total_bytes=len(canonical_bytes(selected)),
                counts_by_check_id=dict(sorted(checks.items())),
                counts_by_status=dict(sorted(statuses.items())), sections=sections, findings=kept,
                truncation=truncation, findings_bytes=delivered_bytes, budget_bytes=cap,
                overflow=overflow, requires_scoped_review=overflow or bool(remaining))
