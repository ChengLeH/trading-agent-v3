"""V4 market-data normalization before the shared OHLCV builder."""
from copy import deepcopy
from datetime import date
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


def normalize_market_record(record, manifest):
    payload = deepcopy(record.payload)
    bars = payload.get("bars") if isinstance(payload, dict) else None
    if not isinstance(bars, list) or not bars:
        return payload, dict(section="market_data", source_id=record.id,
            findings=[], limitations=[f"{record.id}: no bars in market source"],
            selected_price_field="close", price_basis="raw_close")
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
    manifest_sources = manifest.get("sources", []) if isinstance(manifest, dict) else []
    declared = next((row for row in manifest_sources if isinstance(row, dict) and row.get("id") == record.id), {})
    declared_basis = declared.get("price_basis")
    basis_source = "manifest" if declared_basis is not None else "source_metadata"
    if declared_basis is None and isinstance(record.meta, dict):
        declared_basis = record.meta.get("price_basis")
    if isinstance(declared_basis, str) and declared_basis.strip():
        normalized_declared_basis = declared_basis.strip()
        price_basis = ("raw_close" if normalized_declared_basis.casefold()
                       in {"raw", "raw_close", "raw_ohlcv"}
                       else normalized_declared_basis)
    else:
        # With no explicit adjustment metadata, preserve the existing V4
        # assumption and state it as a default rather than as source fact.
        price_basis = "raw_close"
        basis_source = "default_assumption"
    if price_basis != "raw_close":
        limitations.append(f"{record.id}: declared price basis {price_basis!r} is preserved; "
            "V4 uses the source close field without converting it or substituting adj_close")
    payload["bars"] = normalized
    return payload, dict(section="market_data", source_id=record.id,
        input_rows=len(bars), normalized_rows=len(normalized), findings=findings,
        limitations=limitations, selected_price_field="close", price_basis=price_basis,
        price_basis_source=basis_source,
        adjusted_close_present=adjusted_present, declared_price_basis=declared_basis)


def annotate_market_facts(run, diagnostics):
    by_source = {row["source_id"]: row for row in diagnostics}
    for fact in run.fact_store._facts.values():
        diagnostic = by_source.get(fact.source_id)
        if diagnostic is None:
            continue
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
    basis_by_source = {source_id: diagnostic["price_basis"]
                       for source_id, diagnostic in by_source.items()}
    for calc in run.calc_store._calcs.values():
        if calc.get("calc_type") == "return_cc":
            refs = calc.get("input_refs", [])
            price_bases = {basis_by_source[run.fact_store.get(ref).source_id]
                           for ref in refs
                           if ref in run.fact_store._facts
                           and run.fact_store.get(ref).source_id in basis_by_source}
            if len(price_bases) == 1:
                calc.setdefault("meta", {})["price_basis"] = price_bases.pop()
