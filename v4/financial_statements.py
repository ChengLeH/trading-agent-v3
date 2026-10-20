"""Generic, deterministic financial-statement attribution and coverage checks.

The adapter recognizes only documented standardized keys. Source-specific
meaning must be declared in ``financial_statement_metadata`` or on a row.
Legacy facts and source payloads are retained verbatim.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import math

from v3.facts import Fact, FactStore
from .precision import number


ALIASES = {
    "ParentEquity": ("parent_equity", "equity_attributable_to_parent"),
    "NonRedeemableNCI": ("nonredeemable_nci", "non_redeemable_nci"),
    "RedeemableNCI": ("redeemable_nci", "redeemable_noncontrolling_interest"),
    "ConsolidatedEquity": ("consolidated_equity",),
}
KNOWN_INPUT_FIELDS = {key for aliases in ALIASES.values() for key in aliases} | {
    "fiscal_year", "fiscal_period", "unit", "currency", "basis", "precision",
    "equity_attribution", "statement_scope", "entity", "redeemable_nci_inclusion",
}
KNOWN_INPUT_FIELDS |= {
    # Existing deterministic V3 statement adapter fields. Coverage reports
    # these as already mapped and leaves their fact creation to the legacy builder.
    "total_revenue", "total_gross_profit", "total_operating_income", "net_income",
    "basic_eps", "diluted_eps", "cash_and_equivalents", "total_assets",
    "total_liabilities", "total_equity", "net_inventory", "long_term_debt",
    "short_term_debt", "current_portion_of_long_term_debt",
    "net_cash_from_operating_activities", "purchase_of_plant_property_and_equipment",
}
_BALANCE_FIELDS = {"total_assets", "total_liabilities", "total_equity", "cash_and_equivalents",
                   "net_inventory", "long_term_debt", "short_term_debt",
                   "current_portion_of_long_term_debt"}
_ATTRIBUTION_FIELDS = {field for aliases in ALIASES.values() for field in aliases}


def _diagnostic(record, section, row, status, check_id, reason, **extra):
    return dict(status=status, check_id=check_id, source_id=record.id,
                section=section, row=row, reason=reason, **extra)


def source_declarations(run):
    return {rec.id: deepcopy(rec.meta['financial_statement_declarations'])
            for rec in getattr(run, 'records', [])
            if getattr(rec, 'meta', {}).get('financial_statement_declarations')}


def extract_financial_statements(record):
    """Return explicit equity facts and source coverage diagnostics.

    Metadata contract: payload.financial_metadata (or the longer
    financial_statement_metadata spelling) may declare
    ``unit`` (or currency), ``basis``, ``precision`` (absolute source rounding
    increment), and ``entity``. Row values override metadata. ``total_equity``
    is recognized only when the row explicitly declares equity_attribution as
    ``parent`` or ``consolidated``.
    """
    payload = record.payload
    metadata = payload.get("financial_statement_metadata", payload.get("financial_metadata", {}))
    if not isinstance(metadata, dict):
        metadata = {}
    alternate_metadata = payload.get("financial_metadata")
    if ("financial_statement_metadata" in payload and isinstance(alternate_metadata, dict)
            and metadata != alternate_metadata):
        findings = [_diagnostic(record, "financial", None, "WARN", "financial_metadata_conflict",
                                "financial_metadata and financial_statement_metadata disagree; using financial_statement_metadata")]
    else:
        findings = []
    metadata = {**metadata, **getattr(record, 'meta', {}).get('financial_statement_declarations', {})}
    facts, coverage = [], []
    for section in ("income", "balance"):
        rows = payload.get(section, [])
        if not isinstance(rows, list):
            findings.append(_diagnostic(record, section, None, "WARN", "financial_source_shape",
                                        "statement section is not a list; attribution checks unavailable"))
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                findings.append(_diagnostic(record, section, index, "WARN", "financial_source_shape",
                                            "statement row is not an object"))
                continue
            year = row.get("fiscal_year")
            fiscal_period = row.get("fiscal_period", "FY")
            period = f"FY{year}" if fiscal_period == "FY" else f"FY{year}-{fiscal_period}"
            if type(year) is not int or not isinstance(fiscal_period, str) or not fiscal_period:
                findings.append(_diagnostic(record, section, index, "WARN", "financial_period_unknown",
                    "row lacks a valid fiscal year or period",
                    fields=sorted(k for k, v in row.items() if v is not None)))
                continue
            explicit = {k: v for k, v in row.items() if k not in KNOWN_INPUT_FIELDS and v is not None}
            produced = []
            values = {}
            if section == "income":
                misplaced = [key for key in ("total_equity",) + tuple(k for keys in ALIASES.values() for k in keys)
                             if row.get(key) is not None]
                if misplaced:
                    findings.append(_diagnostic(record, section, index, "WARN", "financial_field_section",
                        "equity attribution fields appear in the income statement section", fields=misplaced))
            declared_precision = row.get("precision", metadata.get("precision"))
            declared_basis = row.get('basis', metadata.get('basis', 'unknown'))
            if isinstance(declared_basis, str) and declared_basis.strip().casefold() in {'parent', 'consolidated', 'single_company', 'segment'}:
                findings.append(_diagnostic(record, section, index, 'WARN', 'financial_basis_scope_conflict',
                    'basis describes an accounting framework; declare scope separately, never infer gaap from a scope label'))
            if declared_precision is not None and (type(declared_precision) not in (int, float)
                                                    or not math.isfinite(declared_precision)
                                                    or declared_precision < 0):
                findings.append(_diagnostic(record, section, index, "WARN", "financial_precision_invalid",
                                            "precision must be a finite nonnegative source rounding increment"))
                declared_precision = None
            for concept, aliases in ALIASES.items():
                if section != "balance":
                    continue
                present = [key for key in aliases if row.get(key) is not None]
                if not present:
                    continue
                if any(type(row[k]) not in (int, float) or not math.isfinite(row[k]) for k in present):
                    findings.append(_diagnostic(record, section, index, "WARN", "financial_value_invalid",
                                                f"{concept} requires finite numeric values", fields=present))
                    continue
                if len({row[k] for k in present}) != 1:
                    findings.append(_diagnostic(record, section, index, "WARN", "financial_alias_conflict",
                                                f"conflicting explicit aliases for {concept}", fields=present))
                    continue
                value = row[present[0]]
                unit = row.get("unit", metadata.get("unit"))
                currency = row.get("currency", metadata.get("currency"))
                unit = unit or (currency if currency else None)
                basis = row.get("basis", metadata.get("basis", "unknown"))
                if not unit:
                    findings.append(_diagnostic(record, section, index, "WARN", "financial_unit_unknown",
                                                f"{concept} has no explicit unit or currency"))
                    continue
                fact_id = _fact_id(concept, period, record.id)
                fact = Fact(fact_id, value, unit, period,
                    fiscal_year=year, fiscal_period=fiscal_period, concept=concept, basis=basis,
                    scope="parent" if concept == "ParentEquity" else "consolidated",
                    source_id=record.id, source_pointer=f"{section}[{index}].{present[0]}",
                    source_hash=record.source_hash,
                    entity=row.get("entity", metadata.get("entity", record.subject_ticker or "")))
                facts.append(fact); produced.append(concept); values[concept] = fact
            if section == "balance" and row.get("total_equity") is not None:
                attribution = row.get("equity_attribution", metadata.get("equity_attribution"))
                if attribution in ("parent", "consolidated"):
                    concept = "ParentEquity" if attribution == "parent" else "ConsolidatedEquity"
                    unit = row.get("unit", metadata.get("unit")) or row.get("currency", metadata.get("currency"))
                    value = row["total_equity"]
                    if type(value) not in (int, float) or not math.isfinite(value) or not unit:
                        findings.append(_diagnostic(record, section, index, "WARN", "financial_value_invalid",
                                                    "declared total_equity attribution requires finite value and explicit unit"))
                    elif concept in values and values[concept].value != value:
                        findings.append(_diagnostic(record, section, index, "WARN", "financial_alias_conflict",
                            f"declared total_equity conflicts with the explicit {concept} field"))
                    elif concept not in values:
                        fact_id = _fact_id(concept, period, record.id)
                        fact = Fact(fact_id, value, unit, period, fiscal_year=year,
                            fiscal_period=fiscal_period, concept=concept, basis=row.get("basis", metadata.get("basis", "unknown")),
                            scope=attribution, source_id=record.id,
                            source_pointer=f"{section}[{index}].total_equity", source_hash=record.source_hash,
                            entity=row.get("entity", metadata.get("entity", record.subject_ticker or "")))
                        facts.append(fact); produced.append(concept); values[concept] = fact
                else:
                    findings.append(_diagnostic(record, section, index, "WARN", "equity_attribution_unknown",
                        "total_equity retained as legacy input; source did not declare parent or consolidated attribution"))
            if explicit:
                findings.append(_diagnostic(record, section, index, "WARN", "financial_fields_unmapped",
                    "nonempty source fields remain outside the standardized adapter", fields=sorted(explicit)))
            coverage.append(dict(section=section, row=index, fiscal_year=year, fiscal_period=fiscal_period,
                                 period=period,
                                 produced_concepts=produced, unmapped_fields=sorted(explicit),
                                 precision=declared_precision,
                                 redeemable_nci_inclusion=row.get("redeemable_nci_inclusion", metadata.get("redeemable_nci_inclusion"))))
    return facts, dict(section="financial_statements", source_id=record.id,
                       source_hash=record.source_hash, coverage=coverage, findings=findings)


def _fact_id(concept, period, source_id):
    suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:10]
    return f"fact.{concept}.{period}.{suffix}"


def _source_row_index(pointer):
    """Parse only the two typed financial fact pointer forms we emit/use."""
    if not isinstance(pointer, str):
        return None
    head, separator, remainder = pointer.partition("[")
    if not separator:
        return None
    index_text, close, tail = remainder.partition("]")
    if not close or not index_text.isdigit():
        return None
    if head == "balance" and tail.startswith(".") and tail[1:] in (_BALANCE_FIELDS | _ATTRIBUTION_FIELDS):
        return int(index_text)
    if not tail and head in _BALANCE_FIELDS:
        return int(index_text)
    return None


def augment_financial_statements(run, report):
    """Attach adapter facts/diagnostics after the existing V3 build."""
    additions = [(rec, *extract_financial_statements(rec)) for rec in run.records
                 if getattr(rec, "type", None) == "openbb_fundamentals"]
    source_ids = {rec.id for rec, _, _ in additions}
    existing = run.fact_store.to_dict()
    owned = set(ALIASES)
    removed = {fid for fid, f in existing.items() if f["source_id"] in source_ids and f["concept"] in owned}
    store = FactStore()
    for fid, raw in existing.items():
        if fid not in removed:
            store.add(Fact(**raw))
    run.fact_store = store
    diagnostics = []
    for rec, facts, diagnostic in additions:
        for fact in facts:
            run.fact_store.add(fact)
        if not hasattr(rec, "meta"):
            rec.meta = {}
        rec.meta["financial_statement_extraction"] = diagnostic
        diagnostics.append(deepcopy(diagnostic))
        report["facts"] += len(facts) - len(removed)
        report["limitations"].extend(f["reason"] for f in diagnostic["findings"])
    report["financial_statement_coverage"] = diagnostics
    report["financial_statement_findings"] = financial_statement_findings(run)
    return report


def financial_statement_findings(run):
    """Check explicit attribution coverage and accounting identities."""
    out = []
    for rec in getattr(run, "records", []):
        diag = getattr(rec, "meta", {}).get("financial_statement_extraction", {})
        out.extend(deepcopy(diag.get("findings", [])))
    facts = run.fact_store.to_dict()
    by_key = {}
    for fid, f in facts.items():
        if f.get("concept") in {"Assets", "Liabilities", *ALIASES.keys()}:
            entity = f.get('entity') or _source_entity(run, f)
            basis = f.get('basis')
            if isinstance(basis, str):
                basis = basis.strip().casefold()
            key = (entity, f.get("period"), f.get("unit"), basis)
            by_key.setdefault(key, {}).setdefault(f["concept"], []).append((fid, f))
    for key, group in by_key.items():
        if not key[0] or not key[2] or not key[3] or key[3] in {'unknown', 'parent', 'consolidated', 'single_company', 'segment'}:
            out.append(dict(status="WARN", check_id="balance_sheet_identity_coverage",
                entity=key[0], period=key[1], reason="identity not evaluated: entity, unit, or accounting basis is unknown"))
            continue
        # Duplicate sources with the same typed identity are ambiguous until
        # the caller chooses a source; never silently pick one by ordering.
        duplicates = [concept for concept, values in group.items() if len(values) > 1]
        if duplicates:
            out.append(dict(status="WARN", check_id="balance_sheet_identity_coverage",
                entity=key[0], period=key[1], reason="identity not evaluated: multiple sources provide the same typed component",
                concepts=duplicates))
            continue
        assets = group.get("Assets", [None])[0]
        liabilities = group.get("Liabilities", [None])[0]
        parent = group.get("ParentEquity", [None])[0]
        consolidated = group.get("ConsolidatedEquity", [None])[0]
        nci = group.get("NonRedeemableNCI", [None])[0]
        redeemable = group.get("RedeemableNCI", [None])[0]
        if not redeemable:
            out.append(dict(status='WARN', check_id='balance_sheet_identity_coverage',
                entity=key[0], period=key[1], reason='identity not evaluated: redeemable NCI is not provided; missing is not zero'))
            continue
        inclusion = (_coverage(run, redeemable[1]).get('redeemable_nci_inclusion') if redeemable else None)
        if redeemable and redeemable[1]['value'] == 0:
            inclusion = inclusion or 'separate'  # zero contributes equally under each classification
        if inclusion not in {'separate', 'liabilities', 'consolidated_equity'}:
            out.append(dict(status='WARN', check_id='redeemable_nci_inclusion_unknown',
                entity=key[0], period=key[1],
                reason='identity not evaluated: declare whether redeemable NCI is separate or included in liabilities/consolidated equity'))
            continue
        if consolidated and parent and nci:
            equity_components = [parent, nci] + ([redeemable] if inclusion == 'consolidated_equity' else [])
            out.append(_identity_result(run, key, consolidated, equity_components,
                'ConsolidatedEquity = ParentEquity + NonRedeemableNCI' +
                (' + RedeemableNCI' if inclusion == 'consolidated_equity' else ''),
                'equity_attribution_identity'))
        if not assets or not liabilities:
            out.append(dict(status='WARN', check_id='balance_sheet_identity_coverage',
                entity=key[0], period=key[1], basis=key[3],
                available_components=sorted(group),
                reason='identity not evaluated: Assets and Liabilities are not both available with matching entity, unit and basis'))
            continue
        if consolidated and redeemable:
            components = [liabilities, consolidated] + ([redeemable] if inclusion == 'separate' else [])
            equation = 'Assets = Liabilities + ConsolidatedEquity' + (' + RedeemableNCI' if inclusion == 'separate' else '')
        elif parent and nci and redeemable:
            components = [liabilities, parent, nci] + ([redeemable] if inclusion != 'liabilities' else [])
            equation = 'Assets = Liabilities + ParentEquity + NonRedeemableNCI' + (' + RedeemableNCI' if inclusion != 'liabilities' else '')
        else:
            peer_equity = any(other_key[0] == key[0] and other_key[1] == key[1]
                and any(concept in other_group for concept in ALIASES)
                for other_key, other_group in by_key.items() if other_key != key)
            out.append(dict(status="WARN", check_id="balance_sheet_identity_coverage",
                entity=key[0], period=key[1], reason=(
                    "identity not evaluated: equity candidates have a different unit or accounting basis"
                    if peer_equity else
                    "identity not evaluated: explicitly attributed equity components are incomplete")))
            continue
        out.append(_identity_result(run, key, assets, components, equation, 'balance_sheet_identity'))
    return out


def _source_entity(run, fact):
    """Fill only a missing entity from its own typed source, never alias names.

    Legacy builders omit entity. A declaration on that exact source/row also
    governs its legacy components; an explicit fact entity is never replaced.
    """
    for rec in getattr(run, 'records', []):
        if getattr(rec, 'id', None) != fact.get('source_id'):
            continue
        payload = getattr(rec, 'payload', {})
        metadata = payload.get('financial_statement_metadata', payload.get('financial_metadata', {}))
        metadata = metadata if isinstance(metadata, dict) else {}
        metadata = {**metadata, **getattr(rec, 'meta', {}).get('financial_statement_declarations', {})}
        index = _source_row_index(fact.get('source_pointer', ''))
        rows = payload.get('balance', [])
        row = rows[index] if isinstance(rows, list) and index is not None and index < len(rows) else {}
        if isinstance(row, dict) and row.get('fiscal_year') == fact.get('fiscal_year'):
            entity = row.get('entity') or metadata.get('entity')
        else:
            entity = metadata.get('entity')
        return entity if isinstance(entity, str) and entity.strip() else getattr(rec, 'subject_ticker', '')
    return ''


def _coverage(run, fact):
    row_index = _source_row_index(fact.get('source_pointer', ''))
    if row_index is None:
        return {}
    for rec in getattr(run, 'records', []):
        if getattr(rec, 'id', None) != fact.get('source_id'):
            continue
        for row in getattr(rec, 'meta', {}).get('financial_statement_extraction', {}).get('coverage', []):
            if row.get('section') == 'balance' and row.get('period') == fact.get('period') and row.get('row') == row_index:
                return row
    return {}


def _identity_result(run, key, lhs, components, equation, check_id):
    # Each independently rounded operand contributes half its declared increment.
    steps = [_coverage(run, item[1]).get('precision') for item in [lhs] + components]
    tolerance = sum((Decimal(str(p)) / 2 for p in steps if p is not None), Decimal(0))
    try:
        value = number(lhs[1]['value'])
        rhs = sum((number(item[1]['value']) for item in components), Decimal(0))
    except ValueError:
        return dict(status='FAIL', check_id=check_id, entity=key[0], period=key[1],
            refs=[lhs[0]] + [item[0] for item in components], reason='financial identity requires finite JSON numbers')
    delta = value - rhs
    balanced = abs(delta) <= tolerance
    return dict(status='PASS' if balanced else 'FAIL', check_id=check_id, entity=key[0], period=key[1],
        equation=equation, values=dict(lhs=lhs[1]['value'], components=[item[1]['value'] for item in components]),
        delta=float(delta), tolerance=float(tolerance), refs=[lhs[0]] + [item[0] for item in components],
        reason='identity balances within declared source precision' if balanced else 'complete financial statement components do not balance')
