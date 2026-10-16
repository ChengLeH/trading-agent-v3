#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 evidence.py — Evidence Packet schema + provider interface.
Spec §5: the layer uses Sirchmunk DEEP, Muse's own deep extraction, or any
replaceable provider. What matters is the OUTPUT FORMAT, not the impl.

Default provider here: the local-deepread pipeline
(Extract -> Expand -> Filter -> Deep-read), no external APIs, no embeddings.
DEEP extracts evidence only; it never answers the final investment question
and never replaces authoritative numeric sources."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class EvidencePacket:
    evidence_id: str
    claim_candidate: str
    source_id: str
    source_type: str
    source_span: str              # e.g. "p.42 ll.10-18" / "sec filing p.112"
    section: str = ""
    published_or_filing_time: str = ""
    company: str = ""
    period: str = ""
    topic: str = ""
    polarity_if_applicable: str = ""   # positive | negative | neutral | ""
    confidence: float = 0.0
    extraction_method: str = ""
    source_hash: str = ""
    conflict_group_id: str = ""        # set when sources disagree (spec §5)
    # --- entity binding (was entity_period_consistency checker, removed 2026-10-16) ---
    entity: str = ""                      # e.g. "OpenAI", "MBZUAI"; "" if not entity-specific

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvidenceStore:
    def __init__(self):
        self._items: Dict[str, EvidencePacket] = {}

    def add(self, ev: EvidencePacket):
        if ev.evidence_id in self._items:
            raise ValueError(f"duplicate evidence_id {ev.evidence_id}")
        if not ev.claim_candidate or not ev.source_id or not ev.source_span:
            raise ValueError("evidence needs claim_candidate, source_id, source_span")
        self._items[ev.evidence_id] = ev

    def find(self, topic: str = None, period: str = None) -> List[EvidencePacket]:
        out = list(self._items.values())
        if topic:
            out = [e for e in out if e.topic == topic]
        if period:
            out = [e for e in out if e.period == period]
        return out

    def conflicts(self) -> Dict[str, List[EvidencePacket]]:
        groups: Dict[str, List[EvidencePacket]] = {}
        for e in self._items.values():
            if e.conflict_group_id:
                groups.setdefault(e.conflict_group_id, []).append(e)
        return groups

    def to_dict(self) -> Dict[str, Any]:
        return {eid: e.to_dict() for eid, e in self._items.items()}


class EvidenceProvider:
    """Interface. extract() returns EvidencePackets for a topic."""
    name = "base"

    def extract(self, topic: str, corpus: Dict[str, str],
                source_id: str, **kw) -> List[EvidencePacket]:
        raise NotImplementedError


class DeepReadProvider(EvidenceProvider):
    """local-deepread pipeline as an evidence provider.
    corpus: {doc_id: full_text}. The agent (or caller) drives semantic
    judging; this class handles extract/expand/filter mechanics."""

    name = "deepread"

    def __init__(self, max_passages: int = 50, context_lines: int = 3):
        self.max_passages = max_passages
        self.context_lines = context_lines

    @staticmethod
    def expand_terms(topic: str) -> List[str]:
        """Seed expansion. Callers (agent) should extend with domain terms;
        this keeps the mechanical default small and explicit."""
        base = [t for t in re.split(r"\W+", topic.lower()) if len(t) > 2]
        return list(dict.fromkeys(base))[:15]

    def filter_passages(self, text: str, terms: List[str]) -> List[Dict[str, Any]]:
        lines = text.splitlines()
        hits = []
        for i, line in enumerate(lines):
            low = line.lower()
            if any(t in low for t in terms):
                lo = max(0, i - self.context_lines)
                hi = min(len(lines), i + self.context_lines + 1)
                hits.append({
                    "line_start": lo + 1, "line_end": hi,
                    "text": "\n".join(lines[lo:hi]),
                })
                if len(hits) >= self.max_passages:
                    break
        return hits

    def extract(self, topic: str, corpus: Dict[str, str],
                source_id: str, extra_terms: List[str] = None,
                company: str = "", period: str = "",
                section: str = "") -> List[EvidencePacket]:
        terms = self.expand_terms(topic) + (extra_terms or [])
        out: List[EvidencePacket] = []
        for doc_id, text in corpus.items():
            for j, p in enumerate(self.filter_passages(text, terms)):
                span = f"{doc_id} ll.{p['line_start']}-{p['line_end']}"
                eid = f"ev_{abs(hash(source_id + span)) % 10**8:08d}_{j}"
                out.append(EvidencePacket(
                    evidence_id=eid,
                    claim_candidate=p["text"][:300],
                    source_id=source_id, source_type="generic_text",
                    source_span=span, section=section,
                    company=company, period=period, topic=topic,
                    extraction_method="deepread:expand->filter->agent-judge",
                    source_hash=hashlib.sha256(
                        p["text"].encode()).hexdigest()[:16],
                ))
        return out
