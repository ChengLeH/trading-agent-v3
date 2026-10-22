"""Regression tests for the eight reproduced V4 data-contract gaps."""
import unittest
from types import SimpleNamespace

from v3.facts import CalcStore, Fact, FactStore
from v3.pipeline import RunState
from v3.sources import SourceRecord
from .pipeline import OrchestratorV4, calculation_findings
from .reporting import render_report


def fundamentals_record(payload, source_id="filing"):
    return SourceRecord(source_id, "openbb_fundamentals", "TEST", None,
                        payload, "fixture-hash", meta={})


class FinancialSourceContracts(unittest.TestCase):
    def test_default_fact_serialization_preserves_the_v3_shape(self):
        fact = Fact("legacy", 1, "USD", "FY2025", concept="Revenue", source_id="fixture")
        row = fact.to_dict()
        self.assertNotIn("period_type", row)
        self.assertNotIn("period_coverage", row)
        self.assertNotIn("published_or_filing_time", row)

    def build(self, payload, *, as_of="2026-10-20"):
        orch, rid = OrchestratorV4(), "financial-gap-fixture"
        orch.runs[rid] = RunState(run_id=rid, ticker="TEST",
            manifest={"subject": {"ticker": "TEST"}, "as_of": as_of})
        orch.runs[rid].events = {}
        orch.runs[rid].records = [fundamentals_record(payload)]
        report = orch.build_facts(rid)
        return orch, rid, report

    def test_row_dimensions_override_legacy_defaults_and_keep_availability(self):
        orch, rid, _ = self.build({
            "income": [{"fiscal_year": 2025, "total_revenue": 100,
                        "unit": "EUR million", "basis": "ifrs",
                        "scope": "segment:mobility", "entity": "Example Ltd",
                        "start_date": "2024-01-01", "end_date": "2024-12-31",
                        "filing_date": "2025-03-01", "accession": "A-1"}],
            "balance": [], "cash_flow": []})
        fact = orch.runs[rid].fact_store.get("fact.Revenue.FY2025").to_dict()
        self.assertEqual((fact["value"], fact["unit"], fact["basis"],
                          fact["scope"], fact["entity"]),
                         (100, "EUR million", "ifrs", "segment:mobility", "Example Ltd"))
        self.assertEqual((fact["start"], fact["end"], fact["period_type"],
                          fact["period_coverage"]),
                         ("2024-01-01", "2024-12-31", "duration", "annual"))
        self.assertEqual(fact["published_or_filing_time"], "2025-03-01")
        self.assertEqual(fact["accession_or_version"], "A-1")

    def test_currency_scale_and_eps_dimension_are_preserved_without_guessing(self):
        orch, rid, report = self.build({
            "financial_metadata": {"currency": "EUR", "scale": "millions",
                                   "basis": "ifrs", "scope": "consolidated"},
            "income": [{"fiscal_year": 2025, "total_revenue": 12.5,
                        "diluted_eps": 2.75}],
            "balance": [], "cash_flow": []})
        facts = orch.runs[rid].fact_store.to_dict()
        self.assertEqual(facts["fact.Revenue.FY2025"]["unit"], "EUR millions")
        self.assertEqual(facts["fact.Revenue.FY2025"]["value"], 12.5)
        self.assertEqual(facts["fact.DilutedEPS.FY2025"]["unit"], "EUR_PER_SHARE")
        coverage = report["financial_data_coverage"][0]["coverage"][0]["produced_facts"]
        self.assertEqual({row["unit"] for row in coverage}, {"EUR millions", "EUR_PER_SHARE"})
        from .precision import known_unit, unit
        self.assertTrue(known_unit("EUR millions"))
        self.assertEqual(unit("EUR millions"), unit("EUR million"))

    def test_unknown_financial_scale_is_visible_and_never_converted(self):
        orch, rid, report = self.build({
            "financial_metadata": {"currency": "USD", "scale": "crore"},
            "income": [{"fiscal_year": 2025, "total_revenue": 42}],
            "balance": [], "cash_flow": []})
        fact = orch.runs[rid].fact_store.get("fact.Revenue.FY2025")
        self.assertEqual((fact.value, fact.unit), (42, "USD crore"))
        diagnostic = report["financial_data_coverage"][0]
        self.assertTrue(any("no numeric conversion was applied" in line
                            for line in diagnostic["limitations"]))

    def test_same_fiscal_year_quarters_keep_distinct_identity_and_intervals(self):
        orch, rid, _ = self.build({
            "income": [
                {"fiscal_year": 2026, "fiscal_period": "Q1", "total_revenue": 10,
                 "start_date": "2026-01-01", "end_date": "2026-03-31",
                 "period_coverage": "single_period"},
                {"fiscal_year": 2026, "fiscal_period": "Q2", "total_revenue": 12,
                 "start_date": "2026-04-01", "end_date": "2026-06-30",
                 "period_coverage": "single_period"},
                {"fiscal_year": 2026, "fiscal_period": "FY", "total_revenue": 45}],
            "balance": [], "cash_flow": []})
        facts = orch.runs[rid].fact_store.to_dict()
        quarters = [row for row in facts.values() if row["concept"] == "Revenue"]
        self.assertEqual({row["period"] for row in quarters},
                         {"FY2026-Q1", "FY2026-Q2", "FY2026"})
        q2 = next(row for row in quarters if row["fiscal_period"] == "Q2")
        self.assertEqual((q2["start"], q2["end"], q2["period_coverage"]),
                         ("2026-04-01", "2026-06-30", "single_period"))

    def test_future_filing_date_blocks_as_of_even_when_fiscal_period_is_historical(self):
        orch, rid, _ = self.build({
            "income": [{"fiscal_year": 2025, "total_revenue": 100,
                        "filing_date": "2026-11-05"}],
            "balance": [], "cash_flow": []}, as_of="2026-10-20")
        findings, blocked = orch.validate_stage(rid, "ingest")
        self.assertTrue(blocked)
        self.assertTrue(any(f.get("check_id") == "as_of_availability"
                            and f.get("object_id") == "fact.Revenue.FY2025"
                            for f in findings))

    def test_unrecognized_explicit_period_is_not_silently_replaced_by_fiscal_year(self):
        orch, rid, report = self.build({
            "income": [{"fiscal_year": 2025, "period": "FY2025-ish",
                        "total_revenue": 100}],
            "balance": [], "cash_flow": []})
        self.assertNotIn("fact.Revenue.FY2025", orch.runs[rid].fact_store.to_dict())
        diagnostics = report["financial_data_coverage"][0]["findings"]
        self.assertTrue(any(row["check_id"] == "financial_period_unknown"
                            and "not a recognized typed period" in row["reason"]
                            for row in diagnostics))

    def test_unrecognized_fiscal_period_is_not_promoted_to_a_fact(self):
        orch, rid, report = self.build({
            "income": [{"fiscal_year": 2025, "fiscal_period": "Q5",
                        "total_revenue": 100}],
            "balance": [], "cash_flow": []})
        self.assertNotIn("fact.Revenue.FY2025-Q5", orch.runs[rid].fact_store.to_dict())
        diagnostics = report["financial_data_coverage"][0]["findings"]
        self.assertTrue(any(row["check_id"] == "financial_period_unknown"
                            and "fiscal_period 'Q5'" in row["reason"]
                            for row in diagnostics))

    def test_partial_debt_is_retained_but_not_promoted_to_total(self):
        orch, rid, _ = self.build({"income": [], "balance": [
            {"fiscal_year": 2025, "short_term_debt": 10,
             "current_portion_of_long_term_debt": 4}], "cash_flow": []})
        facts = orch.runs[rid].fact_store.to_dict()
        self.assertNotIn("fact.TotalDebt.FY2025", facts)
        self.assertTrue(any(f["concept"] == "CurrentPortionOfLongTermDebt"
                            and f["value"] == 4 for f in facts.values()))
        self.assertTrue(any("TotalDebt unavailable" in line
                            for line in orch.runs[rid].records[0].meta[
                                "financial_data_extraction"]["limitations"]))

    def test_complete_debt_components_respect_current_portion_overlap(self):
        for included, expected in ((False, 34), (True, 30)):
            with self.subTest(current_portion_in_short_term_debt=included):
                orch, rid, report = self.build({"income": [], "balance": [{
                    "fiscal_year": 2025, "short_term_debt": 10,
                    "current_portion_of_long_term_debt": 4, "long_term_debt": 20,
                    "debt_components_complete": True,
                    "current_portion_in_short_term_debt": included}], "cash_flow": []})
                facts = orch.runs[rid].fact_store.to_dict()
                self.assertEqual(facts["fact.TotalDebt.FY2025"]["value"], expected)
                self.assertFalse(any('TotalDebt unavailable' in line for line in report['limitations']))

    def test_invalid_debt_component_never_enters_canonical_facts(self):
        # Source rows remain unchanged for forensic/audit purposes, while
        # rejected components cannot be cited as valid financial facts.
        from math import nan
        for invalid in (-5, -0.5, nan, float("inf"), "bad"):
            with self.subTest(source_value=repr(invalid)):
                row = dict(fiscal_year=2025, short_term_debt=invalid,
                    current_portion_of_long_term_debt=7, long_term_debt=11,
                    debt_components_complete=True,
                    current_portion_in_short_term_debt=False)
                orch, rid, report = self.build(dict(income=[], balance=[row], cash_flow=[]))
                facts = orch.runs[rid].fact_store.to_dict()
                self.assertNotIn("fact.ShortTermDebt.FY2025", facts)
                self.assertNotIn("fact.TotalDebt.FY2025", facts)
                self.assertIs(orch.runs[rid].records[0].payload["balance"][0]["short_term_debt"], invalid)
                diag = report["financial_data_coverage"][0]
                self.assertIn(dict(status="FAIL", check_id="debt_component_invalid",
                    source_id="filing", section="balance", row=0,
                    field="short_term_debt",
                    reason="debt components must be finite nonnegative numeric values"),
                    diag["findings"])
                self.assertTrue(any("invalid component: short_term_debt" in s
                    for s in diag["limitations"]))

    def test_total_debt_diagnostics_name_only_the_unsatisfied_requirements(self):
        from .financial_data import extract_financial_facts

        complete = dict(fiscal_year=2025, short_term_debt=10,
            current_portion_of_long_term_debt=4, long_term_debt=20,
            debt_components_complete=True, current_portion_in_short_term_debt=False)
        cases = []
        for field in ('short_term_debt', 'current_portion_of_long_term_debt', 'long_term_debt'):
            row = {key: value for key, value in complete.items() if key != field}
            cases.append((row, [f'missing component: {field}']))
        for field in ('debt_components_complete', 'current_portion_in_short_term_debt'):
            row = {key: value for key, value in complete.items() if key != field}
            cases.append((row, [f'missing declaration: {field}']))
        cases.extend([
            ({key: value for key, value in complete.items() if key not in
              ('debt_components_complete', 'current_portion_in_short_term_debt')},
             ['missing declaration: debt_components_complete',
              'missing declaration: current_portion_in_short_term_debt']),
            ({**complete, 'short_term_debt': -1}, ['invalid component: short_term_debt']),
            ({**complete, 'debt_components_complete': False},
             ['incomplete declaration: debt_components_complete=false']),
            ({**complete, 'debt_components_complete': 'true'},
             ['invalid declaration: debt_components_complete (expected true)']),
            ({**complete, 'current_portion_in_short_term_debt': 'false'},
             ['invalid declaration: current_portion_in_short_term_debt (expected boolean)']),
            (dict(fiscal_year=2025, debt_components_complete=True,
                  current_portion_in_short_term_debt=False),
             [f'missing component: {field}' for field in
              ('short_term_debt', 'current_portion_of_long_term_debt', 'long_term_debt')]),
        ])
        for row, expected in cases:
            with self.subTest(row=row):
                facts, diagnostic = extract_financial_facts(fundamentals_record(
                    dict(income=[], balance=[row], cash_flow=[])))
                self.assertFalse(any(f.concept == 'TotalDebt' for f in facts))
                reasons = [line.split('TotalDebt unavailable: ', 1)[1]
                           for line in diagnostic['limitations'] if 'TotalDebt unavailable: ' in line]
                self.assertEqual(reasons, ['; '.join(expected)])

    def test_duplicate_source_versions_keep_distinct_facts_and_no_ambiguous_growth(self):
        orch, rid = OrchestratorV4(), "duplicate-financial-source-fixture"
        orch.runs[rid] = RunState(run_id=rid, ticker="TEST", manifest={
            "subject": {"ticker": "TEST"}, "as_of": "2026-10-20"})
        orch.runs[rid].events = {}
        orch.runs[rid].records = [
            fundamentals_record({"income": [
                {"fiscal_year": 2024, "total_revenue": 100},
                {"fiscal_year": 2025, "total_revenue": 110}],
                "balance": [], "cash_flow": []}, "filing-primary"),
            fundamentals_record({"income": [
                {"fiscal_year": 2025, "total_revenue": 120}],
                "balance": [], "cash_flow": []}, "filing-restated"),
        ]
        report = orch.build_facts(rid)
        run = orch.runs[rid]
        revenue_2025 = [fact for fact in run.fact_store.to_dict().values()
                        if fact["concept"] == "Revenue" and fact["period"] == "FY2025"]
        self.assertEqual({fact["value"] for fact in revenue_2025}, {110, 120})
        self.assertEqual(len({fact["fact_id"] for fact in revenue_2025}), 2)
        self.assertIn("fact.Revenue.FY2025", {fact["fact_id"] for fact in revenue_2025})
        self.assertNotIn("calc.rev_growth.FY2025", run.calc_store.to_dict())
        self.assertNotIn("calc.cagr.rev", run.calc_store.to_dict())
        self.assertTrue(any("multiple same-period source rows" in row
                            for row in report["limitations"]))

    def test_duplicate_rows_from_one_source_keep_accession_and_unique_identity(self):
        orch, rid, report = self.build({"income": [
            {"fiscal_year": 2025, "total_revenue": 110, "accession": "A-1"},
            {"fiscal_year": 2025, "total_revenue": 120, "accession": "A-2"}],
            "balance": [], "cash_flow": []})
        facts = [row for row in orch.runs[rid].fact_store.to_dict().values()
                 if row["concept"] == "Revenue" and row["period"] == "FY2025"]
        self.assertEqual({row["value"] for row in facts}, {110, 120})
        self.assertEqual({row["accession_or_version"] for row in facts}, {"A-1", "A-2"})
        self.assertEqual(len({row["fact_id"] for row in facts}), 2)
        self.assertTrue(any("repeats FY2025" in row for row in report["limitations"]))


class CalculationInputContracts(unittest.TestCase):
    def test_known_dimension_mismatch_fails_but_growth_can_compare_periods(self):
        facts = FactStore()
        facts.add(Fact("rev.now", 120, "USD", "FY2025", concept="Revenue",
                       entity="A", basis="gaap", scope="consolidated", source_id="s"))
        facts.add(Fact("rev.old", 100, "USD", "FY2024", concept="Revenue",
                       entity="A", basis="gaap", scope="consolidated", source_id="s"))
        facts.add(Fact("cost", 24, "EUR", "FY2025", concept="OperatingIncome",
                       entity="B", basis="ifrs", scope="consolidated", source_id="s"))
        calcs = CalcStore()
        calcs.add("growth.ok", .2, "growth", ["rev.now", "rev.old"], "RATIO")
        calcs.add("margin.bad", .2, "margin", ["cost", "rev.now"], "RATIO",
                  {"num_basis": "ifrs", "den_basis": "gaap"})
        findings = calculation_findings(SimpleNamespace(fact_store=facts, calc_store=calcs))
        by_id = {row["object_id"]: row for row in findings}
        self.assertFalse(any(row["object_id"] == "growth.ok" and row["status"] == "FAIL"
                             for row in findings))
        self.assertEqual(by_id["margin.bad"]["status"], "FAIL")
        self.assertIn("dimension", by_id["margin.bad"]["reason"])

    def test_close_to_close_return_rejects_mixed_price_bases(self):
        from .pipeline import _calculation_dimensions
        facts = {
            "close.raw": {"unit": "USD", "basis": "raw_price",
                          "entity": "A", "effective_date": "2026-01-02"},
            "close.adjusted": {"unit": "USD", "basis": "price:split_adjusted",
                               "entity": "A", "effective_date": "2026-01-03"},
        }
        mismatch, _ = _calculation_dimensions(
            {"calc_type": "return_cc", "input_refs": ["close.raw", "close.adjusted"]},
            facts, {})
        self.assertIn("basis", mismatch)

    def test_growth_rejects_different_period_shapes_without_blocking_distinct_years(self):
        facts = FactStore()
        facts.add(Fact("rev.now", 120, "USD", "FY2025", concept="Revenue",
                       entity="A", basis="gaap", scope="consolidated",
                       period_type="duration", period_coverage="annual", source_id="s"))
        facts.add(Fact("rev.old", 100, "USD", "FY2024", concept="Revenue",
                       entity="A", basis="gaap", scope="consolidated",
                       period_type="duration", period_coverage="rolling", source_id="s"))
        calcs = CalcStore()
        calcs.add("growth.mismatch", .2, "growth", ["rev.now", "rev.old"], "RATIO")
        findings = calculation_findings(SimpleNamespace(fact_store=facts, calc_store=calcs))
        self.assertTrue(any(row["object_id"] == "growth.mismatch" and row["status"] == "FAIL"
                            and "period_coverage" in row["reason"] for row in findings))

    def test_risk_reward_rejects_incompatible_price_bases(self):
        from .pipeline import _calculation_dimensions
        facts = {
            "entry": {"unit": "USD", "period": "2026-10-20", "period_type": "instant",
                      "period_coverage": "point_in_time", "basis": "raw_price"},
            "stop": {"unit": "USD", "period": "2026-10-20", "period_type": "instant",
                     "period_coverage": "point_in_time", "basis": "raw_price"},
            "target": {"unit": "USD", "period": "2026-10-20", "period_type": "instant",
                       "period_coverage": "point_in_time", "basis": "split_adjusted"},
        }
        mismatch, _ = _calculation_dimensions(
            {"calc_type": "risk_reward", "input_refs": ["entry", "stop", "target"]}, facts, {})
        self.assertIn("basis", mismatch)

    def test_annual_growth_does_not_bridge_a_missing_fiscal_year(self):
        orch, rid = OrchestratorV4(), "missing-financial-year-fixture"
        orch.runs[rid] = RunState(run_id=rid, ticker="TEST", manifest={
            "subject": {"ticker": "TEST"}, "as_of": "2026-10-20"})
        orch.runs[rid].events = {}
        orch.runs[rid].records = [fundamentals_record({"income": [
            {"fiscal_year": 2023, "total_revenue": 100},
            {"fiscal_year": 2025, "total_revenue": 121}],
            "balance": [], "cash_flow": []})]
        orch.build_facts(rid)
        calcs = orch.runs[rid].calc_store.to_dict()
        self.assertNotIn("calc.rev_growth.FY2025", calcs)
        self.assertEqual(calcs["calc.cagr.rev"]["meta"]["years"], 2.0)

    def test_duplicate_cash_flow_sources_keep_separate_fact_and_calc_references(self):
        orch, rid = OrchestratorV4(), "duplicate-cash-flow-fixture"
        orch.runs[rid] = RunState(run_id=rid, ticker="TEST", manifest={
            "subject": {"ticker": "TEST"}, "as_of": "2026-10-20"})
        orch.runs[rid].events = {}
        orch.runs[rid].records = [
            fundamentals_record({"income": [], "balance": [], "cash_flow": [{
                "fiscal_year": 2025, "net_cash_from_operating_activities": 100,
                "purchase_of_plant_property_and_equipment": -20}]}, "cash-primary"),
            fundamentals_record({"income": [], "balance": [], "cash_flow": [{
                "fiscal_year": 2025, "net_cash_from_operating_activities": 110,
                "purchase_of_plant_property_and_equipment": -21}]}, "cash-restated"),
        ]
        orch.build_facts(rid)
        run = orch.runs[rid]
        cash_flow_facts = [row for row in run.fact_store.to_dict().values()
                           if row["concept"] in {"OperatingCashFlow", "Capex", "FreeCashFlow"}]
        self.assertEqual(len(cash_flow_facts), 6)
        self.assertEqual(len({row["fact_id"] for row in cash_flow_facts}), 6)
        for calc in run.calc_store.to_dict().values():
            if calc["calc_type"] == "fcf":
                self.assertTrue(all(ref in run.fact_store.to_dict()
                                    for ref in calc["input_refs"]))
        fcf_calcs = [row for row in run.calc_store.to_dict().values()
                     if row["calc_type"] == "fcf"]
        self.assertEqual(len(fcf_calcs), 2)


class MarketAndFreshnessContracts(unittest.TestCase):
    def test_market_facts_are_order_independent_and_report_raw_price_basis(self):
        bars = [
            {"date": "2026-10-15", "open": 100, "high": 101, "low": 99,
             "close": 100, "adj_close": 50, "volume": 10,
             "week": "2026-W42", "month": "2026-10"},
            {"date": "2026-10-16", "open": 50, "high": 51, "low": 49,
             "close": 50, "adj_close": 50, "volume": 11,
             "week": "2026-W42", "month": "2026-10"},
        ]
        outputs = []
        for source_bars in (bars, list(reversed(bars))):
            orch, rid = OrchestratorV4(), "market-gap-fixture"
            orch.runs[rid] = RunState(run_id=rid, ticker="TEST",
                manifest={"subject": {"ticker": "TEST"}, "as_of": "2026-10-16"})
            orch.runs[rid].events = {}
            orch.runs[rid].records = [SourceRecord("market", "openbb_ohlcv", "TEST", None,
                {"bars": source_bars}, "market-hash", meta={})]
            orch.build_facts(rid)
            run = orch.runs[rid]
            outputs.append((run.fact_store.get("fact.Close.latest").value,
                            run.fact_store.get("fact.Close.latest").effective_date,
                            run.fact_store.get("fact.Close.latest").source_pointer,
                            run.fact_store.get("fact.Close.prev").source_pointer,
                            run.calc_store.get("calc.ReturnD.latest")["value"],
                            run.records[0].meta["market_data_extraction"]["price_basis"]))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0][:4], (50, "2026-10-16",
                         "bars[2026-10-16].close", "bars[2026-10-15].close"))
        self.assertEqual(outputs[0][4], -.5)
        self.assertEqual(outputs[0][5], "raw_close")

    def test_declared_market_basis_is_preserved_without_claiming_conversion(self):
        orch, rid = OrchestratorV4(), "declared-market-basis"
        orch.runs[rid] = RunState(run_id=rid, ticker="TEST", manifest={
            "subject": {"ticker": "TEST"}, "as_of": "2026-10-16",
            "sources": [{"id": "market", "price_basis": "split_adjusted"}]})
        orch.runs[rid].events = {}
        orch.runs[rid].records = [SourceRecord("market", "openbb_ohlcv", "TEST", None,
            {"bars": [
                {"date": "2026-10-15", "open": 100, "high": 101, "low": 99,
                 "close": 100, "volume": 10},
                {"date": "2026-10-16", "open": 50, "high": 51, "low": 49,
                 "close": 50, "volume": 11},
            ]}, "market-hash", meta={})]
        orch.build_facts(rid)
        run = orch.runs[rid]
        diagnostic = run.records[0].meta["market_data_extraction"]
        self.assertEqual(diagnostic["selected_price_field"], "close")
        self.assertEqual(diagnostic["price_basis"], "split_adjusted")
        self.assertEqual(diagnostic["price_basis_source"], "manifest")
        self.assertTrue(any("is preserved" in row for row in diagnostic["limitations"]))
        self.assertEqual(run.fact_store.get("fact.Close.latest").basis,
                         "price:split_adjusted")
        self.assertEqual(run.calc_store.get("calc.ReturnD.latest")["meta"]["price_basis"],
                         "split_adjusted")

    def test_freshness_tolerance_is_applied_only_to_current_data_and_is_not_defaulted(self):
        from .pipeline import freshness_findings
        facts = FactStore()
        facts.add(Fact("fact.Close.latest", 10, "USD", "2020-01-01",
                       concept="Close", source_id="market", effective_date="2020-01-01"))
        facts.add(Fact("fact.Revenue.FY2010", 10, "USD", "FY2010",
                       concept="Revenue", source_id="filing"))
        run = SimpleNamespace(manifest={"as_of": "2026-10-16", "freshness_tolerance_days": 3},
                              fact_store=facts, calc_store=CalcStore())
        rows = freshness_findings(run)
        self.assertTrue(any(row["status"] == "FAIL" and row["check_id"] == "freshness"
                            for row in rows))
        self.assertFalse(any(row.get("object_id") == "fact.Revenue.FY2010"
                             and row["status"] == "FAIL" for row in rows))
        run.manifest.pop("freshness_tolerance_days")
        self.assertTrue(any(row["status"] == "WARN" and row["check_id"] == "freshness_policy"
                            for row in freshness_findings(run)))


class CitationRenderingContracts(unittest.TestCase):
    def test_claim_citations_follow_calculations_and_markdown_links_known_sources(self):
        rendered = render_report({
            "run_id": "report-fixture", "ticker": "TEST", "as_of": "2026-10-20",
            "trade": {}, "claims": [{"claim_id": "analyst.claim", "author_role": "analyst",
                "text": "Revenue increased.", "refs": ["calc.growth"],
                "fact_refs": [], "dependencies": [], "subsection": ""}],
            "facts": {"fact.revenue": {"source_id": "filing", "source_pointer": "income[0].revenue",
                "published_or_filing_time": "2026-03-01", "accession_or_version": "A-1"}},
            "calcs": {"calc.growth": {"input_refs": ["fact.revenue"]}},
            "evidence": {}, "events": {},
            "manifest": {"sources": [{"id": "filing", "url": "https://example.test/filing"}]},
        })
        self.assertIn("[1]", rendered["report_markdown"])
        self.assertIn("https://example.test/filing", rendered["report_markdown"])
        self.assertIn(r"income\[0\]\.revenue", rendered["report_markdown"])
        self.assertEqual(rendered["sidecar"]["claim_citations"]["analyst.claim"], [1])

    def test_noncanonical_fact_source_id_does_not_inherit_manifest_url(self):
        rendered = render_report({
            "run_id": "report-fixture", "ticker": "TEST", "as_of": "2026-10-20",
            "trade": {}, "claims": [{"claim_id": "analyst.claim", "author_role": "analyst",
                "text": "Revenue increased.", "refs": ["fact.revenue"],
                "fact_refs": [], "dependencies": [], "subsection": ""}],
            "facts": {"fact.revenue": {"source_id": " filing ",
                "source_pointer": "income[0].revenue"}},
            "calcs": {}, "evidence": {}, "events": {},
            "manifest": {"sources": [{"id": "filing", "url": "https://example.test/filing"}]},
        })
        self.assertNotIn("https://example.test/filing", rendered["report_markdown"])
        self.assertIn("filing（未提供外部網址）", rendered["report_markdown"])

    def test_duplicate_manifest_source_ids_do_not_inherit_arbitrary_citation_metadata(self):
        rendered = render_report({
            "run_id": "report-fixture", "ticker": "TEST", "as_of": "2026-10-20",
            "trade": {}, "claims": [{"claim_id": "analyst.claim", "author_role": "analyst",
                "text": "Revenue increased.", "refs": ["fact.revenue"],
                "fact_refs": [], "dependencies": [], "subsection": ""}],
            "facts": {"fact.revenue": {"source_id": "filing", "source_pointer": "income[0].revenue"}},
            "calcs": {}, "evidence": {}, "events": {},
            "source_declarations": {"filing": {"url": "https://ambiguous.test/declaration"}},
            "manifest": {"sources": [
                {"id": "filing", "title": "Original filing", "url": "https://first.test/filing"},
                {"id": "filing", "title": "Restatement", "url": "https://second.test/filing"},
            ]},
        })
        report = rendered["report_markdown"]
        for ambiguous_url in ("https://first.test/filing", "https://second.test/filing",
                              "https://ambiguous.test/declaration"):
            self.assertNotIn(ambiguous_url, report)
        self.assertIn("filing（未提供外部網址）", report)
        self.assertIsNone(rendered["sidecar"]["citations"][0]["url"])
        self.assertEqual(rendered["sidecar"]["claim_citations"]["analyst.claim"], [1])

    def test_untrusted_citation_metadata_stays_plain_text_and_bad_types_do_not_crash(self):
        rendered = render_report({
            "run_id": "report-fixture", "ticker": "TEST", "as_of": "2026-10-20",
            "trade": {}, "claims": [{"claim_id": "analyst.claim", "author_role": "analyst",
                "text": "Revenue increased.", "refs": ["fact.revenue"],
                "fact_refs": [], "dependencies": [], "subsection": ""}],
            "facts": {"fact.revenue": {"source_id": "filing", "source_pointer": "income[0].revenue\n# forged heading",
                "published_or_filing_time": "2026-03-01", "accession_or_version": {"bad": "type"}}},
            "calcs": {}, "evidence": {}, "events": {},
            "manifest": {"sources": [{"id": "filing", "type": "<script>alert(1)</script>",
                "title": "![forged](https://evil.test)\n# injected", "url": "javascript:alert(1)"}]},
        })
        report = rendered["report_markdown"]
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("\\!\\[forged\\]", report)
        self.assertNotIn("<script>", report)
        self.assertNotIn("\n# injected", report)
        self.assertNotIn("\n# forged heading", report)


if __name__ == "__main__":
    unittest.main()
