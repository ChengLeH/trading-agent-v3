#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 facts.py — FACT STORE. Facts are only created by deterministic code,
never by LLM reading. Schema per spec §4."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Fact:
    fact_id: str
    value: Any
    unit: str
    period: str            # e.g. "2026-Q2", "2026-H1", "FY2025", "2026-10-14"
    start: Optional[str] = None
    end: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None   # Q1/Q2/Q3/Q4/FY
    concept: str = ""                     # e.g. "Revenue", "NetIncome", "Close"
    # --- accounting basis (user decision 2026-10-15: independent dimension) ---
    basis: str = "unknown"                # pre_tax | post_tax | gaap | non_gaap | ...
    scope: str = "unknown"                # consolidated | segment:AWS | ...
    share_basis: str = "unknown"          # basic | diluted | outstanding
    # --- provenance ---
    source_id: str = ""
    source_pointer: str = ""              # e.g. "income[0].Revenue"
    accession_or_version: str = ""
    source_hash: str = ""
    # --- as_of_consistency (2026-10-15): observation/effective date ---
    effective_date: str = ""              # YYYY-MM-DD; required for latest/current FACTs
    # --- entity binding (was entity_period_consistency checker, removed 2026-10-16) ---
    entity: str = ""                      # e.g. "OpenAI", "MBZUAI"; "" if not entity-specific
    # Additive V4 canonical dimensions. Existing V3 builders keep their
    # behavior; callers that do not declare these values retain safe defaults.
    period_type: str = "unknown"           # instant | duration | rolling | unknown
    period_coverage: str = "unknown"       # annual | single_period | year_to_date | rolling | point_in_time | unknown
    published_or_filing_time: str = ""     # when this value became publicly available

    def identity_key(self) -> str:
        """Metric identity for cross-report consistency (spec §16)."""
        return "|".join([self.concept, self.period, self.unit,
                         self.basis, self.scope, self.share_basis])

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Keep V3's serialized schema stable when callers leave the additive
        # V4 dimensions at their defaults. V4 facts with declared metadata
        # persist those fields through the same canonical store.
        if self.period_type == "unknown":
            data.pop("period_type")
        if self.period_coverage == "unknown":
            data.pop("period_coverage")
        if not self.published_or_filing_time:
            data.pop("published_or_filing_time")
        return data


class FactStore:
    def __init__(self):
        self._facts: Dict[str, Fact] = {}

    def add(self, fact: Fact):
        if not fact.fact_id:
            raise ValueError("fact_id required")
        if fact.fact_id in self._facts:
            raise ValueError(f"duplicate fact_id {fact.fact_id}")
        for req in ("unit", "period", "concept", "source_id"):
            if not getattr(fact, req):
                raise ValueError(f"fact {fact.fact_id} missing {req}")
        self._facts[fact.fact_id] = fact

    def get(self, fact_id: str) -> Optional[Fact]:
        return self._facts.get(fact_id)

    def find(self, concept: str = None, period: str = None) -> List[Fact]:
        out = list(self._facts.values())
        if concept:
            out = [f for f in out if f.concept == concept]
        if period:
            out = [f for f in out if f.period == period]
        return out

    def check_identity_collisions(self) -> List[Dict[str, Any]]:
        """Same metric identity, different values -> must not happen (spec §16)."""
        seen: Dict[str, Fact] = {}
        collisions = []
        for f in self._facts.values():
            k = f.identity_key()
            if k in seen and seen[k].value != f.value:
                collisions.append({
                    "identity": k,
                    "fact_ids": [seen[k].fact_id, f.fact_id],
                    "values": [seen[k].value, f.value],
                })
            else:
                seen[k] = f
        return collisions

    def to_dict(self) -> Dict[str, Any]:
        return {fid: f.to_dict() for fid, f in self._facts.items()}

    def snapshot_hash(self) -> str:
        raw = json.dumps(self.to_dict(), sort_keys=True, default=str).encode()
        return hashlib.sha256(raw).hexdigest()[:16]


class CalcStore:
    """CALC STORE: derived values, each traceable to input fact_ids."""

    def __init__(self):
        self._calcs: Dict[str, Dict[str, Any]] = {}

    def add(self, calc_id: str, value: Any, calc_type: str,
            input_refs: List[str], unit: str = "", meta: Dict[str, Any] = None):
        if calc_id in self._calcs:
            raise ValueError(f"duplicate calc_id {calc_id}")
        self._calcs[calc_id] = {
            "calc_id": calc_id, "value": value, "calc_type": calc_type,
            "input_refs": input_refs, "unit": unit, "meta": meta or {},
        }

    def get(self, calc_id: str) -> Optional[Dict[str, Any]]:
        return self._calcs.get(calc_id)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._calcs)
