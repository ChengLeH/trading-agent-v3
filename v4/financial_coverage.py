"""Explicit financial-report inventory and optional publication-aware requirements.

No prose, ticker calendars, filing-form assumptions, or freshness thresholds are
interpreted here. Existing Facts and Evidence supply typed period/publication
fields. A source may additionally declare ``meta.financial_report_coverage`` as
rows with entity, period, period_coverage and published_or_filing_time; this
allows an announced report to be received without pretending numbers were
extracted. Optional manifest.financial_coverage_requirements uses the same
fields plus source_refs, require_structured, purpose and optional display form.
Only an explicit requirement can produce FAIL; unknown metadata produces WARN.
"""
from copy import deepcopy
from datetime import date, datetime, time, timezone

from .precision import period as normalize_period


_EXTRACTORS = ('cash_flow_extraction', 'financial_statement_extraction',
               'financial_data_extraction')


def _timestamp(value, *, as_of=False):
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value),
                time.max if as_of else time.min, tzinfo=timezone.utc)
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # A timestamp without an offset does not establish an absolute instant.
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except ValueError:
        return None


def _dimensions(row):
    explicit = row.get('period')
    if not explicit and type(row.get('fiscal_year')) is int and row.get('fiscal_period'):
        explicit = f"FY{row['fiscal_year']}-{row['fiscal_period']}"
        if row['fiscal_period'] == 'FY':
            explicit = f"FY{row['fiscal_year']}"
    period = normalize_period(explicit) if isinstance(explicit, str) else None
    coverage = row.get('period_coverage', 'unknown')
    part = period.split('-')[-1] if period else None
    annual_period = bool(period and period.startswith('FY') and len(period) == 6)
    if ((coverage == 'annual' and part in {'Q1', 'Q2', 'Q3', 'Q4', 'H1', 'H2', 'YTD', 'TTM'})
            or (annual_period and coverage in {'single_period', 'year_to_date', 'rolling'})):
        category = 'unknown'
    elif coverage == 'annual' or annual_period:
        category = 'annual'
    elif coverage == 'rolling' or part == 'TTM':
        category = 'ttm'
    elif coverage == 'year_to_date' or part == 'YTD':
        category = 'year_to_date'
    elif coverage == 'single_period' and part in {'Q1', 'Q2', 'Q3', 'Q4'}:
        category = 'quarter'
    elif part in {'Q1', 'Q2', 'Q3', 'Q4', 'H1', 'H2'}:
        category = 'interim_unknown'
    else:
        category = 'unknown'
    entity = row.get('entity')
    return (entity if isinstance(entity, str) and entity and entity != 'unknown' else None,
            period, category)


def _store_rows(run, name):
    store = getattr(run, name, None)
    return store.to_dict() if store is not None else {}


def financial_coverage_diagnostics(run):
    """Return one optional-context diagnostic; never alter or reject a run.

    ``latest`` selects the newest explicit fiscal period per entity/coverage
    category available by as_of, then its latest known version, retaining ties.
    ``structured`` means at least one fact
    from the same report period/publication; it does not certify all metrics,
    comparability or complete statements. ``requirements`` can be consumed by
    the caller's existing gate, but missing declarations are never invented.
    """
    manifest = getattr(run, 'manifest', {}) or {}
    as_of = manifest.get('as_of')
    cutoff = _timestamp(as_of, as_of=True)
    entries, findings = [], []

    def add(row, source_id, origin, ref_id=None, entity=None):
        if not isinstance(row, dict):
            findings.append(dict(status='WARN', check_id='financial_coverage_metadata_unknown',
                                 source_id=source_id, reason='report coverage row is not an object'))
            return
        values = dict(row)
        if not values.get('entity') and entity:
            values['entity'] = entity
        subject, period, category = _dimensions(values)
        # Unrelated news and market-price observations are not report inventory.
        if category == 'unknown' and not (values.get('period_coverage') or values.get('fiscal_period')):
            return
        published = values.get('published_or_filing_time', '')
        stamp = _timestamp(published)
        availability = ('unknown' if stamp is None or cutoff is None else
                        'by_as_of' if stamp <= cutoff else 'after_as_of')
        entry = dict(entity=subject, period=period, category=category,
            period_coverage=values.get('period_coverage', 'unknown'),
            published_or_filing_time=published, source_id=source_id,
            ref_id=ref_id or source_id, origin=origin, availability=availability)
        entries.append(entry)
        if availability == 'unknown' or subject is None or period is None or category in {'unknown', 'interim_unknown'}:
            findings.append(dict(status='WARN', check_id='financial_coverage_metadata_unknown',
                source_id=source_id, ref_id=entry['ref_id'],
                reason='report publication, entity, period or quarter/YTD/TTM coverage is unknown'))

    received_refs = {getattr(rec, 'id', '') for rec in getattr(run, 'records', [])}
    facts = _store_rows(run, 'fact_store')
    received_refs.update(row.get('source_id', '') for row in facts.values())
    financial_sources = {rec.id for rec in getattr(run, 'records', [])
                         if getattr(rec, 'type', None) == 'openbb_fundamentals'
                         or any(key in (getattr(rec, 'meta', {}) or {}) for key in _EXTRACTORS)}
    for fid, row in facts.items():
        if (row.get('source_id') in financial_sources or row.get('period_coverage')
                in {'annual', 'single_period', 'year_to_date', 'rolling'}):
            add(row, row.get('source_id', ''), 'fact', fid)
    for eid, row in _store_rows(run, 'evidence_store').items():
        received_refs.update((eid, row.get('source_id', '')))
        add(row, row.get('source_id', ''), 'evidence', eid)
    for rec in getattr(run, 'records', []):
        meta = getattr(rec, 'meta', {}) or {}
        declarations = meta.get('financial_report_coverage', [])
        if isinstance(declarations, list):
            for row in declarations:
                add(row, rec.id, 'source_declaration', entity=getattr(rec, 'subject_ticker', None))
        elif declarations:
            findings.append(dict(status='WARN', check_id='financial_coverage_metadata_unknown',
                                 source_id=rec.id, reason='financial_report_coverage must be a list'))
        for key in _EXTRACTORS:
            extraction = meta.get(key)
            if not isinstance(extraction, dict):
                continue
            for row in extraction.get('coverage', []):
                add(row, rec.id, 'extraction', entity=getattr(rec, 'subject_ticker', None))

    def compatible(a, b):
        return (a['entity'] is not None and a['entity'] == b['entity']
                and a['period'] is not None and a['period'] == b['period']
                and a['category'] == b['category']
                and a['category'] not in {'unknown', 'interim_unknown'})

    eligible = [e for e in entries if e['availability'] == 'by_as_of'
                and e['entity'] is not None and e['period'] is not None]
    def report_order(entry):
        parts = entry['period'].removeprefix('FY').split('-')
        year = int(parts[0]) if len(parts[0]) == 4 and parts[0].isascii() and parts[0].isdigit() else 0
        part = parts[1] if len(parts) > 1 else ''
        ordinal = {'Q1': 1, 'Q2': 2, 'Q3': 3, 'Q4': 4, 'H1': 1, 'H2': 2}.get(part, 0)
        return year, ordinal, _timestamp(entry['published_or_filing_time'])

    groups = {}
    for e in eligible:
        if e['category'] == 'unknown':
            continue
        key = e['entity'], e['category']
        stamp = report_order(e)
        best = groups.get(key)
        if best is None or stamp > best:
            groups[key] = stamp
    latest = []
    seen = set()
    for e in eligible:
        key = e['entity'], e['category']
        if key not in groups or report_order(e) != groups[key]:
            continue
        identity = (*key, e['period'], _timestamp(e['published_or_filing_time']))
        if identity in seen:
            continue
        seen.add(identity)
        peers = [x for x in eligible if compatible(e, x)
                 and _timestamp(x['published_or_filing_time']) == _timestamp(e['published_or_filing_time'])]
        fact_refs = sorted({x['ref_id'] for x in peers if x['origin'] == 'fact'})
        latest.append(dict(entity=e['entity'], category=e['category'], period=e['period'],
            published_or_filing_time=e['published_or_filing_time'], received=True,
            structured=bool(fact_refs), fact_refs=fact_refs,
            source_refs=sorted({x['source_id'] for x in peers} or {e['source_id']})))
        if not fact_refs:
            findings.append(dict(status='WARN', check_id='latest_report_not_structured',
                entity=e['entity'], period=e['period'], category=e['category'],
                reason='known report is received without same-period/version structured facts'))

    requirements = []
    declared = manifest.get('financial_coverage_requirements', [])
    if not isinstance(declared, list):
        findings.append(dict(status='WARN', check_id='financial_coverage_requirements_unknown',
                             reason='financial_coverage_requirements must be a list'))
        declared = []
    for i, raw in enumerate(declared):
        raw = raw if isinstance(raw, dict) else {}
        entity, period, category = _dimensions(raw)
        pub = _timestamp(raw.get('published_or_filing_time'))
        refs = raw.get('source_refs')
        refs_valid = (isinstance(refs, list) and bool(refs)
                      and all(isinstance(x, str) and x for x in refs))
        target = dict(entity=entity, period=period, category=category)
        matched = [e for e in eligible if compatible(target, e)
            and refs_valid and (e['source_id'] in refs or e['ref_id'] in refs)
            and pub is not None and _timestamp(e['published_or_filing_time']) == pub]
        fact_refs = sorted({e['ref_id'] for e in matched if e['origin'] == 'fact'})
        received = bool(refs_valid and received_refs.intersection(refs))
        structured = bool(fact_refs)
        unknown_fact_publication = any(compatible(target, e) and e['origin'] == 'fact'
            and refs_valid and (e['source_id'] in refs or e['ref_id'] in refs)
            and e['availability'] == 'unknown' for e in entries)
        if (entity is None or period is None or category in {'unknown', 'interim_unknown'}
                or pub is None or cutoff is None or not refs_valid
                or type(raw.get('require_structured')) is not bool):
            status, reason = 'WARN', 'requirement_metadata_unknown'
        elif pub > cutoff:
            status, reason = 'WARN', 'report_after_as_of'
        elif not received:
            status, reason = 'FAIL', 'required_report_not_received'
        elif raw['require_structured'] and not structured and unknown_fact_publication:
            status, reason = 'WARN', 'structured_publication_unknown'
        elif raw['require_structured'] and not structured:
            status, reason = 'FAIL', 'required_report_not_structured'
        else:
            status, reason = 'PASS', 'required_report_covered'
        requirements.append(dict(index=i, requirement=deepcopy(raw), status=status,
            reason=reason, received=received, structured=structured, fact_refs=fact_refs))
    inventory = {}
    for entry in entries:
        identity = tuple(entry[key] for key in ('entity', 'period', 'category',
            'period_coverage', 'published_or_filing_time', 'source_id', 'origin', 'availability'))
        row = inventory.setdefault(identity, {**entry, 'ref_ids': []})
        if entry['ref_id'] not in row['ref_ids']:
            row['ref_ids'].append(entry['ref_id'])
    return [dict(section='financial_coverage', as_of=as_of, coverage=list(inventory.values()),
        latest=latest, requirements=requirements, findings=findings,
        limitations=['Inventory covers only explicit metadata in supplied sources and stores; '
            'absence is not proof that no report was published. Structured means at least one '
            'same-period/version fact, not complete statement or metric coverage. '
            'Annual, quarter, YTD, interim-unknown and TTM are not substitutes.'])]
