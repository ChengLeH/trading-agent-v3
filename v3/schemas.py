#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 schemas.py — structured event schemas for timeline consistency.

event_timeline_consistency (2026-10-15): abstracted from the CBRS 171.1M
lockup error, where an AGGREGATE cap ("up to ~171.1M shares released during
the lock-up period") was misread as a FINAL BATCH ("171.1M shares unlock on
11/10").

DESIGN RULE (user decision 2026-10-15):
  - Schema is defined FIRST. The checker operates on structured input.
  - Do NOT write regex to guess events from prose. The DEEP extraction
    layer will populate ReleaseEvent[] directly (integration comes later).
  - The checker only does timeline reconciliation: sum(individual events)
    vs aggregate caps, and validates that claims citing a "batch on DATE"
    bind to a real individual event, not an aggregate cap.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict


@dataclass
class ReleaseEvent:
    """A single share-release / lockup-expiry / warrant-exercise event."""
    date: str            # YYYY-MM-DD (expected release/trade date)
    condition: str       # e.g. "Q3 earnings release", "180 days post-IPO"
    shares: int          # number of shares for THIS event
    is_aggregate: bool   # True = cumulative cap across the period, NOT an
                         #   individual event. Must never be cited as a
                         #   "final batch on DATE".
    source_ref: str      # e.g. "S-1 p.112", "10-Q Note 7"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ReleaseEvent":
        return cls(
            date=d["date"],
            condition=d.get("condition", ""),
            shares=int(d["shares"]),
            is_aggregate=bool(d.get("is_aggregate", False)),
            source_ref=d.get("source_ref", ""),
        )
