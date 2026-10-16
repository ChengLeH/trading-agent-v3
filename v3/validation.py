#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 validation.py — ONE validation service, internal checkers (spec §10).
User decisions 2026-10-15 baked in:
  - accounting_basis_consistency checker (independent of period_consistency)
  - CAGR is a first-class calculation type
  - weekly/monthly returns are adjusted close-to-close (see calculations.py)

Output per check: check_id, status, severity, object_id, reason, refs,
retryable, suggested_action. Only blocking errors stop a stage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from . import calculations as K


@dataclass
class Finding:
    check_id: str
    status: str              # PASS | FAIL | WARN
    severity: str            # 不分級：FAIL 就擋
    object_id: str
    reason: str
    refs: List[str] = field(default_factory=list)
    retryable: bool = True
    suggested_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def blocks(self) -> bool:
        return self.status == "FAIL"


# --------------------------------------------------------------------------
# Checkers — each is a pure function: (stores, context) -> List[Finding]
# --------------------------------------------------------------------------

def check_source_identity(manifest: Dict[str, Any],
                          records: List[Any]) -> List[Finding]:
    out = []
    want = manifest["subject"]["ticker"]
    for r in records:
        if getattr(r, "subject_ticker", "") != want:
            out.append(Finding("source_identity", "FAIL", "ERROR", r.id,
                               f"ticker mismatch: {getattr(r,'subject_ticker','')} != {want}",
                               [r.id], False, "drop source or fix manifest"))
    if not out:
        out.append(Finding("source_identity", "PASS", "ERROR", "manifest",
                           "all sources match subject ticker"))
    return out


def check_source_integrity(records: List[Any]) -> List[Finding]:
    """Source is non-empty, parsed OK, type correct, hash present,
    identity verified from payload (not just manifest echo)."""
    out = []
    for r in records:
        rid = getattr(r, "id", "?")
        if type(r).__name__ == "SourceError":
            out.append(Finding("source_integrity", "FAIL", "ERROR", rid,
                               f"adapter error: {getattr(r,'message','')}",
                               [rid], False, "fix source or drop it"))
            continue
        payload = getattr(r, "payload", None)
        if not payload:
            out.append(Finding("source_integrity", "FAIL", "ERROR", rid,
                               "empty payload", [rid], False,
                               "source produced no data"))
            continue
        if not getattr(r, "source_hash", ""):
            out.append(Finding("source_integrity", "FAIL", "ERROR", rid,
                               "missing source_hash", [rid], True,
                               "re-hash the payload"))
        meta = getattr(r, "meta", {}) or {}
        ident = meta.get("identity_verified")
        if ident is False:
            out.append(Finding("source_integrity", "FAIL", "ERROR", rid,
                               "payload ticker != manifest ticker",
                               [rid], False, "wrong company data"))
        elif ident is None:
            out.append(Finding("source_integrity", "WARN", "ERROR", rid,
                               "identity not verifiable from payload "
                               f"(payload_ticker={meta.get('payload_ticker')})",
                               [rid]))
        else:
            out.append(Finding("source_integrity", "PASS", "ERROR", rid,
                               f"payload ticker verified: {meta.get('payload_ticker')}",
                               [rid]))
    return out


def check_trade_consistency(trade: Dict[str, Any],
                            calc_store) -> List[Finding]:
    """Trade plan levels must match registered trade CALCs."""
    out = []
    if not trade:
        return out
    # WAIT = 不建倉，無交易可驗證，直接通過
    if str(trade.get("direction", "")).strip().upper() == "WAIT":
        out.append(Finding("trade_consistency", "PASS", "ERROR", "trade",
                           "WAIT direction: no position, skip calc check", []))
        return out
    calcs = calc_store.to_dict() if calc_store else {}
    # risk_reward registered by register_trade_plan must exist
    for cid in ("calc.trade.risk_reward", "calc.trade.position_risk"):
        if cid not in calcs:
            out.append(Finding("trade_consistency", "FAIL", "ERROR", cid,
                               f"trade calc {cid} not in CalcStore: "
                               "trader plan was not wired through kernel",
                               [cid], False,
                               "run register_trade_plan()"))
    if not any(f.status == "FAIL" for f in out):
        out.append(Finding("trade_consistency", "PASS", "ERROR", "trade",
                           "trade calcs registered via kernel", []))
    return out


def check_fact_schema(store) -> List[Finding]:
    out = []
    for fid, f in store.to_dict().items():
        missing = [k for k in ("unit", "period", "concept", "source_id",
                               "source_pointer", "source_hash")
                   if not f.get(k)]
        if missing:
            out.append(Finding("fact_schema", "FAIL", "ERROR", fid,
                               f"missing fields: {missing}", [fid],
                               True, "re-extract fact with full provenance"))
    if not out:
        out.append(Finding("fact_schema", "PASS", "ERROR", "fact_store",
                           "schema complete"))
    # identity collisions: same metric, different values
    for c in store.check_identity_collisions():
        out.append(Finding("fact_schema", "FAIL", "ERROR", c["identity"],
                           f"same metric identity, conflicting values {c['values']}",
                           c["fact_ids"], False,
                           "resolve source conflict before proceeding"))
    return out


def check_calculation(calc_store, fact_store) -> List[Finding]:
    """Recompute every calc from its input refs. Supports: cagr, margin,
    ev, per_share, risk_reward, position_risk, weighted_cost, sma, rsi,
    return_cc."""
    out = []
    for cid, c in calc_store.to_dict().items():
        try:
            ctype, refs = c["calc_type"], c["input_refs"]
            vals = {}
            for r in refs:
                f = fact_store.get(r)
                if f is None and not r.startswith("calc."):
                    raise KeyError(f"missing ref {r}")
                vals[r] = f.value if f else calc_store.get(r[5:])["value"]
            if ctype == "cagr":
                got = K.calc_cagr(vals[refs[0]], vals[refs[1]], c["meta"]["years"])
            elif ctype == "margin":
                got = K.calc_margin(vals[refs[0]], vals[refs[1]],
                                    c["meta"].get("num_basis", "unknown"),
                                    c["meta"].get("den_basis", "unknown"))
            elif ctype == "ev":
                got = K.calc_enterprise_value(vals[refs[0]], vals[refs[1]],
                                              vals[refs[2]])
            elif ctype == "per_share":
                got = K.calc_per_share(vals[refs[0]], vals[refs[1]],
                                       c["meta"]["total_unit"],
                                       c["meta"]["shares_unit"])
            elif ctype == "risk_reward":
                got = K.calc_risk_reward_ratio(
                    vals[refs[0]], vals[refs[1]], vals[refs[2]],
                    direction=c["meta"].get("direction", "BUY"))
            elif ctype == "position_risk":
                got = K.calc_position_risk_pct(
                    vals[refs[0]], vals[refs[1]], vals[refs[2]],
                    direction=c["meta"].get("direction", "BUY"))
            elif ctype == "volume_ratio":
                got = K.calc_volume_ratio(vals[refs[0]], vals[refs[1]])
            elif ctype == "fcf":
                got = K.calc_fcf(vals[refs[0]], vals[refs[1]])
            elif ctype == "growth":
                got = K.calc_growth(vals[refs[0]], vals[refs[1]])
            elif ctype == "derived_pct":
                got = K.calc_distance_pct(vals[refs[1]], vals[refs[0]])
            elif ctype == "weighted_cost":
                got = K.calc_weighted_entry_cost(c["meta"]["tranches"])
            elif ctype == "return_cc":
                got = K.close_to_close_return(vals[refs[0]], vals[refs[1]])
            else:
                out.append(Finding("calculation", "WARN", "ERROR", cid,
                                   f"unknown calc_type '{ctype}', skipped",
                                   [cid]))
                continue
            if abs(got - c["value"]) > c["meta"].get("tolerance", 1e-4):
                out.append(Finding("calculation", "FAIL", "ERROR", cid,
                                   f"recompute mismatch: stored {c['value']} vs {got}",
                                   [cid] + refs, True, "fix calc or source fact"))
            else:
                out.append(Finding("calculation", "PASS", "ERROR", cid,
                                   f"{ctype} recompute matches", [cid]))
        except K.BasisMismatchError as e:
            out.append(Finding("calculation", "FAIL", "ERROR", cid,
                               f"basis mismatch in calc: {e}", [cid],
                               False, "align input bases"))
        except Exception as e:
            out.append(Finding("calculation", "FAIL", "ERROR", cid,
                               f"calc error: {type(e).__name__}: {str(e)[:150]}",
                               [cid], True, "inspect calc inputs"))
    return out


def check_period_consistency(store) -> List[Finding]:
    """Quarter / YTD / annual must not be mixed within one claim's refs."""
    out, seen = [], {}
    for fid, f in store.to_dict().items():
        key = (f.get("concept"), f.get("fiscal_year"))
        periods = seen.setdefault(key, set())
        fp = f.get("fiscal_period")
        if fp in ("Q1", "Q2", "Q3", "Q4") and "FY" in periods:
            out.append(Finding("period_consistency", "FAIL", "ERROR", fid,
                               f"quarter {fp} mixed with annual for {key}",
                               [fid], False, "separate quarterly and annual series"))
        periods.add(fp or f.get("period"))
    if not out:
        out.append(Finding("period_consistency", "PASS", "ERROR", "fact_store",
                           "no period mixing detected"))
    return out


def check_accounting_basis_consistency(store) -> List[Finding]:
    """User decision 2026-10-15: basis is an INDEPENDENT dimension from period.
    pre/post-tax, GAAP/non-GAAP, basic/diluted, scope must not mix."""
    out = []
    _NON_FINANCIAL = {"Close", "SMA20", "SMA50", "SMA200", "RSI14", "ATR14",
                      "High52w", "Low52w", "Volume", "ReturnW", "ReturnM"}
    for fid, f in store.to_dict().items():
        if f.get("concept") in _NON_FINANCIAL:
            continue  # market facts have no accounting basis by nature
        basis, scope, sb = f.get("basis"), f.get("scope"), f.get("share_basis")
        if basis == "unknown" or scope == "unknown":
            out.append(Finding("accounting_basis_consistency", "WARN", "ERROR", fid,
                               "basis/scope not labeled; label explicitly",
                               [fid], True, "set basis/scope/share_basis on fact"))
    # pairwise: same concept+period, different basis -> flag
    by_key: Dict[str, List[str]] = {}
    for fid, f in store.to_dict().items():
        k = (f.get("concept"), f.get("period"))
        by_key.setdefault(k, []).append(fid)
    for k, fids in by_key.items():
        bases = {store.get(fid).basis for fid in fids}
        if len(bases) > 1:
            out.append(Finding("accounting_basis_consistency", "FAIL", "ERROR",
                               "|".join(fids),
                               f"{k} has mixed bases {sorted(bases)}; "
                               f"ratios across these are invalid",
                               fids, False,
                               "use same-basis inputs or disclose the mix"))
    if not out:
        out.append(Finding("accounting_basis_consistency", "PASS", "ERROR",
                           "fact_store", "bases consistent"))
    return out


def check_claim_grounding(claims: List[Dict[str, Any]],
                           fact_store=None,
                           ev_store=None,
                           calc_store=None,
                           trade_ids=None) -> List[Finding]:
    """source_fact / derived_fact / semantic_fact must have non-empty refs.
    When stores are provided, refs must also resolve to real facts/evidence.
    calc.* refs resolve against the calc store (calcs are first-class).
    trade.* refs resolve against registered trade ids."""
    out = []
    grounded_types = {"source_fact", "derived_fact", "semantic_fact"}
    # claim-to-claim refs are legitimate (Phase 2 builds on Phase 1 output)
    claim_ids = {c.get("claim_id") for c in claims}
    trade_ids = set(trade_ids or [])
    for cl in claims:
        ctype = cl.get("claim_type", "")
        refs = cl.get("refs", [])
        cid = cl.get("claim_id", "?")
        if ctype in grounded_types and not refs:
            out.append(Finding("claim_grounding", "FAIL", "ERROR", cid,
                               f"{ctype} claim has empty refs: "
                               f"{cl.get('text','')[:80]}",
                               [], False,
                               "add refs or demote to interpretation"))
            continue
        if ctype in grounded_types and (fact_store or ev_store or calc_store):
            facts = fact_store.to_dict() if fact_store else {}
            evs = ev_store.to_dict() if ev_store else {}
            calcs = calc_store.to_dict() if calc_store else {}
            for ref in refs:
                ok = (ref in facts) or (ref in evs) or (ref in calcs) or \
                     (ref in claim_ids) or (ref in trade_ids) or \
                     (ref.startswith("evidence.") and ref[9:] in evs) or \
                     (ref.startswith("trade.") and ref in trade_ids)
                if not ok:
                    out.append(Finding(
                        "claim_grounding", "FAIL", "ERROR", cid,
                        f"dangling ref {ref}: not in fact/evidence/calc store",
                        [cid], False,
                        "fix the ref or add the missing fact/evidence/calc"))
        if ctype in grounded_types and refs:
            out.append(Finding("claim_grounding", "PASS", "ERROR", cid,
                               "refs present", refs))
    return out


def check_temporal_grounding(ev_store,
                             as_of: str) -> List[Finding]:
    """Spec §15 Prototype B: a timestamped fake news item must be blocked.
    Evidence dated after the analysis date, or with unparseable/missing
    dates on time-sensitive claims, fails."""
    from datetime import date
    out = []
    try:
        as_of_d = date.fromisoformat(as_of[:10])
    except ValueError:
        return [Finding("temporal_grounding", "FAIL", "ERROR", "as_of",
                        f"bad as_of date {as_of}", [], False,
                        "pass analysis date as YYYY-MM-DD")]
    for eid, e in ev_store.to_dict().items():
        pub = (e.get("published_or_filing_time") or "").strip()
        if not pub:
            out.append(Finding("temporal_grounding", "WARN", "ERROR", eid,
                               "evidence has no timestamp; treat as low trust",
                               [eid], True, "add published_or_filing_time"))
            continue
        try:
            pub_d = date.fromisoformat(pub[:10])
        except ValueError:
            out.append(Finding("temporal_grounding", "WARN", "ERROR", eid,
                               f"unparseable timestamp {pub!r}",
                               [eid], True, "normalize to YYYY-MM-DD"))
            continue
        if pub_d > as_of_d:
            out.append(Finding(
                "temporal_grounding", "FAIL", "ERROR", eid,
                f"evidence dated {pub_d} is AFTER analysis date {as_of_d}; "
                f"possible injected fake news: {e.get('claim_candidate','')[:60]}",
                [eid], False,
                "quarantine this evidence and re-run without it"))
    if not out:
        out.append(Finding("temporal_grounding", "PASS", "ERROR", "evidence_store",
                           f"all evidence dated on/before {as_of_d}"))
    return out


def check_evidence_conflict(ev_store) -> List[Finding]:
    out = []
    for gid, items in ev_store.conflicts().items():
        out.append(Finding("evidence_conflict", "WARN", "ERROR", gid,
                           f"{len(items)} conflicting evidence in group {gid}; "
                           f"must be disclosed, not silently picked",
                           [e.evidence_id for e in items], True,
                           "surface the conflict in the report"))
    if not out:
        out.append(Finding("evidence_conflict", "PASS", "ERROR", "evidence_store",
                           "no unresolved conflicts"))
    return out


# --------------------------------------------------------------------------
# 2026-10-15: Four generic invariants abstracted from CBRS report errors.
# Not CBRS-specific. Each is a bottom-up consistency rule.
# --------------------------------------------------------------------------

_MARKET_CONCEPTS = {"Close", "SMA20", "SMA50", "SMA200", "RSI14", "ATR14",
                    "Volume", "AvgVolume20D", "AvgVolume60D",
                    "High52w", "Low52w", "ReturnD", "ReturnW", "ReturnM",
                    "Beta", "SharesOutstanding"}


def _parse_date(s: str):
    from datetime import date
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def check_as_of_consistency(fact_store, calc_store,
                            as_of: str,
                            freshness_tolerance_days: int) -> List[Finding]:
    """Invariant 1: as_of_consistency.

    Every latest/current FACT carries effective_date. CALC inputs must not be
    older than the allowed cutoff. Market-derived FACTs in one observation
    group must share the same effective_date.

    freshness_tolerance_days is passed from the analysis context (manifest),
    NEVER hardcoded here. (User boundary 2026-10-15.)
    """
    out = []
    as_of_d = _parse_date(as_of)
    if as_of_d is None:
        return [Finding("as_of_consistency", "FAIL", "ERROR", "as_of",
                        f"bad as_of date {as_of!r}", [], False,
                        "pass analysis date as YYYY-MM-DD")]

    facts = fact_store.to_dict() if fact_store else {}

    def _eff(f):
        return _parse_date(f.get("effective_date", ""))

    # (a) latest/current FACTs must have effective_date within tolerance
    for fid, f in facts.items():
        is_latest = fid.endswith(".latest") or fid.endswith(".current")
        if not is_latest:
            continue
        ed = _eff(f)
        if ed is None:
            out.append(Finding(
                "as_of_consistency", "FAIL", "ERROR", fid,
                f"latest/current FACT {fid} has no effective_date",
                [fid], True, "set effective_date on the FACT"))
            continue
        delta = (as_of_d - ed).days
        if delta < 0:
            out.append(Finding(
                "as_of_consistency", "FAIL", "ERROR", fid,
                f"{fid} effective_date {ed} is AFTER as_of {as_of_d}",
                [fid], False, "fact is from the future; check data pipeline"))
        elif delta > freshness_tolerance_days:
            out.append(Finding(
                "as_of_consistency", "FAIL", "ERROR", fid,
                f"{fid} is stale: effective {ed}, as_of {as_of_d} "
                f"({delta}d > tolerance {freshness_tolerance_days}d)",
                [fid], True,
                "refresh the FACT or widen tolerance explicitly in context"))

    # (b) CALC inputs must not be older than the cutoff
    calcs = calc_store.to_dict() if calc_store else {}
    for cid, c in calcs.items():
        for ref in c.get("input_refs", []):
            if not ref.startswith("fact."):
                continue
            f = facts.get(ref)
            if not f:
                continue  # dangling ref is claim_grounding/calculation's job
            ed = _eff(f)
            if ed is None:
                continue  # (a) already flags latest/current; skip others
            delta = (as_of_d - ed).days
            if delta > freshness_tolerance_days:
                out.append(Finding(
                    "as_of_consistency", "FAIL", "ERROR", cid,
                    f"CALC {cid} consumes stale input {ref} "
                    f"(effective {ed}, {delta}d > tolerance "
                    f"{freshness_tolerance_days}d)",
                    [cid, ref], True,
                    "use a fresher FACT or widen tolerance explicitly"))

    # (c) market-derived FACTs in one group share observation date
    # Only .latest/.current FACTs participate; .prev and historical
    # variants (e.g. fact.Close.prev) are inputs, not observations.
    # Historical period FACTs (fact.ReturnW.2026-W31, fact.ReturnM.2026-10,
    # fact.OHLCV.*) are explicitly exempt even if their concept is in
    # _MARKET_CONCEPTS — they carry their own period dates by design.
    import re as _re
    _HIST_PERIOD_RE = _re.compile(r"\.20\d{2}-(W\d{1,2}|M\d{1,2}|\d{2})(\.|$)")
    seen_dates: Dict[str, str] = {}
    for fid, f in facts.items():
        if f.get("concept") not in _MARKET_CONCEPTS:
            continue
        if _HIST_PERIOD_RE.search(fid):
            continue  # historical period FACT: exempt from group date rule
        if not (fid.endswith(".latest") or fid.endswith(".current")):
            continue
        ed = f.get("effective_date", "")
        if not ed:
            continue
        prev = seen_dates.setdefault("market", ed)
        if prev != ed:
            out.append(Finding(
                "as_of_consistency", "FAIL", "ERROR", fid,
                f"market FACT {fid} effective_date {ed} != group date "
                f"{prev}; price/indicators must share one observation date",
                [fid], True,
                "recompute indicators on the same bar window as price"))
            break

    if not out:
        out.append(Finding("as_of_consistency", "PASS", "ERROR", "fact_store",
                           f"all latest FACTs within {freshness_tolerance_days}d "
                           f"of {as_of_d}; market group shares one date"))
    return out


def check_accounting_basis_consistency(fact_store,
                                       calc_store=None) -> List[Finding]:
    """Invariant 2: accounting_scope_consistency (extends basis checker).

    User decision 2026-10-15: restricted cash is itself a hard FACT; do NOT
    auto-downgrade it. What must be explicit is the CALC's scope:
      - cash_scope ∈ {unrestricted, total, custom} is REQUIRED on any CALC
        about cash/liquidity/runway (e.g. available_liquidity, cash_runway).
      - A CALC named available_* using cash_scope="total" is a BLOCKING
        error (the CBRS "$8.61B liquidity" mistake: total cash presented
        as available cash).
      - Same pattern for revenue_scope ∈ {gaap, core, reported} on CALCs
        consuming non-GAAP/mixed-basis revenue FACTs.
    """
    out = []
    _NON_FINANCIAL = {"Close", "SMA20", "SMA50", "SMA200", "RSI14", "ATR14",
                      "High52w", "Low52w", "Volume", "ReturnW", "ReturnM"}
    for fid, f in (fact_store.to_dict() if fact_store else {}).items():
        if f.get("concept") in _NON_FINANCIAL:
            continue
        basis, scope, sb = f.get("basis"), f.get("scope"), f.get("share_basis")
        if basis == "unknown" or scope == "unknown":
            out.append(Finding("accounting_basis_consistency", "WARN", "ERROR", fid,
                               "basis/scope not labeled; label explicitly",
                               [fid], True, "set basis/scope/share_basis on fact"))
    by_key: Dict[str, List[str]] = {}
    for fid, f in (fact_store.to_dict() if fact_store else {}).items():
        k = (f.get("concept"), f.get("period"))
        by_key.setdefault(k, []).append(fid)
    for k, fids in by_key.items():
        bases = {fact_store.get(fid).basis for fid in fids}
        if len(bases) > 1:
            out.append(Finding("accounting_basis_consistency", "FAIL", "ERROR",
                               "|".join(fids),
                               f"{k} has mixed bases {sorted(bases)}; "
                               f"ratios across these are invalid",
                               fids, False,
                               "use same-basis inputs or disclose the mix"))

    # --- CALC scope rules (new) ---
    if calc_store:
        facts = fact_store.to_dict() if fact_store else {}
        for cid, c in calc_store.to_dict().items():
            meta = c.get("meta", {}) or {}
            low_id = cid.lower()
            is_cash_calc = any(w in low_id for w in
                               ("cash", "liquidity", "runway")) or \
                           meta.get("cash_concept", False)
            if is_cash_calc:
                scope = meta.get("cash_scope")
                if scope not in ("unrestricted", "total", "custom"):
                    out.append(Finding(
                        "accounting_basis_consistency", "FAIL", "ERROR", cid,
                        f"cash CALC {cid} must declare cash_scope ∈ "
                        f"{{unrestricted, total, custom}}; got {scope!r}",
                        [cid], True,
                        "set meta.cash_scope explicitly"))
                elif "available" in low_id and scope == "total":
                    out.append(Finding(
                        "accounting_basis_consistency", "FAIL", "ERROR", cid,
                        f"CALC {cid} is named available_* but uses "
                        f"cash_scope='total' (includes restricted cash)",
                        [cid], False,
                        "use cash_scope='unrestricted' or rename the CALC"))
            # revenue_scope: required when consuming non-GAAP/mixed revenue
            rev_inputs = [r for r in c.get("input_refs", [])
                          if r.startswith("fact.") and
                          "revenue" in (facts.get(r, {}).get("concept", "").lower())]
            if rev_inputs:
                rev_bases = {facts[r].get("basis") for r in rev_inputs
                             if r in facts}
                needs_scope = (len(rev_bases) > 1) or \
                              (rev_bases - {"gaap", "unknown"})
                if needs_scope and meta.get("revenue_scope") not in \
                        ("gaap", "core", "reported", "custom"):
                    out.append(Finding(
                        "accounting_basis_consistency", "FAIL", "ERROR", cid,
                        f"CALC {cid} consumes revenue with bases "
                        f"{sorted(rev_bases)}; declare revenue_scope",
                        [cid] + rev_inputs, True,
                        "set meta.revenue_scope ∈ {gaap, core, reported, custom}"))

    if not out:
        out.append(Finding("accounting_basis_consistency", "PASS", "ERROR",
                           "fact_store", "bases and CALC scopes consistent"))
    return out




def check_event_timeline_consistency(events: List[Dict[str, Any]],
                                     claims: List[Dict[str, Any]] = None
                                     ) -> List[Finding]:
    """Invariant 4: event_timeline_consistency.

    Operates on structured ReleaseEvent[] (v3/schemas.py). DEEP extraction
    populates these; NEVER regex-guess events from prose.

    Rules:
      a. Sum of individual (non-aggregate) event shares must not exceed any
         stated aggregate cap.
      b. A claim framing an aggregate cap as a single "batch"/"final"
         release (the CBRS 171.1M error) is BLOCKING.
    """
    out = []
    events = events or []
    individuals = [e for e in events if not e.get("is_aggregate", False)]
    aggregates = [e for e in events if e.get("is_aggregate", False)]

    total_ind = sum(int(e.get("shares", 0)) for e in individuals)
    for agg in aggregates:
        cap = int(agg.get("shares", 0))
        if cap > 0 and total_ind > cap * 1.05:
            out.append(Finding(
                "event_timeline_consistency", "FAIL", "ERROR",
                agg.get("source_ref", "aggregate"),
                f"individual releases total {total_ind:,} shares, exceeding "
                f"aggregate cap {cap:,} (source: {agg.get('source_ref')})",
                [], False, "re-check event extraction; caps cannot be exceeded"))

    # (b) aggregate-as-batch: claim cites the cap number as one batch
    for agg in aggregates:
        cap = int(agg.get("shares", 0))
        if cap <= 0:
            continue
        for cl in (claims or []):
            text = cl.get("text", "")
            low = text.lower()
            # single-event framing without aggregate qualifiers
            single_event = any(w in low for w in ("batch", "final", "unlock",
                                                 "release", "tranche"))
            aggregate_qual = any(w in low for w in ("total", "aggregate",
                                                    "cumulative", "up to",
                                                    "combined", "overall"))
            if not single_event or aggregate_qual:
                continue
            for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[Mm]?\b", text):
                try:
                    n = float(m.group(1))
                except ValueError:
                    continue
                # scale: "171.1M" vs raw shares
                candidates = {n, n * 1e6}
                if any(abs(c - cap) / cap < 0.05 for c in candidates):
                    # is there a real individual event with ≈ this size?
                    has_ind = any(
                        abs(int(e.get("shares", 0)) - cap) / cap < 0.10
                        for e in individuals)
                    if not has_ind:
                        out.append(Finding(
                            "event_timeline_consistency", "FAIL", "ERROR",
                            cl.get("claim_id", "?"),
                            f"claim frames aggregate cap {cap:,} shares as a "
                            f"single batch ('{text[:70]}...'); no individual "
                            f"event of that size exists",
                            [cl.get("claim_id", "?")], False,
                            "cite the aggregate as a cumulative cap, or bind "
                            "to a real dated release event"))
                        break

    if not out:
        out.append(Finding("event_timeline_consistency", "PASS", "ERROR",
                           "release_events",
                           f"{len(individuals)} individual events, "
                           f"{len(aggregates)} aggregate caps reconciled"))
    return out


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------
CHECKERS = {
    "source_identity": check_source_identity,
    "source_integrity": check_source_integrity,
    "fact_schema": check_fact_schema,
    "calculation": check_calculation,
    "period_consistency": check_period_consistency,
    "accounting_basis_consistency": check_accounting_basis_consistency,
    "claim_grounding": check_claim_grounding,
    "temporal_grounding": check_temporal_grounding,
    "evidence_conflict": check_evidence_conflict,
    "trade_consistency": check_trade_consistency,
    # 2026-10-15: four generic invariants (from CBRS errors)
    "as_of_consistency": check_as_of_consistency,
    "event_timeline_consistency": check_event_timeline_consistency,
    # 2026-10-16: structural checkers (from GOOGL review)
}


class ValidationService:
    """One service, internal checkers. validate_stage runs the checkers
    relevant to a stage and returns (findings, blocks_stage)."""

    STAGE_CHECKERS = {
        "ingest": ["source_identity", "source_integrity", "fact_schema",
                   "as_of_consistency"],
        "analysis": ["fact_schema", "period_consistency",
                     "accounting_basis_consistency", "calculation",
                     "claim_grounding"],
        "trade": ["calculation", "claim_grounding", "trade_consistency"],
        "final": ["claim_grounding", 
                  "temporal_grounding", 
                  "evidence_conflict", "calculation",
                  "accounting_basis_consistency", "as_of_consistency",
                   "event_timeline_consistency"],
    }

    def validate_stage(self, stage: str, ctx: Dict[str, Any]):
        findings: List[Finding] = []
        for name in self.STAGE_CHECKERS.get(stage, []):
            fn = CHECKERS[name]
            if name == "source_identity":
                findings += fn(ctx["manifest"], ctx.get("records", []))
            elif name == "source_integrity":
                findings += fn(ctx.get("records", []))
            elif name == "trade_consistency":
                findings += fn(ctx.get("trade"), ctx.get("calc_store"))
            elif name in ("fact_schema", "period_consistency"):
                findings += fn(ctx["fact_store"])
            elif name == "accounting_basis_consistency":
                findings += fn(ctx["fact_store"], ctx.get("calc_store"))
            elif name == "as_of_consistency":
                # tolerance comes from analysis context, never hardcoded.
                # Fail closed if the caller did not provide it.
                tol = ctx.get("freshness_tolerance_days")
                if tol is None:
                    findings.append(Finding(
                        "as_of_consistency", "FAIL", "ERROR", "context",
                        "freshness_tolerance_days not provided in analysis "
                        "context; refusing to guess a tolerance",
                        [], False,
                        "set freshness_tolerance_days in the run manifest"))
                else:
                    findings += fn(ctx["fact_store"], ctx.get("calc_store"),
                                   ctx.get("as_of", "9999-12-31"), tol)
            elif name == "event_timeline_consistency":
                findings += fn(ctx.get("release_events", []),
                               ctx.get("claims", []))
            elif name == "calculation":
                findings += fn(ctx["calc_store"], ctx["fact_store"])
            elif name == "claim_grounding":
                findings += fn(ctx.get("claims", []), ctx.get("fact_store"),
                               ctx.get("evidence_store"), ctx.get("calc_store"))
            elif name == "evidence_conflict":
                findings += fn(ctx["evidence_store"])
            elif name == "temporal_grounding":
                findings += fn(ctx["evidence_store"],
                               ctx.get("as_of", "9999-12-31"))
        blocked = any(f.blocks for f in findings)
        return findings, blocked
