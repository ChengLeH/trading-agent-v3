import unittest
from copy import deepcopy
from v3.pipeline import RunState
from v3.sources import SourceRecord
from .pipeline import OrchestratorV4


class CashFlowContracts(unittest.TestCase):
    def build(self, rows, convention=None):
        orch = OrchestratorV4(); rid = 'fixture'
        payload = dict(income=[], balance=[], cash_flow=rows)
        if convention: payload['cash_flow_capex_convention'] = convention
        record = SourceRecord('source', 'openbb_fundamentals', 'TEST', None, payload, 'original-hash')
        original = deepcopy(payload)
        orch.runs[rid] = RunState(run_id=rid, ticker='TEST', manifest={'as_of': '2026-10-20'}, records=[record])
        result = orch.build_facts(rid)
        self.assertEqual(record.payload, original)
        self.assertEqual(record.source_hash, 'original-hash')
        return orch, rid, result

    def test_aliases_and_repurchases_keep_actual_pointer_and_sign(self):
        orch,rid,report = self.build([dict(fiscal_year=2025,operating_cash_flow=100,capex=20,share_repurchases=-10)], 'positive_outflow')
        facts = orch.runs[rid].fact_store.to_dict()
        self.assertEqual(facts['fact.FreeCashFlow.FY2025']['value'],80)
        self.assertEqual(facts['fact.Capex.FY2025']['value'],-20)
        self.assertEqual(facts['fact.Capex.FY2025']['source_pointer'],'cash_flow[0].capex')
        self.assertEqual(facts['fact.ShareRepurchases.FY2025']['value'],-10)
        self.assertEqual(report['cash_flow_coverage'][0]['produced_facts'],4)

    def test_nonempty_unsupported_section_is_visible(self):
        _,_,report = self.build([dict(fiscal_year=2025,unmapped_cash=10)])
        self.assertTrue(any('no supported fields' in line for line in report['limitations']))

    def test_zero_values_are_facts_and_cashflow_years_do_not_need_balance(self):
        orch,rid,_ = self.build([dict(fiscal_year=2022,net_cash_from_operating_activities=0,purchase_of_plant_property_and_equipment=0)])
        self.assertEqual(orch.runs[rid].fact_store.get('fact.FreeCashFlow.FY2022').value,0)

    def test_rebuild_removes_old_resources_when_source_rows_are_removed(self):
        orch,rid,_ = self.build([dict(fiscal_year=2025,operating_cash_flow=100,capex=-20)])
        orch.runs[rid].records[0].payload['cash_flow'] = []
        orch.build_facts(rid)
        self.assertNotIn('fact.FreeCashFlow.FY2025',orch.runs[rid].fact_store.to_dict())
        self.assertNotIn('calc.fcf.FY2025',orch.runs[rid].calc_store.to_dict())

    def test_ambiguous_sign_and_conflicting_aliases_fail_before_build(self):
        for row in [dict(fiscal_year=2025,operating_cash_flow=100,capex=20),
                    dict(fiscal_year=2025,operating_cash_flow=100,net_cash_from_operating_activities=99)]:
            with self.subTest(row=row),self.assertRaises(ValueError): self.build([row])
