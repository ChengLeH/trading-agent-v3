#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Typed source bindings for V4 claims.

Important: this module NEVER parses claim prose. It only validates explicit
metadata attached to a claim. Semantic correctness of prose remains an LLM job.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional
from .schema import DIMENSIONS


@dataclass
class SourceBinding:
    """What a claim says one referenced source means.

    All dimensions are optional during migration, but when supplied they must
    match the referenced canonical FACT exactly. This catches entity/metric/
    period/scope/unit/basis misbinding without keyword or regex rules.
    """

    ref_id: str
    entity: Optional[str] = None
    concept: Optional[str] = None
    period: Optional[str] = None
    scope: Optional[str] = None
    unit: Optional[str] = None
    basis: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BindingFinding:
    claim_id: str
    ref_id: str
    status: str  # PASS | FAIL | WARN
    dimension: str
    expected: Any
    actual: Any
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fact_dict(fact_store) -> Dict[str, Dict[str, Any]]:
    return fact_store.to_dict() if fact_store is not None else {}


def validate_claim_bindings(
    claims: Iterable[Dict[str, Any]],
    fact_store=None,
    *,
    require_bindings: bool = False,
    sources=None,
) -> List[BindingFinding]:
    """Compare source bindings with canonical typed metadata, never prose.

    Known Claim bindings describe an upstream premise, not a source record.
    Their attribution is checked by mandatory LLM reference review; structural
    declaration and DAG checks remain required.
    """

    claims = list(claims)
    claim_ids = {c.get("claim_id") for c in claims if isinstance(c.get("claim_id"), str)}
    facts = _fact_dict(fact_store)
    canonical = sources if sources is not None else facts
    out: List[BindingFinding] = []
    for claim in claims:
        cid = claim.get("claim_id", "?")
        bindings = claim.get("bindings") or []
        grounded = claim.get("claim_type") in {
            "source_fact", "derived_fact", "semantic_fact"
        }
        if grounded and not bindings:
            out.append(BindingFinding(
                claim_id=cid,
                ref_id="",
                status="FAIL" if require_bindings else "WARN",
                dimension="bindings",
                expected="at least one explicit binding",
                actual="missing",
                reason="grounded claim has no typed source binding",
            ))
            continue

        for raw in bindings:
            if not isinstance(raw, dict):
                out.append(BindingFinding(
                    cid, "", "FAIL", "binding", "object", type(raw).__name__,
                    "binding must be a JSON object",
                ))
                continue
            ref_id = raw.get("ref_id", "")
            if not ref_id:
                out.append(BindingFinding(
                    cid, "", "FAIL", "ref_id", "non-empty", ref_id,
                    "binding has no ref_id",
                ))
                continue
            if require_bindings:
                for dim in DIMENSIONS:
                    if not isinstance(raw.get(dim), str) or not raw[dim]:
                        out.append(BindingFinding(cid, ref_id, "FAIL", dim,
                                                  "explicit dimension", raw.get(dim),
                                                  "typed binding dimension required"))
                if ref_id not in claim.get("refs", []):
                    out.append(BindingFinding(cid, ref_id, "FAIL", "ref_id",
                                              "declared reference", ref_id,
                                              "binding ref is not declared"))
            if ref_id in claim_ids and ref_id not in canonical:
                if require_bindings and ref_id not in claim.get("dependencies", []):
                    out.append(BindingFinding(cid, ref_id, "FAIL", "ref_id",
                                              "declared Claim dependency", ref_id,
                                              "Claim binding must name a declared dependency"))
                continue  # its meaning is verified by the LLM, not source dimensions
            fact = canonical.get(ref_id)
            if fact is None:
                # Unknown IDs are not inferred from spelling or prose. Claim
                # references were resolved by actual membership above.
                if require_bindings:
                    out.append(BindingFinding(cid, ref_id, "FAIL", "ref_id",
                                              "existing source", ref_id,
                                              "binding source not found"))
                continue

            for dim in ("entity", "concept", "period", "scope", "unit", "basis"):
                expected = raw.get(dim)
                if expected in (None, ""):
                    continue
                if dim not in fact or fact[dim] in (None, "", "unknown"):
                    # An incomplete evidence extraction is not proof of a
                    # mismatch. Every such claim still goes to the LLM.
                    continue
                actual = fact.get(dim)
                if expected != actual:
                    out.append(BindingFinding(
                        cid, ref_id, "FAIL", dim, expected, actual,
                        f"claim binding {dim} does not match canonical FACT",
                    ))
                else:
                    out.append(BindingFinding(
                        cid, ref_id, "PASS", dim, expected, actual,
                        f"claim binding {dim} matches canonical FACT",
                    ))
            for dim in ("share_basis", "effective_date"):
                if dim in raw and fact.get(dim) not in (None, "", "unknown") and raw[dim] != fact[dim]:
                    out.append(BindingFinding(cid, ref_id, "FAIL", dim,
                                              raw[dim], fact[dim], "explicit dimension mismatch"))
            assertion = claim.get("assertion", {})
            if (assertion.get("kind") == "value" and assertion.get("ref_id") == ref_id
                    and "value" in fact and assertion.get("value") != fact["value"]):
                out.append(BindingFinding(cid, ref_id, "FAIL", "value",
                                          assertion.get("value"), fact["value"],
                                          "explicit asserted value differs from source"))
    return out
