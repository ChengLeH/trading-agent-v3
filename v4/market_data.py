"""V4 market-data normalization before the shared OHLCV builder."""
from copy import deepcopy
from datetime import date, datetime
import math


_PRICE_FACTS = {"Close", "Open", "High", "Low", "SMA20", "SMA50", "SMA200",
                "ATR14", "High52w", "Low52w"}


def _parse_date(value, source_id, index):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source_id}: market bar {index} needs an explicit date")
    text = value.strip()
    try:
        # OpenBB daily data may include a midnight timestamp; its calendar date
        # remains the stable daily key used by the existing V4 facts.
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"{source_id}: market bar {index} has invalid date") from None


_PRICE_FIELDS = ("open", "high", "low", "close")


def _optional_metadata(record, payload, declared, key):
    if key in declared:
        return True, deepcopy(declared[key]), "manifest"
    metadata = getattr(record, "meta", None)
    if isinstance(metadata, dict) and key in metadata:
        return True, deepcopy(metadata[key]), "source_metadata"
    if isinstance(payload, dict) and key in payload:
        return True, deepcopy(payload[key]), "payload"
    return False, None, None


def _normalize_basis_label(value):
    if not isinstance(value, str) or not value.strip():
        return None
    label = value.strip()
    if label.casefold() in {"raw", "raw_close", "raw_ohlcv"}:
        return "raw_close"
    return label


def _annotation_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _typed_warning(diagnostic, check_id, source_id, reason, **details):
    row = dict(status="WARN", check_id=check_id, source_id=source_id, reason=reason)
    row.update(details)
    diagnostic.setdefault("findings", []).append(row)


def _add_market_annotations(diagnostic, record, manifest, payload, declared, bars):
    has_field_map, raw_field_map, field_map_source = _optional_metadata(
        record, payload, declared, "price_basis_by_field")
    if has_field_map:
        normalized_field_map = {}
        if isinstance(raw_field_map, dict):
            for field in _PRICE_FIELDS:
                normalized_field_map[field] = (
                    _normalize_basis_label(raw_field_map.get(field)) or "unknown")
        else:
            normalized_field_map = {field: "unknown" for field in _PRICE_FIELDS}
        labels = list(normalized_field_map.values())
        consistent = (all(label != "unknown" for label in labels)
                      and len(set(labels)) == 1)
        diagnostic["price_basis_by_field"] = normalized_field_map
        diagnostic["price_basis_by_field_source"] = field_map_source
        diagnostic["ohlc_basis_consistency"] = (
            "declared_consistent" if consistent else "unverified")
        if not consistent:
            _typed_warning(
                diagnostic, "price_fields_basis_unverified", record.id,
                "OHLC field-basis labels are incomplete or differ. V4 does not infer "
                "equivalence or adjust prices; the existing LLM semantic verifier "
                "must review whether the fields support a comparable claim.")

    has_actions, raw_actions, actions_source = _optional_metadata(
        record, payload, declared, "corporate_actions")
    if not has_actions:
        return
    diagnostic["corporate_actions"] = raw_actions
    diagnostic["corporate_actions_source"] = actions_source
    diagnostic["corporate_actions_completeness"] = "unverified"
    if not isinstance(raw_actions, list):
        diagnostic["corporate_action_timing"] = []
        _typed_warning(
            diagnostic, "invalid_corporate_actions", record.id,
            "The declared corporate_actions value is not an array; it is preserved "
            "as raw metadata and no event timing is inferred.")
        return

    manifest_as_of = manifest.get("as_of") if isinstance(manifest, dict) else None
    as_of = _annotation_date(manifest_as_of)
    bar_dates = [
        parsed for bar in (bars or [])
        if isinstance(bar, dict)
        for parsed in [_annotation_date(bar.get("date"))]
        if parsed is not None
    ]
    window_start = min(bar_dates) if bar_dates else None
    window_end = max(bar_dates) if bar_dates else None
    timing = []
    for index, event in enumerate(raw_actions):
        is_object = isinstance(event, dict)
        raw_effective_date = event.get("effective_date") if is_object else None
        effective_date = _annotation_date(raw_effective_date)
        if effective_date is None:
            classification = "unknown"
        elif as_of is not None and effective_date > as_of:
            classification = "future_as_of"
        elif window_start is not None and window_end is not None:
            if effective_date < window_start:
                classification = "before_bar_window"
            elif effective_date > window_end:
                classification = "after_bar_window"
            else:
                classification = "within_bar_window"
        elif as_of is not None:
            classification = "on_or_before_as_of"
        else:
            classification = "unknown"

        annotation = {"index": index}
        if is_object and "action_id" in event:
            annotation["action_id"] = deepcopy(event["action_id"])
        if is_object and "effective_date" in event:
            annotation["effective_date"] = deepcopy(raw_effective_date)
        annotation["timing_classification"] = classification
        annotation["adjustment_status"] = "unverified"
        timing.append(annotation)
        if classification == "unknown":
            _typed_warning(
                diagnostic, "corporate_action_timing_unverified", record.id,
                "The event has no usable explicit effective_date, or available dates "
                "cannot place it relative to as_of or the normalized bar window; "
                "timing remains unknown.",
                event_index=index,
                action_id=deepcopy(event.get("action_id")) if is_object else None)
    diagnostic["corporate_action_timing"] = timing


def _field_for_price_fact(concept):
    if concept in {"Open", "High", "Low", "Close"}:
        return concept.casefold()
    if isinstance(concept, str) and concept.startswith("OHLCV_"):
        field = concept.rsplit("_", 1)[-1].casefold()
        return field if field in _PRICE_FIELDS else None
    if concept in {"SMA20", "SMA50", "SMA200", "ReturnW", "ReturnM"}:
        return "close"
    if concept == "High52w":
        return "high"
    if concept == "Low52w":
        return "low"
    if concept == "ATR14":
        return "atr_inputs"
    return None


def _fact_price_basis(label):
    if label == "unknown":
        return "unknown"
    return "raw_price" if label == "raw_close" else f"price:{label}"


def _return_price_basis(label):
    if label == "unknown":
        return "unknown"
    return ("raw_close_to_close" if label == "raw_close"
            else f"{label}_close_to_close")


def _close_basis_for_source(diagnostic):
    if "price_basis_by_field" in diagnostic:
        return diagnostic["price_basis_by_field"].get("close", "unknown")
    return diagnostic.get("price_basis", "unknown")


def normalize_market_record(record, manifest):
    payload = deepcopy(record.payload)
    bars = payload.get("bars") if isinstance(payload, dict) else None
    manifest_sources = manifest.get("sources", []) if isinstance(manifest, dict) else []
    declared = next((row for row in manifest_sources
                     if isinstance(row, dict) and row.get("id") == record.id), {})
    if not isinstance(bars, list) or not bars:
        diagnostic = dict(section="market_data", source_id=record.id,
            findings=[], limitations=[f"{record.id}: no bars in market source"],
            selected_price_field="close", price_basis="raw_close")
        _add_market_annotations(diagnostic, record, manifest, payload, declared, [])
        return payload, diagnostic
    by_date = {}
    findings, limitations = [], []
    for index, raw in enumerate(bars):
        if not isinstance(raw, dict):
            raise ValueError(f"{record.id}: market bar {index} must be an object")
        day = _parse_date(raw.get("date"), record.id, index)
        bar = deepcopy(raw)
        bar["date"] = day.isoformat()
        numeric = {}
        for field in ("open", "high", "low", "close"):
            value = bar.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{record.id}: market bar {index} has invalid {field}")
            numeric[field] = value
        if numeric["high"] < max(numeric["open"], numeric["close"]) or numeric["low"] > min(numeric["open"], numeric["close"]) or numeric["high"] < numeric["low"]:
            raise ValueError(f"{record.id}: market bar {index} has inconsistent OHLC bounds")
        volume = bar.get("volume")
        if volume is not None and (type(volume) not in (int, float) or not math.isfinite(volume) or volume < 0):
            raise ValueError(f"{record.id}: market bar {index} has invalid volume")
        if not bar.get("week"):
            iso = day.isocalendar()
            bar["week"] = f"{iso.year}-W{iso.week:02d}"
        if not bar.get("month"):
            bar["month"] = day.strftime("%Y-%m")
        previous = by_date.get(day)
        if previous is not None:
            if previous == bar:
                findings.append(dict(status="WARN", check_id="duplicate_market_bar",
                    source_id=record.id, date=day.isoformat(), reason="identical duplicate daily bar was collapsed"))
                continue
            raise ValueError(f"{record.id}: conflicting market bars share date {day.isoformat()}")
        by_date[day] = bar
    normalized = [by_date[key] for key in sorted(by_date)]
    adjusted_present = any(bar.get("adj_close") is not None for bar in normalized)
    if adjusted_present:
        limitations.append(f"{record.id}: adjusted-close values were present but not used; "
            "V4 used the raw OHLC fields consistently for close, return, range, and volatility facts")
    declared_basis = declared.get("price_basis")
    basis_source = "manifest" if declared_basis is not None else "source_metadata"
    if declared_basis is None and isinstance(record.meta, dict):
        declared_basis = record.meta.get("price_basis")
    price_basis = _normalize_basis_label(declared_basis)
    if price_basis is None:
        # With no explicit adjustment metadata, preserve the existing V4
        # assumption and state it as a default rather than as source fact.
        price_basis = "raw_close"
        basis_source = "default_assumption"
    if price_basis != "raw_close":
        limitations.append(f"{record.id}: declared price basis {price_basis!r} is preserved; "
            "V4 uses the source close field without converting it or substituting adj_close")
    payload["bars"] = normalized
    diagnostic = dict(section="market_data", source_id=record.id,
        input_rows=len(bars), normalized_rows=len(normalized), findings=findings,
        limitations=limitations, selected_price_field="close", price_basis=price_basis,
        price_basis_source=basis_source,
        adjusted_close_present=adjusted_present, declared_price_basis=declared_basis)
    _add_market_annotations(diagnostic, record, manifest, payload, declared, normalized)
    return payload, diagnostic


def remove_incomplete_52_week_values(run, diagnostics, records):
    """Never label a shorter supplied window as the builder's 252-bar range."""
    by_source = {rec.id: rec for rec in records if getattr(rec, 'type', None) == 'openbb_ohlcv'}
    removed = set()
    for diagnostic in diagnostics:
        rec = by_source.get(diagnostic['source_id'])
        high = run.fact_store.get('fact.High52w.latest')
        if rec is None or high is None or high.source_id != rec.id:
            continue
        # The shared builder can exclude an unfinished last bar. Count only
        # observations through the date actually used for its latest fact.
        bars = rec.payload.get('bars', [])
        used = sum(1 for bar in bars if bar['date'] <= high.effective_date)
        if used >= 252:
            continue
        reason = f'{rec.id}: 52-week high/low unavailable: only {used} of 252 required observations'
        diagnostic['limitations'].append(reason)
        diagnostic['findings'].append(dict(status='WARN', check_id='market_52_week_history_incomplete',
            source_id=rec.id, actual_observations=used, required_observations=252, reason=reason))
        for fid in ('fact.High52w.latest', 'fact.Low52w.latest'):
            if run.fact_store.get(fid) is not None:
                removed.add(fid)
                run.fact_store._facts.pop(fid)
    for cid, calc in list(run.calc_store._calcs.items()):
        if removed.intersection(calc.get('input_refs', [])):
            run.calc_store._calcs.pop(cid)


def annotate_market_facts(run, diagnostics):
    by_source = {row["source_id"]: row for row in diagnostics}
    for fact in run.fact_store._facts.values():
        diagnostic = by_source.get(fact.source_id)
        if diagnostic is None:
            continue
        if "price_basis_by_field" in diagnostic:
            field = _field_for_price_fact(fact.concept)
            if field == "atr_inputs":
                field_map = diagnostic["price_basis_by_field"]
                labels = [field_map.get(name, "unknown")
                          for name in ("high", "low", "close")]
                label = labels[0] if (
                    all(value != "unknown" for value in labels)
                    and len(set(labels)) == 1) else "unknown"
            elif field is not None:
                label = diagnostic["price_basis_by_field"].get(field, "unknown")
            else:
                label = None
            if label is not None:
                fact.basis = _fact_price_basis(label)
            if fact.concept in {"ReturnW", "ReturnM"}:
                close_basis = diagnostic["price_basis_by_field"].get("close", "unknown")
                fact.basis = _return_price_basis(close_basis)
                if close_basis != "unknown":
                    fact.source_pointer = (
                        f"{close_basis}:close_to_close:source close field")
        else:
            price_basis = diagnostic["price_basis"]
            if fact.concept in _PRICE_FACTS or fact.concept.startswith("OHLCV_"):
                fact.basis = ("raw_price" if price_basis == "raw_close"
                              else f"price:{price_basis}")
            if fact.concept in {"ReturnW", "ReturnM"}:
                fact.basis = ("raw_close_to_close" if price_basis == "raw_close"
                              else f"{price_basis}_close_to_close")
                fact.source_pointer = f"{price_basis}:close_to_close:source close field"

        # The shared V3 builder indexes bars after V4 has sorted them. Replace
        # negative positional references with a date key before the original
        # unsorted source payload is restored.
        pointer = fact.source_pointer
        if isinstance(pointer, str) and pointer.startswith("bars[") and fact.effective_date:
            close_bracket = pointer.find("].")
            if close_bracket > 0 and pointer[5:close_bracket].startswith("-"):
                field = pointer[close_bracket + 2:]
                fact.source_pointer = f"bars[{fact.effective_date}].{field}"

    for calc in run.calc_store._calcs.values():
        if calc.get("calc_type") != "return_cc":
            continue
        source_bases = []
        has_unknown_close_basis = False
        has_field_map = False
        for ref in calc.get("input_refs", []):
            fact = run.fact_store.get(ref)
            if fact is None or fact.source_id not in by_source:
                continue
            diagnostic = by_source[fact.source_id]
            has_field_map = has_field_map or "price_basis_by_field" in diagnostic
            basis = _close_basis_for_source(diagnostic)
            if basis == "unknown":
                has_unknown_close_basis = True
            else:
                source_bases.append(basis)
        if has_field_map:
            if has_unknown_close_basis or len(set(source_bases)) != 1:
                calc.setdefault("meta", {}).pop("price_basis", None)
            else:
                calc.setdefault("meta", {})["price_basis"] = source_bases[0]
        elif len(set(source_bases)) == 1:
            calc.setdefault("meta", {})["price_basis"] = source_bases[0]
