#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V4 regression tests.

These tests intentionally prove that Python never decides semantic correctness
from claim prose. Only explicit typed bindings are checked deterministically.
"""

from types import SimpleNamespace
import unittest

from v3.facts import Fact, FactStore, CalcStore
from v3.evidence import EvidenceStore
from v4.atomic_phase6 import build_atomic_tasks, summarize_atomic_results
from v4.bindings import validate_claim_bindings


def _fact(fid, *, entity, concept, period, scope, value=1, unit="KRW", basis="reported"):
    return Fact(
        fact_id=fid,
        value=value,
        unit=unit,
        period=period,
        concept=concept,
        basis=basis,
        scope=scope,
        entity=entity,
        source_id="fixture",
        source_pointer="fixture",
        source_hash="fixture",
    )


def make_run(claims):
    fs = FactStore()
    fs.add(_fact(
        "fact.dram_supply.2029",
        entity="Samsung+SK hynix",
        concept="DRAMWaferSupply",
        period="2029",
        scope="combined",
        value=900000,
        unit="wafers/month",
    ))
    return SimpleNamespace(
        fact_store=fs,
        calc_store=CalcStore(),
        evidence_store=EvidenceStore(),
        claims=claims,
    )


def test_binding_catches_scope_misbinding_without_reading_text():
    claim = {
        "claim_id": "bull.1",
        "claim_type": "source_fact",
        # Deliberately harmless prose: checker must still fail from metadata.
        "text": "Demand remains strong.",
        "refs": ["fact.dram_supply.2029"],
        "bindings": [{
            "ref_id": "fact.dram_supply.2029",
            "entity": "SK hynix",
            "concept": "DRAMWaferSupply",
            "period": "2029",
            "scope": "single_company",
            "unit": "wafers/month",
            "basis": "reported",
        }],
    }
    findings = validate_claim_bindings([claim], make_run([claim]).fact_store)
    assert any(f.status == "FAIL" and f.dimension in {"entity", "scope"} for f in findings)


def test_same_bad_prose_does_not_trigger_python_semantic_filter():
    claim = {
        "claim_id": "bull.2",
        "claim_type": "interpretation",
        "text": "SK hynix single-company supply is 900,000 wafers per month.",
        "refs": ["fact.dram_supply.2029"],
        "bindings": [],
    }
    findings = validate_claim_bindings([claim], make_run([claim]).fact_store)
    # It is interpretation and has no explicit binding: deterministic checker
    # must not infer semantic truth from prose. The LLM atomic review catches it.
    assert not any(f.status == "FAIL" for f in findings)


def test_every_claim_requires_llm_result():
    claims = [
        {"claim_id": "c1", "claim_type": "interpretation", "text": "A", "refs": []},
        {"claim_id": "c2", "claim_type": "interpretation", "text": "B", "refs": []},
    ]
    summary = summarize_atomic_results(
        ["c1", "c2"],
        {"c1": {"claim_id": "c1", "verdict": "PASS", "findings": []}},
    )
    assert summary["complete"] is False
    assert summary["missing_claim_ids"] == ["c2"]


def test_atomic_task_resolves_explicit_refs_only():
    claim = {
        "claim_id": "c1",
        "claim_type": "source_fact",
        "text": "whatever prose",
        "refs": ["fact.dram_supply.2029"],
    }
    tasks = build_atomic_tasks(make_run([claim]))
    assert len(tasks) == 1
    assert "fact.dram_supply.2029" in tasks[0].resolved_refs
    assert tasks[0].unresolved_refs == []


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(v) for k, v in sorted(globals().items())
                              if k.startswith("test_") and callable(v))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"{len(tests)} passed")
