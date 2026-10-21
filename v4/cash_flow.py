"""V4-only typed cash-flow extraction. Never infer meaning from prose."""
from copy import deepcopy
from collections import Counter
import hashlib
from v3.facts import Fact, FactStore, CalcStore
from .schema import digest
from .financial_data import (statement_metadata, period_dimensions, financial_fact,
                             disambiguate_fact_ids)

FIELDS = {'OperatingCashFlow': ('net_cash_from_operating_activities', 'operating_cash_flow'),
          'Capex': ('purchase_of_plant_property_and_equipment', 'capex'),
          'ShareRepurchases': ('share_repurchases',)}


def extract_cash_flow(record):
    rows = record.payload.get('cash_flow', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f'{record.id}: cash_flow must be a list of row objects')
    facts, calcs, limitations, coverage = [], [], [], []
    seen_fact_ids = set()
    metadata = statement_metadata(record)
    convention = record.payload.get('cash_flow_capex_convention')
    if convention not in (None, 'signed_cash_flow', 'positive_outflow'):
        raise ValueError(f'{record.id}: unsupported cash_flow_capex_convention')
    for i, row in enumerate(rows):
        try:
            dims = period_dimensions(row, 'cash_flow', metadata)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{record.id}: cash_flow[{i}] {exc}') from None
        year, period = dims['fiscal_year'], dims['period']
        values, transforms = {}, []
        for concept, aliases in FIELDS.items():
            present = [key for key in aliases if row.get(key) is not None]
            if not present:
                continue
            if any(type(row[key]) not in (int, float) for key in present):
                raise ValueError(f'{record.id}: cash_flow[{i}] {concept} must be numeric')
            if len({row[key] for key in present}) > 1:
                raise ValueError(f'{record.id}: cash_flow[{i}] conflicting aliases for {concept}: {present}')
            field = present[0]; value = row[field]
            if concept == 'Capex':
                if convention == 'positive_outflow':
                    if value < 0:
                        raise ValueError(f'{record.id}: negative Capex conflicts with positive_outflow convention')
                    value = -value
                elif value > 0 and convention != 'signed_cash_flow':
                    raise ValueError(f'{record.id}: positive Capex requires explicit cash_flow_capex_convention')
            if value != row[field]:
                transforms.append(dict(field=field, input_value=row[field], canonical_value=value,
                                       method='positive_outflow_to_signed_cash_flow'))
            fact = financial_fact(record, row, metadata, 'cash_flow', i, field, concept,
                         'USD', value, period_info=dims)
            if fact.fact_id in seen_fact_ids:
                discriminator = "|".join((record.id, fact.source_pointer,
                    fact.accession_or_version, fact.source_hash))
                suffix = hashlib.sha256(discriminator.encode('utf-8')).hexdigest()[:10]
                fact.fact_id = f'{fact.fact_id}.{suffix}'
                limitations.append(f'{record.id}: repeated {concept} fact for {period}; '
                    'source rows are retained with separate identities')
            seen_fact_ids.add(fact.fact_id)
            facts.append(fact); values[concept] = fact
        coverage.append(dict(row=i, fiscal_year=year, fiscal_period=dims['fiscal_period'],
            period=period, period_type=dims['period_type'], period_coverage=dims['period_coverage'],
            start=dims['start'], end=dims['end'], published_or_filing_time=dims['published_or_filing_time'],
            accession_or_version=dims['accession_or_version'], produced_concepts=list(values),
            normalizations=transforms))
        if not values:
            limitations.append(f'{record.id}: cash_flow[{i}] has data but no supported fields; no facts created')
        if 'OperatingCashFlow' in values and 'Capex' in values:
            value = values['OperatingCashFlow'].value + values['Capex'].value
            ocf, capex = values['OperatingCashFlow'], values['Capex']
            if (ocf.unit, ocf.basis, ocf.scope, ocf.entity, ocf.period) != (
                    capex.unit, capex.basis, capex.scope, capex.entity, capex.period):
                limitations.append(f'{record.id}: cash_flow[{i}] FCF unavailable: OCF and Capex dimensions differ')
                continue
            fcf = financial_fact(record, row, metadata, 'cash_flow', i, 'derived:OCF+Capex',
                'FreeCashFlow', ocf.unit, value, period_info=dims,
                source_pointer=f'derived:cash_flow[{i}].ocf+capex')
            facts.append(fcf)
            calcs.append(dict(cid=f'calc.fcf.{period}' + ('' if dims['fiscal_period'] == 'FY' else
                '.' + __import__('hashlib').sha256(record.id.encode('utf-8')).hexdigest()[:10]),
                value=value, calc_type='fcf',
                input_refs=[ocf.fact_id, capex.fact_id], unit=ocf.unit,
                meta=dict(capex_convention=convention or 'signed_cash_flow')))
        else:
            limitations.append(f'{record.id}: {period} FCF unavailable: OperatingCashFlow and signed Capex required')
    digest([fact.to_dict() for fact in facts])  # rejects NaN/infinity before mutation
    return facts, calcs, dict(section='cash_flow', source_id=record.id,
        source_hash=record.source_hash, input_rows=len(rows), produced_facts=len(facts),
        coverage=coverage, limitations=limitations)


def build_with_cash_flow(orchestrator, run_id, build_legacy, source_records=None):
    run = orchestrator.runs[run_id]
    source_records = run.records if source_records is None else source_records
    additions = [(rec, *extract_cash_flow(rec)) for rec in source_records
                 if getattr(rec, 'type', None) == 'openbb_fundamentals']
    disambiguate_fact_ids(additions)
    calc_counts = Counter(calc['cid'] for _, _, calcs, _ in additions for calc in calcs)
    calc_occurrences = Counter()
    for rec, _, calcs, diagnostic in additions:
        for calc in calcs:
            old_cid = calc['cid']
            if calc_counts[old_cid] <= 1:
                continue
            calc_occurrences[old_cid] += 1
            if calc_occurrences[old_cid] == 1:
                diagnostic['limitations'].append(f'{rec.id}: {old_cid} has multiple same-period source calculations; '
                    'the first retains the canonical ID for compatibility')
                continue
            discriminator = '|'.join((rec.id, old_cid, *calc['input_refs']))
            suffix = hashlib.sha256(discriminator.encode('utf-8')).hexdigest()[:10]
            calc['cid'] = f'{old_cid}.{suffix}'
            diagnostic['limitations'].append(f'{rec.id}: {old_cid} has multiple same-period source calculations; '
                f'this version is stored as {calc["cid"]}')
    # Remove only cash-flow resources owned by these same source records.
    # Updating/removing an input row must not leave stale cash-flow facts.
    source_ids = {item[0].id for item in additions}
    cash_concepts = set(FIELDS) | {'FreeCashFlow'}
    existing = run.fact_store.to_dict()
    removed = {fid for fid, row in existing.items() if row['source_id'] in source_ids
               and row['concept'] in cash_concepts}
    facts = FactStore()
    for fid, row in existing.items():
        if fid not in removed: facts.add(Fact(**row))
    calcs = CalcStore()
    for cid, row in run.calc_store.to_dict().items():
        if row['calc_type'] == 'fcf' and set(row['input_refs']).intersection(removed): continue
        calcs.add(cid,row['value'],row['calc_type'],row['input_refs'],row['unit'],row['meta'])
    run.fact_store, run.calc_store = facts, calcs
    original = run.records
    # V3 remains untouched. Preserve its raw source payload and hash: only a
    # private builder view excludes the section now handled by V4.
    run.records = deepcopy(original)
    for rec in run.records:
        if getattr(rec, 'type', None) == 'openbb_fundamentals':
            rec.payload['cash_flow'] = []
    try:
        report = build_legacy(run_id)
    finally:
        run.records = original
    for rec, facts, calcs, diagnostic in additions:
        for fact in facts:
            run.fact_store.add(fact)
        for calc in calcs:
            run.calc_store.add(calc['cid'], calc['value'], calc['calc_type'], calc['input_refs'], calc['unit'], calc['meta'])
        if not hasattr(rec, 'meta'):
            rec.meta = {}
        rec.meta['cash_flow_extraction'] = diagnostic
        report['facts'] += len(facts); report['calcs'] += len(calcs)
        report['limitations'].extend(diagnostic['limitations'])
    report['cash_flow_coverage'] = [item[-1] for item in additions]
    return report
