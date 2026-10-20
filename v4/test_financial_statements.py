import unittest
from copy import deepcopy
from types import SimpleNamespace

from v3.facts import Fact, FactStore, CalcStore
from v3.sources import SourceRecord
from .financial_statements import (augment_financial_statements,
                                   extract_financial_statements,
                                   financial_statement_findings)


def record(payload, source_id="source-a"):
    # Fully specified fixtures declare where the redeemable amount is presented.
    payload.setdefault('financial_statement_metadata', payload.get('financial_metadata', {})).setdefault('redeemable_nci_inclusion', 'separate')
    return SourceRecord(source_id, "openbb_fundamentals", "ZZZ", None,
                        payload, "hash-preserved")


def run_for(rows, company="ZZZ"):
    rec = record({"income": [], "balance": rows,
                  "financial_statement_metadata": {
                      "unit": "USD", "basis": "gaap", "entity": company,
                      "precision": 1}})
    facts = FactStore()
    # Model the untouched legacy facts, including the ambiguous Equity label.
    for concept, value in (("Assets", 100), ("Liabilities", 20), ("Equity", 80)):
        facts.add(Fact(f"legacy.{concept}", value, "USD", "FY2030", fiscal_year=2030,
                       fiscal_period="FY", concept=concept, basis="gaap",
                       scope="consolidated", source_id=rec.id, source_hash=rec.source_hash))
    return SimpleNamespace(records=[rec], fact_store=facts, calc_store=CalcStore())


class FinancialStatementTests(unittest.TestCase):
    def test_parent_equity_identity_handles_negative_nci_zero_redeemable_and_scale(self):
        cases = [
            ("A", 2028, 10_000_000, 2_000_000, -100_000, 0),
            ("B", 2031, 0.25, 0.10, -0.02, 0),
        ]
        for company, year, liabilities, parent, nci, redeemable in cases:
            with self.subTest(company=company, year=year):
                assets = liabilities + parent + nci + redeemable
                fields = [
                    ("redeemable_nci", redeemable),
                    ("nonredeemable_nci", nci),
                    ("parent_equity", parent),
                ]
                balance = [{"fiscal_year": year, "total_assets": assets,
                    "total_liabilities": liabilities, **dict(fields)}]
                rec = record({"income": [], "balance": balance,
                    "financial_statement_metadata": {"unit": "USD", "basis": "gaap",
                    "entity": company, "precision": 0.01}}, f"src-{company}")
                run = SimpleNamespace(records=[rec], fact_store=FactStore())
                run.fact_store.add(Fact(f"a-{company}", assets, "USD", f"FY{year}",
                    fiscal_year=year, fiscal_period="FY", concept="Assets", basis="gaap",
                    scope="consolidated", source_id=rec.id, source_hash=rec.source_hash, entity=company))
                run.fact_store.add(Fact(f"l-{company}", liabilities, "USD", f"FY{year}",
                    fiscal_year=year, fiscal_period="FY", concept="Liabilities", basis="gaap",
                    scope="consolidated", source_id=rec.id, source_hash=rec.source_hash, entity=company))
                new, diagnostic = extract_financial_statements(rec)
                rec.meta["financial_statement_extraction"] = diagnostic
                for fact in new:
                    run.fact_store.add(fact)
                matches = [f for f in financial_statement_findings(run)
                           if f["check_id"] == "balance_sheet_identity"]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["status"], "PASS")

    def test_redeemable_zero_is_complete_but_missing_is_not_zero(self):
        complete = record({"income": [], "balance": [{"fiscal_year": 2032,
            "parent_equity": 50, "non_redeemable_nci": 10, "redeemable_nci": 0}],
            "financial_statement_metadata": {"unit": "USD", "basis": "gaap"}})
        facts, diagnostic = extract_financial_statements(complete)
        complete.meta["financial_statement_extraction"] = diagnostic
        self.assertEqual({f.concept for f in facts},
                         {"ParentEquity", "NonRedeemableNCI", "RedeemableNCI"})
        missing = record({"income": [], "balance": [{"fiscal_year": 2032,
            "parent_equity": 50, "non_redeemable_nci": 10}],
            "financial_statement_metadata": {"unit": "USD", "basis": "gaap"}}, "src-missing")
        run = SimpleNamespace(records=[missing], fact_store=FactStore())
        for concept, val in (("Assets", 60), ("Liabilities", 0)):
            run.fact_store.add(Fact(f"{concept}", val, "USD", "FY2032", concept=concept,
                basis="gaap", scope="consolidated", source_id=missing.id))
        missing_facts, diagnostic = extract_financial_statements(missing)
        missing.meta["financial_statement_extraction"] = diagnostic
        for fact in missing_facts:
            run.fact_store.add(fact)
        findings = financial_statement_findings(run)
        self.assertTrue(any(f["check_id"] == "balance_sheet_identity_coverage"
                            and f["status"] == "WARN" for f in findings))

    def test_consolidated_attribution_and_mismatch(self):
        rec = record({"income": [], "balance": [{"fiscal_year": 2033,
            "consolidated_equity": 70, "redeemable_nci": 10}],
            "financial_statement_metadata": {"unit": "USD", "basis": "gaap"}})
        facts, diagnostic = extract_financial_statements(rec)
        rec.meta["financial_statement_extraction"] = diagnostic
        run = SimpleNamespace(records=[rec], fact_store=FactStore())
        for concept, value in (("Assets", 101), ("Liabilities", 20)):
            run.fact_store.add(Fact(concept, value, "USD", "FY2033", concept=concept,
                basis="gaap", scope="consolidated", source_id=rec.id))
        for fact in facts:
            run.fact_store.add(fact)
        finding = next(f for f in financial_statement_findings(run)
                       if f["check_id"] == "balance_sheet_identity")
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["delta"], 1)

    def test_decimal_identity_uses_exact_decimal_arithmetic_without_default_tolerance(self):
        rec = record({"income": [], "balance": [{"fiscal_year": 2036,
            "consolidated_equity": 0.2, "redeemable_nci": 0.1}],
            "financial_metadata": {"unit": "USD", "basis": "gaap"}}, "decimal-source")
        facts, diagnostic = extract_financial_statements(rec)
        rec.meta["financial_statement_extraction"] = diagnostic
        run = SimpleNamespace(records=[rec], fact_store=FactStore())
        for concept, value in (("Assets", 0.6), ("Liabilities", 0.3)):
            run.fact_store.add(Fact(f"{concept}-decimal", value, "USD", "FY2036",
                concept=concept, basis="gaap", scope="consolidated", source_id=rec.id,
                source_pointer=f"{concept}[0]"))
        for fact in facts:
            run.fact_store.add(fact)
        finding = next(f for f in financial_statement_findings(run)
                       if f["check_id"] == "balance_sheet_identity")
        self.assertEqual(finding["tolerance"], 0)
        self.assertEqual(finding["delta"], 0)
        self.assertEqual(finding["status"], "PASS")

    def test_legacy_total_equity_stays_unattributed_and_unknown_fields_visible(self):
        rec = record({"income": [{"fiscal_year": 2034, "total_revenue": 9,
                                  "vendor_special_total": 7}],
            "balance": [{"fiscal_year": 2034, "total_equity": 12}],
            "financial_statement_metadata": {"unit": "USD", "basis": "gaap"}})
        facts, diagnostic = extract_financial_statements(rec)
        self.assertEqual(facts, [])
        self.assertTrue(any(f["check_id"] == "equity_attribution_unknown"
                            for f in diagnostic["findings"]))
        unmapped = next(f for f in diagnostic["findings"]
                        if f["check_id"] == "financial_fields_unmapped")
        self.assertEqual(unmapped["fields"], ["vendor_special_total"])

    def test_declared_parent_attribution_and_precision(self):
        rec = record({"income": [], "balance": [{"fiscal_year": 2035,
            "total_equity": 30, "equity_attribution": "parent",
            "nonredeemable_nci": 5, "redeemable_nci": 0}],
            "financial_statement_metadata": {"unit": "USD", "basis": "gaap", "precision": 1}})
        facts, diagnostic = extract_financial_statements(rec)
        rec.meta["financial_statement_extraction"] = diagnostic
        self.assertIn("ParentEquity", {f.concept for f in facts})
        run = SimpleNamespace(records=[rec], fact_store=FactStore())
        for concept, value in (("Assets", 36), ("Liabilities", 0)):
            run.fact_store.add(Fact(f"{concept}-rounded", value, "USD", "FY2035",
                concept=concept, basis="gaap", scope="consolidated", source_id=rec.id,
                source_pointer="total_assets[0]"))
        for fact in facts:
            run.fact_store.add(fact)
        finding = next(f for f in financial_statement_findings(run)
                       if f["check_id"] == "balance_sheet_identity")
        self.assertEqual(finding["tolerance"], 2.5)
        self.assertEqual(finding["status"], "PASS")

    def test_augmentation_preserves_legacy_fact_and_source_hash(self):
        run = run_for([{"fiscal_year": 2030, "parent_equity": 50,
                        "nonredeemable_nci": 20, "redeemable_nci": 0}])
        old = run.fact_store.get("legacy.Equity")
        payload_before = deepcopy(run.records[0].payload)
        report = {"facts": 0, "calcs": 0, "limitations": []}
        augment_financial_statements(run, report)
        self.assertEqual(run.fact_store.get("legacy.Equity").to_dict(), old.to_dict())
        self.assertEqual(run.records[0].source_hash, "hash-preserved")
        self.assertEqual(run.records[0].payload, payload_before)
        self.assertTrue(report["financial_statement_coverage"])
        first_report_count = report["facts"]
        first_store_count = len(run.fact_store.to_dict())
        augment_financial_statements(run, report)
        self.assertEqual(report["facts"], first_report_count)
        self.assertEqual(len(run.fact_store.to_dict()), first_store_count)


if __name__ == "__main__":
    unittest.main()


class PresentationAndRounding(unittest.TestCase):
    def scenario(self, inclusion='separate', assets=110, consolidated=60, parent=50, precision=0):
        rec=record(dict(income=[],balance=[dict(fiscal_year=2040,parent_equity=parent,
            nonredeemable_nci=10,redeemable_nci=10,consolidated_equity=consolidated)],
            financial_statement_metadata=dict(unit='EUR',basis='ifrs',entity='FICTION',precision=precision,
                redeemable_nci_inclusion=inclusion)))
        facts,diag=extract_financial_statements(rec)
        rec.meta['financial_statement_extraction']=diag
        run=SimpleNamespace(records=[rec],fact_store=FactStore())
        for concept,value in [('Assets',assets),('Liabilities',40)]:
            run.fact_store.add(Fact(concept,value,'EUR','FY2040',concept=concept,basis='ifrs',scope='consolidated',
                entity='FICTION',source_id=rec.id,source_pointer='total_assets[0]' if concept=='Assets' else 'total_liabilities[0]'))
        for fact in facts:run.fact_store.add(fact)
        return run

    def test_explicit_redeemable_classification_avoids_double_counting(self):
        for inclusion,assets,consolidated in [('separate',110,60),('liabilities',100,60),('consolidated_equity',110,70)]:
            with self.subTest(inclusion=inclusion):
                rows=financial_statement_findings(self.scenario(inclusion,assets,consolidated))
                checks=[r for r in rows if r['check_id'] in {'balance_sheet_identity','equity_attribution_identity'}]
                self.assertEqual(len(checks),2)
                self.assertTrue(all(r['status']=='PASS' for r in checks),checks)

    def test_unspecified_classification_stays_visible_and_parallel_equity_must_reconcile(self):
        rows=financial_statement_findings(self.scenario(None))
        self.assertTrue(any(r['check_id']=='redeemable_nci_inclusion_unknown' and r['status']=='WARN' for r in rows))
        self.assertFalse(any(r['check_id']=='balance_sheet_identity' for r in rows))
        rows=financial_statement_findings(self.scenario(parent=49))
        self.assertTrue(any(r['check_id']=='equity_attribution_identity' and r['status']=='FAIL' for r in rows))
        self.assertTrue(any(r['check_id']=='balance_sheet_identity' and r['status']=='PASS' for r in rows))

    def test_source_rounding_bounds_accumulate_but_do_not_hide_larger_difference(self):
        for assets,status in [(112,'PASS'),(113,'FAIL')]:
            rows=financial_statement_findings(self.scenario(assets=assets,precision=1))
            match=next(r for r in rows if r['check_id']=='balance_sheet_identity')
            self.assertEqual(match['tolerance'],2)
            self.assertEqual(match['status'],status)

    def test_mixed_source_errors_and_valid_financial_records_do_not_crash(self):
        from v3.sources import SourceError
        run=self.scenario();run.records.append(SourceError('unsupported','fixture','bad-source'))
        self.assertTrue(financial_statement_findings(run))


class DistinctSourcePrecision(unittest.TestCase):
    def test_identity_uses_each_components_own_source_row_rounding_bound(self):
        for assets,status in [(100,'PASS'),(110,'FAIL')]:
            run=SimpleNamespace(records=[],fact_store=FactStore())
            data=[('total_assets','Assets',assets,10),('total_liabilities','Liabilities',35,1),
                  ('parent_equity','ParentEquity',50,0.1),('nonredeemable_nci','NonRedeemableNCI',5,0.01),
                  ('redeemable_nci','RedeemableNCI',5,0.001)]
            for index,(field,concept,value,precision) in enumerate(data):
                rec=record(dict(income=[],balance=[dict(fiscal_year=2041,**{field:value})],
                    financial_statement_metadata=dict(unit='EUR',basis='ifrs',entity='FICTION',precision=precision,
                        redeemable_nci_inclusion='separate')),f'independent-source-{index}')
                facts,diag=extract_financial_statements(rec)
                rec.meta['financial_statement_extraction']=diag;run.records.append(rec)
                if concept in {'Assets','Liabilities'}:
                    facts=[Fact(concept,value,'EUR','FY2041',concept=concept,basis='ifrs',scope='consolidated',
                        entity='FICTION',source_id=rec.id,source_pointer=f'balance[0].{field}')]
                for fact in facts:run.fact_store.add(fact)
            rows=financial_statement_findings(run)
            result=next(r for r in rows if r['check_id']=='balance_sheet_identity')
            self.assertEqual(result['tolerance'],5.5555)
            self.assertEqual(result['status'],status)
