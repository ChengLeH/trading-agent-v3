"""V4-only typed cash-flow extraction. Never infer meaning from prose."""
from copy import deepcopy
from v3.facts import Fact, FactStore, CalcStore
from .schema import digest

FIELDS = {'OperatingCashFlow': ('net_cash_from_operating_activities', 'operating_cash_flow'),
          'Capex': ('purchase_of_plant_property_and_equipment', 'capex'),
          'ShareRepurchases': ('share_repurchases',)}


def extract_cash_flow(record):
    rows = record.payload.get('cash_flow', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f'{record.id}: cash_flow must be a list of row objects')
    facts, calcs, limitations, coverage = [], [], [], []
    convention = record.payload.get('cash_flow_capex_convention')
    if convention not in (None, 'signed_cash_flow', 'positive_outflow'):
        raise ValueError(f'{record.id}: unsupported cash_flow_capex_convention')
    for i, row in enumerate(rows):
        if row.get('fiscal_period') not in (None, 'FY'):
            raise ValueError(f'{record.id}: annual cash_flow extraction requires fiscal_period FY')
        year = row.get('fiscal_year')
        if type(year) is not int:
            raise ValueError(f'{record.id}: cash_flow[{i}].fiscal_year must be an integer')
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
            fact = Fact(f'fact.{concept}.FY{year}', value, 'USD', f'FY{year}',
                fiscal_year=year, fiscal_period=row.get('fiscal_period', 'FY'),
                concept=concept, basis='gaap', scope='consolidated',
                source_id=record.id, source_hash=record.source_hash,
                source_pointer=f'cash_flow[{i}].{field}')
            facts.append(fact); values[concept] = fact
        coverage.append(dict(row=i, fiscal_year=year, produced_concepts=list(values), normalizations=transforms))
        if not values:
            limitations.append(f'{record.id}: cash_flow[{i}] has data but no supported fields; no facts created')
        if 'OperatingCashFlow' in values and 'Capex' in values:
            value = values['OperatingCashFlow'].value + values['Capex'].value
            facts.append(Fact(f'fact.FreeCashFlow.FY{year}', value, 'USD', f'FY{year}',
                fiscal_year=year, fiscal_period='FY', concept='FreeCashFlow', basis='gaap', scope='consolidated',
                source_id=record.id, source_hash=record.source_hash, source_pointer='derived: OCF+signed_capex'))
            calcs.append(dict(cid=f'calc.fcf.FY{year}', value=value, calc_type='fcf',
                input_refs=[values['OperatingCashFlow'].fact_id, values['Capex'].fact_id], unit='USD',
                meta=dict(capex_convention=convention or 'signed_cash_flow')))
        else:
            limitations.append(f'{record.id}: FY{year} FCF unavailable: OperatingCashFlow and signed Capex required')
    digest([fact.to_dict() for fact in facts])  # rejects NaN/infinity before mutation
    return facts, calcs, dict(section='cash_flow', source_id=record.id,
        source_hash=record.source_hash, input_rows=len(rows), produced_facts=len(facts),
        coverage=coverage, limitations=limitations)


def build_with_cash_flow(orchestrator, run_id, build_legacy):
    run = orchestrator.runs[run_id]
    additions = [(rec, *extract_cash_flow(rec)) for rec in run.records
                 if getattr(rec, 'type', None) == 'openbb_fundamentals']
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
