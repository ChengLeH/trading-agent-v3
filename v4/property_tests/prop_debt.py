"""Debt truth table: missing is not zero; overlap requires an explicit boolean."""
import unittest

from hypothesis import example, given, strategies as st

from v3.pipeline import RunState
from v3.sources import SourceRecord
from v4.pipeline import OrchestratorV4


FIELDS = ('short_term_debt', 'current_portion_of_long_term_debt', 'long_term_debt')
MISSING = 'absent'
AMOUNTS = st.one_of(st.just(MISSING), st.none(), st.just(0), st.integers(1, 1000000))
DECLARATIONS = st.sampled_from((MISSING, None, False, True, 'true', 'false'))


def build(row):
    record = SourceRecord('filing', 'openbb_fundamentals', 'TEST', None,
                          dict(income=[], balance=[row], cash_flow=[]), 'fixture-hash')
    orch = OrchestratorV4()
    orch.runs['property'] = RunState(run_id='property', ticker='TEST',
        manifest={'subject': {'ticker': 'TEST'}, 'as_of': '2026-10-20'}, records=[record])
    report = orch.build_facts('property')
    return orch.runs['property'].fact_store.to_dict(), report


class DebtProperties(unittest.TestCase):
    @example(amounts=(0, 0, 0), complete=True, overlap=False)
    @example(amounts=(None, 0, 1), complete=True, overlap=False)
    @example(amounts=(7, 7, 10), complete=True, overlap=True)
    @example(amounts=(7, 7, 10), complete=True, overlap=False)
    @given(amounts=st.tuples(AMOUNTS, AMOUNTS, AMOUNTS),
           complete=DECLARATIONS, overlap=DECLARATIONS)
    def test_component_values_and_exact_missing_diagnostics(self, amounts, complete, overlap):
        row = dict(fiscal_year=2025)
        row.update({k: v for k, v in zip(FIELDS, amounts) if v != MISSING})
        for key, value in [('debt_components_complete', complete),
                           ('current_portion_in_short_term_debt', overlap)]:
            if value != MISSING:
                row[key] = value
        facts, report = build(row)
        # Independent truth table: only complete, known-overlap, nonmissing
        # components may yield a derived total. None and absent remain distinct
        # source representations with the same absence of numerical evidence.
        missing = [field for field, value in zip(FIELDS, amounts)
                   if value is None or value == MISSING]
        eligible = not missing and complete is True and type(overlap) is bool
        total = facts.get('fact.TotalDebt.FY2025')
        self.assertEqual(total is not None, eligible)
        if eligible:
            short, current, long = amounts
            expected = long + short if overlap else long + short + current
            self.assertEqual(total['value'], expected)
        concepts = ('ShortTermDebt', 'CurrentPortionOfLongTermDebt', 'LongTermDebt')
        for field, concept, value in zip(FIELDS, concepts, amounts):
            fact = facts.get(f'fact.{concept}.FY2025')
            self.assertEqual(fact is None, field in missing)
            if field not in missing:
                self.assertEqual(fact['value'], value)

        reasons = [f'missing component: {field}' for field in missing]
        if complete is not True:
            reasons.append('missing declaration: debt_components_complete'
                if complete is None or complete == MISSING else
                'incomplete declaration: debt_components_complete=false' if complete is False else
                'invalid declaration: debt_components_complete (expected true)')
        if type(overlap) is not bool:
            reasons.append('missing declaration: current_portion_in_short_term_debt'
                if overlap is None or overlap == MISSING else
                'invalid declaration: current_portion_in_short_term_debt (expected boolean)')
        diagnostics = [line.split('TotalDebt unavailable: ', 1)[1] for line in report['limitations']
                       if 'TotalDebt unavailable: ' in line]
        has_input = any(value is not None and value != MISSING for value in (*amounts, complete, overlap))
        self.assertEqual(diagnostics, ['; '.join(reasons)] if not eligible and has_input else [])

    @given(total=st.integers(0, 1000000), current=st.integers(0, 1000000))
    def test_reported_total_survives_missing_components_without_inventing_them(self, total, current):
        facts, report = build(dict(fiscal_year=2025, total_debt=total,
                                  current_portion_of_long_term_debt=current))
        self.assertEqual(facts['fact.TotalDebt.FY2025']['value'], total)
        self.assertEqual(facts['fact.CurrentPortionOfLongTermDebt.FY2025']['value'], current)
        self.assertNotIn('fact.ShortTermDebt.FY2025', facts)
        self.assertNotIn('fact.LongTermDebt.FY2025', facts)
        self.assertFalse(any('TotalDebt unavailable' in line for line in report['limitations']))
