#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 sources.py — typed source adapters. Fail-closed: never leak raw parser
exceptions to agents; return typed errors instead."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# Spec §6 suggested source types, plus openbb_* for the VM data layer.
SOURCE_TYPES = {
    "sec_companyfacts", "sec_filing_html", "sec_filing_xbrl",
    "yahoo_chart", "openbb_ohlcv", "openbb_fundamentals",
    "news_json", "rss_xml", "article_html",
    "social_json", "generic_text",
}

# Typed errors (spec §12). Agents get retryable + object, never raw tracebacks.
SOURCE_FORMAT_UNSUPPORTED = "SOURCE_FORMAT_UNSUPPORTED"
SOURCE_IDENTITY_MISMATCH = "SOURCE_IDENTITY_MISMATCH"


@dataclass
class SourceError:
    code: str
    message: str
    source_id: str
    retryable: bool = False


@dataclass
class SourceRecord:
    """One normalized source: bytes in, typed record out."""
    id: str
    type: str
    subject_ticker: str
    subject_cik: Optional[str]
    payload: Any                    # parsed, type-specific
    source_hash: str
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def load_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a Source Manifest (spec §6). Returns normalized manifest.

    as_of MUST survive normalization: it anchors temporal grounding for the
    whole run. Dropping it caused the final gate to fall back to 9999-12-31.
    """
    subject = manifest.get("subject", {})
    if not subject.get("ticker"):
        raise ValueError("manifest.subject.ticker is required")
    sources = manifest.get("sources", [])
    if not sources:
        raise ValueError("manifest.sources must be non-empty")
    for s in sources:
        if s.get("type") not in SOURCE_TYPES:
            raise ValueError(f"unsupported source type: {s.get('type')}")
        if not s.get("id") or not s.get("path"):
            raise ValueError(f"source entry needs id and path: {s}")
    as_of = manifest.get("as_of")
    if not as_of:
        raise ValueError("manifest.as_of is required (YYYY-MM-DD)")
    out = {
        "subject": {
            "ticker": subject["ticker"].upper(),
            "cik": subject.get("cik"),
        },
        "as_of": as_of,
        "sources": sources,
    }
    # Preserve freshness tolerance as-is (per-analysis, never defaulted here).
    # Dropped by normalization before 2026-10-15, causing as_of_consistency
    # to fail closed on every run.
    if "freshness_tolerance_days" in manifest:
        out["freshness_tolerance_days"] = manifest["freshness_tolerance_days"]
    return out


class AdapterRegistry:
    """Maps source type -> adapter function. Adapters are pure functions:
    (source_id, path_or_payload, subject) -> SourceRecord | SourceError."""

    def __init__(self):
        self._adapters: Dict[str, Any] = {}

    def register(self, source_type: str, fn):
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"unknown source type {source_type}")
        self._adapters[source_type] = fn

    def adapt(self, entry: Dict[str, Any], subject: Dict[str, Any]):
        stype = entry["type"]
        fn = self._adapters.get(stype)
        if fn is None:
            return SourceError(
                code=SOURCE_FORMAT_UNSUPPORTED,
                message=f"no adapter registered for type '{stype}'",
                source_id=entry["id"],
                retryable=False,
            )
        try:
            return fn(entry["id"], entry["path"], subject)
        except Exception as e:  # fail-closed: typed error, not raw exception
            return SourceError(
                code=SOURCE_FORMAT_UNSUPPORTED,
                message=f"adapter '{stype}' failed: {type(e).__name__}: {str(e)[:200]}",
                source_id=entry["id"],
                retryable=True,
            )


# ---------------------------------------------------------------------------
# Built-in adapters (VM data layer)
# ---------------------------------------------------------------------------

def adapt_openbb_ohlcv(source_id: str, path: str, subject: Dict[str, Any]):
    """path is a JSON file produced by openbb_data.py (or compatible)."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    bars = payload.get("ohlcv", [])
    if not bars:
        return SourceError(SOURCE_FORMAT_UNSUPPORTED, "empty ohlcv payload",
                           source_id, retryable=True)
    # identity: verify payload ticker against manifest subject
    payload_ticker = (payload.get("_metadata", {}) or {}).get("ticker", "")
    want = subject["ticker"].upper()
    identity_ok = payload_ticker.upper() == want if payload_ticker else None
    if payload_ticker and not identity_ok:
        return SourceError(SOURCE_FORMAT_UNSUPPORTED,
                           f"ticker mismatch: payload={payload_ticker} "
                           f"subject={want}", source_id, retryable=False)
    raw = json.dumps(bars, sort_keys=True).encode()
    return SourceRecord(
        id=source_id, type="openbb_ohlcv",
        subject_ticker=subject["ticker"], subject_cik=subject.get("cik"),
        payload={"bars": bars, "quote": payload.get("quote", {})},
        source_hash=_hash_bytes(raw),
        meta={"bar_count": len(bars),
              "identity_verified": identity_ok,
              "payload_ticker": payload_ticker or "unknown"},
    )


def adapt_openbb_fundamentals(source_id: str, path: str, subject: Dict[str, Any]):
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    fund = payload.get("fundamentals", {})
    if not fund or not any(k in fund for k in ("income", "balance", "cash_flow")):
        return SourceError(SOURCE_FORMAT_UNSUPPORTED,
                           "no income/balance/cash_flow in payload",
                           source_id, retryable=True)
    payload_ticker = (payload.get("_metadata", {}) or {}).get("ticker", "")
    want = subject["ticker"].upper()
    identity_ok = payload_ticker.upper() == want if payload_ticker else None
    if payload_ticker and not identity_ok:
        return SourceError(SOURCE_FORMAT_UNSUPPORTED,
                           f"ticker mismatch: payload={payload_ticker} "
                           f"subject={want}", source_id, retryable=False)
    raw = json.dumps(fund, sort_keys=True, default=str).encode()
    return SourceRecord(
        id=source_id, type="openbb_fundamentals",
        subject_ticker=subject["ticker"], subject_cik=subject.get("cik"),
        payload=fund,
        source_hash=_hash_bytes(raw),
        meta={"identity_verified": identity_ok,
              "payload_ticker": payload_ticker or "unknown"},
    )


def adapt_news_json(source_id: str, path: str, subject: Dict[str, Any]):
    """News pipeline (reworked): no Alpha Vantage. The assistant fills
    news_json via browser.search (news vertical) + social.search; this
    adapter only validates the envelope."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items", [])
    if not isinstance(items, list):
        return SourceError(SOURCE_FORMAT_UNSUPPORTED,
                           "news_json needs items[]", source_id, retryable=True)
    for it in items:
        if not it.get("title") or not it.get("url"):
            return SourceError(
                SOURCE_FORMAT_UNSUPPORTED,
                "news item needs title and url", source_id, retryable=True)
    raw = json.dumps(items, sort_keys=True, default=str).encode()
    return SourceRecord(
        id=source_id, type="news_json",
        subject_ticker=subject["ticker"], subject_cik=subject.get("cik"),
        payload={"items": items},
        source_hash=_hash_bytes(raw),
        meta={"item_count": len(items),
              "collection": "assistant browser.search + social.search (no Alpha Vantage)"},
    )


def adapt_generic_text(source_id: str, path: str, subject: Dict[str, Any]):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return SourceError(SOURCE_FORMAT_UNSUPPORTED, "empty text file",
                           source_id, retryable=True)
    return SourceRecord(
        id=source_id, type="generic_text",
        subject_ticker=subject["ticker"], subject_cik=subject.get("cik"),
        payload={"text": text},
        source_hash=_hash_bytes(text.encode()),
        meta={"chars": len(text)},
    )


def adapt_social_json(source_id: str, path: str, subject: Dict[str, Any]):
    """Social pipeline (assistant social.search). Same envelope as news_json,
    but items use 'text' (or 'title') + 'url'. Fixed 2026-10-15 DELL run:
    SOURCE_TYPES declared social_json but no adapter was registered."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items", [])
    if not isinstance(items, list):
        return SourceError(SOURCE_FORMAT_UNSUPPORTED,
                           "social_json needs items[]", source_id,
                           retryable=True)
    for it in items:
        if not (it.get("title") or it.get("text")) or not it.get("url"):
            return SourceError(
                SOURCE_FORMAT_UNSUPPORTED,
                "social item needs title/text and url", source_id,
                retryable=True)
    raw = json.dumps(items, sort_keys=True, default=str).encode()
    return SourceRecord(
        id=source_id, type="social_json",
        subject_ticker=subject["ticker"], subject_cik=subject.get("cik"),
        payload={"items": items},
        source_hash=_hash_bytes(raw),
        meta={"item_count": len(items),
              "collection": "assistant social.search"},
    )


def default_registry() -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register("openbb_ohlcv", adapt_openbb_ohlcv)
    reg.register("openbb_fundamentals", adapt_openbb_fundamentals)
    reg.register("news_json", adapt_news_json)
    reg.register("social_json", adapt_social_json)
    reg.register("generic_text", adapt_generic_text)
    return reg


# ---- Point 3: Fundamentals source priority ----
# Latest SEC / company IR > old SEC > Reuters/mainstream > other sources.
SOURCE_PRIORITY = [
    "sec_companyfacts",      # latest SEC
    "sec_filing_xbrl",       # SEC filing
    "sec_filing_html",       # SEC filing (html)
    "company_ir",            # company investor relations
    "openbb_fundamentals",   # OpenBB (aggregates SEC)
    "reuters",               # Reuters
    "mainstream_media",      # Bloomberg, WSJ, FT, etc.
    "news_json",             # general news
    "social_json",           # social (lowest for fundamentals)
    "generic_text",          # fallback
]


def source_priority_rank(source_type: str) -> int:
    """Lower number = higher priority. Unknown types go last."""
    try:
        return SOURCE_PRIORITY.index(source_type)
    except ValueError:
        return len(SOURCE_PRIORITY)


def pick_best_source(candidates: list) -> dict | None:
    """Pick the highest-priority source from a list of {source_type, ...} dicts.
    Ties broken by most recent date if 'date' key present."""
    if not candidates:
        return None
    def key(c):
        rank = source_priority_rank(c.get("source_type", ""))
        # More recent = smaller key (negative timestamp)
        date = c.get("date", "")
        return (rank, date)
    return min(candidates, key=key)
