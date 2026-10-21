"""V4 source-driven financial fact normalization.

This adapter keeps the existing V3 field map as its vocabulary while
preserving dimensions and period metadata declared by the source. Unknown
period coverage is kept unknown; no currency conversion or debt-zero
assumptions are introduced here.
"""
from copy import deepcopy
from collections import Counter
import hashlib
import math

from v3.builders import _INCOME_MAP, _BALANCE_MAP
from v3.facts import Fact, FactStore


_MAPS = {"income": _INCOME_MAP, "balance": _BALANCE_MAP}
_CURRENT_PORTION_FIELD = "current_portion_of_long_term_debt"
_DEBT_COMPONENT_FIELDS = ("short_term_debt", _CURRENT_PORTION_FIELD, "long_term_debt")
_PERIOD_TYPES = {"instant", "duration", "rolling", "unknown"}
_PERIOD_COVERAGE = {"annual", "single_period", "year_to_date", "rolling",
                    "point_in_time", "unknown"}
_KNOWN_SCALE_NAMES = {"thousand", "thousands", "million", "millions", "billion", "billions"}


def statement_metadata(record):
    payload = record.payload if isinstance(record.payload, dict) else {}
    primary = payload.get("financial_statement_metadata")
    alternate = payload.get("financial_metadata")
    metadata = primary if isinstance(primary, dict) else alternate
    metadata = deepcopy(metadata) if isinstance(metadata, dict) else {}
    declarations = getattr(record, "meta", {}).get("financial_statement_declarations", {})
    if isinstance(declarations, dict):
        metadata.update(declarations)
    return metadata


def _first(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def source_unit(row, metadata, default):
    explicit = _first(row, ("unit", "currency"))
    if explicit is None:
        explicit = _first(metadata, ("unit", "currency"))
    scale = _first(row, ("scale", "unit_scale"))
    if scale is None:
        scale = _first(metadata, ("scale", "unit_scale"))
    per_share = isinstance(default, str) and default.endswith("_PER_SHARE")
    if explicit is None:
        explicit = default.removesuffix("_PER_SHARE") if per_share else default
    if per_share and isinstance(explicit, str):
        currency = explicit.strip()
        if (len(currency) == 3 and currency.isascii() and currency.isalpha()
                and currency.isupper()):
            return f"{currency}_PER_SHARE"
        if scale:
            # A document-wide monetary scale cannot be applied to an EPS fact.
            return currency
        return currency
    if scale and isinstance(explicit, str):
        pieces = explicit.split()
        if len(pieces) == 1 and pieces[0].isalpha() and pieces[0].isupper():
            explicit = f"{explicit} {str(scale).strip().casefold()}"
    return explicit if isinstance(explicit, str) and explicit.strip() else default


def _dates(row, metadata, section):
    start = _first(row, ("start", "start_date", "period_start"))
    if start is None:
        start = _first(metadata, ("start", "start_date", "period_start"))
    end = _first(row, ("end", "end_date", "period_end"))
    if end is None:
        end = _first(metadata, ("end", "end_date", "period_end"))
    effective = _first(row, ("effective_date", "observation_date"))
    if effective is None:
        effective = _first(metadata, ("effective_date", "observation_date"))
    if effective is None and section == "balance":
        effective = end
    published = _first(row, ("published_or_filing_time", "filing_date",
                             "published_date", "available_date", "accepted_at"))
    if published is None:
        published = _first(metadata, ("published_or_filing_time", "filing_date",
                                      "published_date", "available_date", "accepted_at"))
    version = _first(row, ("accession_or_version", "accession_number", "accession", "version"))
    if version is None:
        version = _first(metadata, ("accession_or_version", "accession_number", "accession", "version"))
    return start, end, effective, published, version


def period_dimensions(row, section, metadata):
    year = row.get("fiscal_year")
    fiscal_period = row.get("fiscal_period", "FY")
    if type(year) is not int or not isinstance(fiscal_period, str) or not fiscal_period.strip():
        raise ValueError("row requires an integer fiscal_year and nonempty fiscal_period")
    fiscal_period = fiscal_period.strip().upper()
    expected = f"FY{year}" if fiscal_period == "FY" else f"FY{year}-{fiscal_period}"
    from .precision import period as normalize_period
    expected_normalized = normalize_period(expected)
    if expected_normalized is None:
        raise ValueError(f"fiscal_period {fiscal_period!r} is not a recognized typed period")
    declared = _first(row, ("period",))
    if declared is not None:
        normalized = normalize_period(declared)
        if normalized is None:
            raise ValueError(f"period metadata {declared!r} is not a recognized typed period")
        if normalized != expected_normalized:
            raise ValueError(f"period metadata {declared!r} conflicts with {expected!r}")
    period_type = _first(row, ("period_type",)) or _first(metadata, ("period_type",))
    if period_type is None:
        period_type = "instant" if section == "balance" else "duration"
    period_type = str(period_type).strip().casefold()
    if period_type not in _PERIOD_TYPES:
        period_type = "unknown"
    coverage = _first(row, ("period_coverage",)) or _first(metadata, ("period_coverage",))
    if coverage is None:
        coverage = "point_in_time" if section == "balance" and fiscal_period == "FY" else (
            "annual" if section != "balance" and fiscal_period == "FY" else "unknown")
    coverage = str(coverage).strip().casefold()
    if coverage not in _PERIOD_COVERAGE:
        coverage = "unknown"
    start, end, effective, published, version = _dates(row, metadata, section)
    return dict(period=expected, fiscal_year=year, fiscal_period=fiscal_period,
                period_type=period_type, period_coverage=coverage, start=start, end=end,
                effective_date=effective or "", published_or_filing_time=published or "",
                accession_or_version=version or "")


def _fact_id(concept, period, source_id, *, annual):
    if annual:
        return f"fact.{concept}.{period}"
    suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:10]
    return f"fact.{concept}.{period}.{suffix}"


def financial_fact(record, row, metadata, section, row_index, field, concept, default_unit, value,
                   *, period_info=None, source_pointer=None):
    dims = period_info or period_dimensions(row, section, metadata)
    basis = _first(row, ("basis",)) or _first(metadata, ("basis",)) or "unknown"
    scope = _first(row, ("scope", "statement_scope")) or _first(metadata, ("scope", "statement_scope")) or "unknown"
    entity = _first(row, ("entity",)) or _first(metadata, ("entity",)) or getattr(record, "subject_ticker", "") or ""
    share_basis = _first(row, ("share_basis",)) or _first(metadata, ("share_basis",)) or "unknown"
    if concept == "BasicEPS" and share_basis == "unknown":
        share_basis = "basic"
    elif concept == "DilutedEPS" and share_basis == "unknown":
        share_basis = "diluted"
    annual = dims["fiscal_period"] == "FY"
    fid = _fact_id(concept, dims["period"], record.id, annual=annual)
    return Fact(fid, value, source_unit(row, metadata, default_unit), dims["period"],
        start=dims["start"], end=dims["end"], fiscal_year=dims["fiscal_year"],
        fiscal_period=dims["fiscal_period"], concept=concept, basis=str(basis),
        scope=str(scope), share_basis=str(share_basis), source_id=record.id,
        source_pointer=source_pointer or f"{section}[{row_index}].{field}",
        accession_or_version=dims["accession_or_version"], source_hash=record.source_hash,
        effective_date=str(dims["effective_date"] or ""), entity=str(entity),
        period_type=dims["period_type"], period_coverage=dims["period_coverage"],
        published_or_filing_time=str(dims["published_or_filing_time"] or ""))


def _finite_source_value(value):
    return type(value) in (int, float) and math.isfinite(value)


def extract_financial_facts(record):
    """Build canonical mapped statement facts for all declared periods."""
    payload = record.payload if isinstance(record.payload, dict) else {}
    metadata = statement_metadata(record)
    facts, findings, limitations, coverage = [], [], [], []
    for section, field_map in _MAPS.items():
        rows = payload.get(section, [])
        if not isinstance(rows, list):
            findings.append(dict(status="FAIL", check_id="financial_source_shape",
                source_id=record.id, section=section, reason="statement section must be a list"))
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                findings.append(dict(status="FAIL", check_id="financial_source_shape",
                    source_id=record.id, section=section, row=index,
                    reason="statement row must be an object"))
                continue
            try:
                dims = period_dimensions(row, section, metadata)
            except (ValueError, TypeError) as exc:
                findings.append(dict(status="WARN", check_id="financial_period_unknown",
                    source_id=record.id, section=section, row=index, reason=str(exc)))
                continue
            produced = []
            fact_start = len(facts)
            for field, concept, default_unit in field_map:
                if row.get(field) is None:
                    continue
                value = row[field]
                if not _finite_source_value(value):
                    findings.append(dict(status="FAIL", check_id="financial_value_invalid",
                        source_id=record.id, section=section, row=index, field=field,
                        reason="mapped statement value must be a finite number"))
                    continue
                facts.append(financial_fact(record, row, metadata, section, index, field, concept,
                                            default_unit, value, period_info=dims))
                produced.append(concept)
            if section == "balance":
                facts.extend(_debt_facts(record, row, metadata, index, dims, produced,
                                         findings, limitations))
            declared_scale = _first(row, ("scale", "unit_scale"))
            if declared_scale is None:
                declared_scale = _first(metadata, ("scale", "unit_scale"))
            if (declared_scale is not None
                    and str(declared_scale).strip().casefold() not in _KNOWN_SCALE_NAMES):
                limitations.append(f"{record.id}: {section}[{index}] scale {declared_scale!r} is preserved as declared; "
                    "no numeric conversion was applied")
            coverage.append(dict(section=section, row=index, period=dims["period"],
                fiscal_year=dims["fiscal_year"], fiscal_period=dims["fiscal_period"],
                period_type=dims["period_type"], period_coverage=dims["period_coverage"],
                start=dims["start"], end=dims["end"], published_or_filing_time=dims["published_or_filing_time"],
                accession_or_version=dims["accession_or_version"], produced_concepts=produced,
                produced_facts=[dict(fact_id=fact.fact_id, concept=fact.concept, unit=fact.unit,
                    basis=fact.basis, scope=fact.scope, entity=fact.entity,
                    share_basis=fact.share_basis) for fact in facts[fact_start:]]))
    return facts, dict(section="financial_data", source_id=record.id, source_hash=record.source_hash,
                       coverage=coverage, findings=findings, limitations=limitations)


def prepare_legacy_builder_view(builder_records):
    """Keep duplicate annual rows from colliding in the V3 compatibility builder.

    The V4 adapters still consume all original records and preserve each version.
    This copy gives legacy calculations the first source row for a statement
    period, and reports every additional row instead of combining versions.
    """
    seen = set()
    limitations = []
    for record in builder_records:
        if getattr(record, "type", None) != "openbb_fundamentals":
            continue
        payload = record.payload if isinstance(record.payload, dict) else {}
        for section in _MAPS:
            rows = payload.get(section, [])
            if not isinstance(rows, list):
                continue
            retained = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    retained.append(row)
                    continue
                fiscal_period = str(row.get("fiscal_period", "FY")).strip().upper()
                year = row.get("fiscal_year")
                key = (section, year, fiscal_period)
                if fiscal_period == "FY" and type(year) is int and key in seen:
                    limitations.append(
                        f"{record.id}: {section}[{index}] repeats FY{year}; "
                        "the compatibility builder uses the first row and V4 preserves this row separately")
                    continue
                if fiscal_period == "FY" and type(year) is int:
                    seen.add(key)
                retained.append(row)
            payload[section] = retained
    return limitations


def disambiguate_fact_ids(additions):
    """Preserve source versions while retaining the first canonical fact ID.

    Each addition starts with a source record and facts; an optional final
    element is its diagnostic. When additions also carry calculations in the
    third position, their input references are updated with the new fact IDs.
    """
    counts = Counter(fact.fact_id for addition in additions for fact in addition[1])
    duplicate_ids = {fact_id for fact_id, count in counts.items() if count > 1}
    occurrence = Counter()
    for addition in additions:
        record, facts, diagnostic = addition[0], addition[1], addition[-1]
        ref_map = {}
        for fact in facts:
            old_id = fact.fact_id
            if old_id not in duplicate_ids:
                continue
            occurrence[old_id] += 1
            ref_map[old_id] = old_id
            if occurrence[old_id] == 1:
                diagnostic.setdefault("limitations", []).append(
                    f"{record.id}: {old_id} has multiple same-period source rows; "
                    "this first source retains the canonical ID for compatibility")
                continue
            discriminator = "|".join((record.id, fact.source_pointer,
                fact.accession_or_version, fact.source_hash, str(occurrence[old_id])))
            suffix = hashlib.sha256(discriminator.encode("utf-8")).hexdigest()[:10]
            fact.fact_id = f"{old_id}.{suffix}"
            ref_map[old_id] = fact.fact_id
            diagnostic.setdefault("limitations", []).append(
                f"{record.id}: {old_id} has multiple same-period source rows; "
                f"this version is stored as {fact.fact_id}")
        if len(addition) >= 4 and isinstance(addition[2], list):
            for calc in addition[2]:
                calc["input_refs"] = [ref_map.get(ref, ref) for ref in calc.get("input_refs", [])]
    return duplicate_ids


def _debt_facts(record, row, metadata, index, dims, produced, findings, limitations):
    facts = []
    components = {}
    for field in _DEBT_COMPONENT_FIELDS:
        value = row.get(field)
        if value is None:
            continue
        if not _finite_source_value(value) or value < 0:
            findings.append(dict(status="FAIL", check_id="debt_component_invalid",
                source_id=record.id, section="balance", row=index, field=field,
                reason="debt components must be finite nonnegative numeric values"))
            continue
        components[field] = value
    if _CURRENT_PORTION_FIELD in components:
        facts.append(financial_fact(record, row, metadata, "balance", index, _CURRENT_PORTION_FIELD,
            "CurrentPortionOfLongTermDebt", "USD", components[_CURRENT_PORTION_FIELD],
            period_info=dims))
        produced.append("CurrentPortionOfLongTermDebt")

    total_fields = [field for field in ("total_debt", "reported_total_debt")
                    if row.get(field) is not None]
    explicit_total = None
    if total_fields:
        if any(not _finite_source_value(row[field]) or row[field] < 0 for field in total_fields):
            findings.append(dict(status="FAIL", check_id="debt_total_invalid", source_id=record.id,
                section="balance", row=index, reason="reported total debt must be finite and nonnegative"))
        elif len({row[field] for field in total_fields}) > 1:
            findings.append(dict(status="FAIL", check_id="debt_total_conflict", source_id=record.id,
                section="balance", row=index, fields=total_fields,
                reason="reported total debt fields conflict"))
        else:
            explicit_total = row[total_fields[0]]
            facts.append(financial_fact(record, row, metadata, "balance", index, total_fields[0],
                "TotalDebt", "USD", explicit_total, period_info=dims))
            produced.append("TotalDebt")
    if explicit_total is None and not total_fields:
        complete = row.get("debt_components_complete") is True
        overlap_known = type(row.get("current_portion_in_short_term_debt")) is bool
        components_complete = all(field in components for field in _DEBT_COMPONENT_FIELDS)
        if complete and overlap_known and components_complete:
            included = row["current_portion_in_short_term_debt"]
            value = components["short_term_debt"] + components["long_term_debt"]
            if not included:
                value += components[_CURRENT_PORTION_FIELD]
            facts.append(financial_fact(record, row, metadata, "balance", index,
                "derived:complete_debt_components", "TotalDebt", "USD", value,
                period_info=dims,
                source_pointer=f"derived:balance[{index}].debt_components"))
            produced.append("TotalDebt")
        elif any(row.get(field) is not None for field in (*_DEBT_COMPONENT_FIELDS,
                "debt_components_complete", "current_portion_in_short_term_debt")):
            reasons = [f"{'missing' if row.get(field) is None else 'invalid'} component: {field}"
                       for field in _DEBT_COMPONENT_FIELDS if field not in components]
            if not complete:
                if row.get("debt_components_complete") is None:
                    reasons.append("missing declaration: debt_components_complete")
                elif row["debt_components_complete"] is False:
                    reasons.append("incomplete declaration: debt_components_complete=false")
                else:
                    reasons.append("invalid declaration: debt_components_complete (expected true)")
            if not overlap_known:
                reasons.append("missing declaration: current_portion_in_short_term_debt"
                    if row.get("current_portion_in_short_term_debt") is None else
                    "invalid declaration: current_portion_in_short_term_debt (expected boolean)")
            limitations.append(f"{record.id}: balance[{index}] TotalDebt unavailable: "
                               + "; ".join(reasons))
    return facts


def augment_financial_facts(run, report):
    records = [rec for rec in getattr(run, "records", [])
               if getattr(rec, "type", None) == "openbb_fundamentals"]
    if not records:
        return report
    additions = [(rec, *extract_financial_facts(rec)) for rec in records]
    disambiguate_fact_ids(additions)
    source_ids = {rec.id for rec, _, _ in additions}
    concepts = {concept for _, mapping in _MAPS.items() for _, concept, _ in mapping}
    concepts |= {"TotalDebt", "CurrentPortionOfLongTermDebt"}
    existing = run.fact_store.to_dict()
    removed = {fid for fid, fact in existing.items()
               if fact.get("source_id") in source_ids and fact.get("concept") in concepts}
    facts = FactStore()
    for fid, raw in existing.items():
        if fid not in removed:
            facts.add(Fact(**raw))
    run.fact_store = facts
    diagnostics = []
    for rec, new_facts, diagnostic in additions:
        for fact in new_facts:
            run.fact_store.add(fact)
        if not hasattr(rec, "meta"):
            rec.meta = {}
        rec.meta["financial_data_extraction"] = deepcopy(diagnostic)
        diagnostics.append(deepcopy(diagnostic))
        report["facts"] += len(new_facts) - sum(1 for fid in removed
            if existing[fid].get("source_id") == rec.id)
        report["limitations"].extend(diagnostic["limitations"])
    report["financial_data_coverage"] = diagnostics
    return report


def row_for_fact(record, fact):
    """Resolve a canonical financial pointer to its structured source row."""
    pointer = fact.get("source_pointer", "")
    if not isinstance(pointer, str) or not pointer:
        return None, None, None
    derived = pointer.startswith("derived:")
    pointer = pointer.removeprefix("derived:")
    head, sep, rest = pointer.partition("[")
    if not sep:
        return None, None, None
    idx_text, close, tail = rest.partition("]")
    if not close or not idx_text.isdigit():
        return None, None, None
    section = head if head in (*_MAPS, "cash_flow") else None
    field = tail.removeprefix(".") if tail.startswith(".") else head
    if section is None and not derived:
        concept = fact.get("concept")
        section = next((s for s, mapping in _MAPS.items()
                        if any(c == concept for _, c, _ in mapping)), None)
    if section is None:
        return None, None, None
    rows = record.payload.get(section, []) if isinstance(record.payload, dict) else []
    index = int(idx_text)
    if not isinstance(rows, list) or index >= len(rows) or not isinstance(rows[index], dict):
        return None, section, index
    return rows[index], section, index


def _row_index(pointer):
    if not isinstance(pointer, str) or "[" not in pointer:
        return None
    text = pointer.split("[", 1)[1].split("]", 1)[0]
    return int(text) if text.isdigit() else None
