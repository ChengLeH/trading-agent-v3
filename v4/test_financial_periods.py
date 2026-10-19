"""Guard V4 revenue calculations against mismatched fiscal-year coverage."""
import unittest
from types import SimpleNamespace
from v3.pipeline import RunState
from .pipeline import OrchestratorV4

class FundamentalYearCoverage(unittest.TestCase):
    def make_run(self, balance_years):
        orch, rid = OrchestratorV4(), "period-coverage-test"
        orch.runs[rid] = RunState(run_id=rid, ticker="UBER",
            manifest={"subject": {"ticker": "UBER"}, "as_of": "2026-10-19"})
        income = [
            {"fiscal_year": 2022, "total_revenue": 33_203_000_000},
            {"fiscal_year": 2023, "total_revenue": 35_819_000_000},
            {"fiscal_year": 2024, "total_revenue": 51_574_000_000},
            {"fiscal_year": 2025, "total_revenue": 63_887_000_000},
        ]
        balance = [{"fiscal_year": y} for y in balance_years]
        orch.runs[rid].records = [SimpleNamespace(
            type="openbb_fundamentals", id="test_financials", source_hash="source-sha",
            payload=dict(income=income, balance=balance, cash_flow=[]))]
        return orch, rid

    def test_balance_missing_fy2022_does_not_truncate_revenue_cagr(self):
        orch, rid = self.make_run([2023, 2024, 2025])
        report = orch.build_facts(rid)
        facts = orch.runs[rid].fact_store.to_dict()
        calcs = orch.runs[rid].calc_store.to_dict()
        self.assertIn("fact.Revenue.FY2022", facts)
        self.assertEqual(calcs["calc.cagr.rev"]["input_refs"],
                         ["fact.Revenue.FY2022", "fact.Revenue.FY2025"])
        self.assertAlmostEqual(calcs["calc.cagr.rev"]["value"], .243784, places=6)
        self.assertEqual(calcs["calc.cagr.rev"]["meta"]["years"], 3.)
        self.assertEqual(calcs["calc.rev_growth.FY2023"]["input_refs"],
                         ["fact.Revenue.FY2023", "fact.Revenue.FY2022"])
        self.assertEqual(report["calcs"], len(calcs))

    def test_balance_empty_still_uses_income_for_revenue_calcs(self):
        orch, rid = self.make_run([])
        orch.build_facts(rid)
        calcs = orch.runs[rid].calc_store.to_dict()
        self.assertEqual(calcs["calc.cagr.rev"]["meta"]["years"], 3.)
        self.assertEqual({k for k in calcs if k.startswith("calc.rev_growth.")},
                         {"calc.rev_growth.FY2023", "calc.rev_growth.FY2024", "calc.rev_growth.FY2025"})

    def test_complete_balance_periods_remain_consistent(self):
        orch, rid = self.make_run([2022, 2023, 2024, 2025])
        orch.build_facts(rid)
        calcs = orch.runs[rid].calc_store.to_dict()
        self.assertAlmostEqual(calcs["calc.cagr.rev"]["value"], .243784, places=6)
        self.assertEqual(calcs["calc.cagr.rev"]["input_refs"],
                         ["fact.Revenue.FY2022", "fact.Revenue.FY2025"])

if __name__ == "__main__":
    unittest.main()
