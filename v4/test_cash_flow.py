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
        orch.runs[rid].events = {}
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

    def test_reported_metrics_and_arbitrary_source_adjustments_do_not_replace_generic_fcf(self):
        row = dict(fiscal_year=2030, operating_cash_flow=100, capex=-80,
                   finance_lease_principal_payments=-7, reported_free_cash_flow=13,
                   special_cash_outflow=-4, issuer_metric=9,
                   metric_definitions={
                       'ReportedFreeCashFlow': dict(reported_field='reported_free_cash_flow',
                           definition='OCF less equipment purchases and lease principal.',
                           component_fields=['operating_cash_flow', 'capex', 'finance_lease_principal_payments'],
                           basis='issuer_non_gaap'),
                       'IssuerResidualCash': dict(reported_field='issuer_metric',
                           definition='Reported cash metric less the explicitly disclosed special outflow.',
                           component_fields=['reported_free_cash_flow', 'special_cash_outflow'])})
        orch, rid, _ = self.build([row])
        run = orch.runs[rid]
        self.assertEqual(run.fact_store.get('fact.FreeCashFlow.FY2030').value, 20)
        self.assertEqual(run.fact_store.get('fact.ReportedFreeCashFlow.FY2030').value, 13)
        self.assertEqual(run.fact_store.get('fact.ReportedFreeCashFlow.FY2030').basis, 'issuer_non_gaap')
        self.assertEqual(run.fact_store.get('fact.FinanceLeasePrincipalPayments.FY2030').value, -7)
        self.assertEqual(run.fact_store.get('fact.IssuerResidualCash.FY2030').value, 9)
        diagnostic = run.records[0].meta['cash_flow_extraction']
        contexts = diagnostic['metric_definitions']
        declared = next(c for c in contexts if c['concept'] == 'IssuerResidualCash')
        self.assertEqual(declared['components'][1]['value'], -4)
        self.assertEqual(declared['components'][1]['source_pointer'], 'cash_flow[0].special_cash_outflow')
        from .reporting import render_report
        sidecar = render_report(orch.snapshot(rid))['sidecar']
        self.assertTrue(any(d.get('metric_definitions') == contexts
                            for d in sidecar['ingestion_diagnostics']))
        # Removing a declaration/row field must also remove its old custom Fact.
        run.records[0].payload['cash_flow'][0].pop('issuer_metric')
        run.records[0].payload['cash_flow'][0]['metric_definitions'].pop('IssuerResidualCash')
        orch.build_facts(rid)
        self.assertNotIn('fact.IssuerResidualCash.FY2030', run.fact_store.to_dict())

    def test_lease_amount_is_not_automatically_subtracted_or_missing_values_filled(self):
        for lease in (None, 0, -7):
            row = dict(fiscal_year=2031, operating_cash_flow=100, capex=-87,
                       reported_free_cash_flow=13, metric_definitions={
                           'ReportedFreeCashFlow': dict(reported_field='reported_free_cash_flow',
                               definition='Capex already includes lease principal; do not deduct again.',
                               component_fields=['operating_cash_flow', 'capex', 'finance_lease_principal_payments'])})
            if lease is not None:
                row['finance_lease_principal_payments'] = lease
            orch, rid, _ = self.build([row])
            run = orch.runs[rid]
            self.assertEqual(run.fact_store.get('fact.FreeCashFlow.FY2031').value, 13)
            self.assertEqual(run.fact_store.get('fact.ReportedFreeCashFlow.FY2031').value, 13)
            self.assertEqual('fact.FinanceLeasePrincipalPayments.FY2031' in run.fact_store.to_dict(), lease is not None)
            context = run.records[0].meta['cash_flow_extraction']['metric_definitions'][0]
            self.assertEqual(context['components'][-1]['present'], lease is not None)

    def test_metric_definitions_survive_checkpoint_and_reach_atomic_source_context(self):
        import tempfile
        from pathlib import Path
        from .checkpoint import save_checkpoint, load_checkpoint
        from .atomic_phase6 import build_atomic_tasks
        from .test_integration import seeded
        runner = seeded()
        record = SourceRecord('disclosure', 'openbb_fundamentals', 'TEST', None, dict(
            income=[], balance=[], cash_flow=[dict(fiscal_year=2032, operating_cash_flow=100,
                capex=-80, source_adjustment=-7, issuer_cash=13)],
            cash_flow_metric_definitions={'IssuerCash': dict(reported_field='issuer_cash',
                definition='The source defines its metric using the listed cells.',
                component_fields=['operating_cash_flow', 'capex', 'source_adjustment'])}), 'source-hash')
        runner.run.records = [record]
        runner.orch.build_facts(runner.run_id)
        ref = 'fact.IssuerCash.FY2032'
        runner.run.claims[0]['refs'] = [ref]
        runner.run.claims[0]['fact_refs'] = [ref]
        fact = runner.run.fact_store.get(ref)
        runner.run.claims[0]['bindings'] = [dict(ref_id=ref, **{key: getattr(fact, key) or 'unknown'
            for key in ('entity', 'concept', 'period', 'scope', 'unit', 'basis')})]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'checkpoint.json'
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
        task = next(t for t in build_atomic_tasks(restored.run) if t.claim_id == 'a')
        diagnostics = task.context['evidence_scope']['ingestion_diagnostics']['findings']
        source = next(f['finding'] for f in diagnostics
                      if f['collection'] == 'source_context' and f['section'] == 'cash_flow')
        self.assertEqual(source['metric_definitions'][0]['components'][-1]['value'], -7)
        self.assertEqual(task.resolved_refs[ref]['value']['basis'], 'unknown')
        self.assertEqual(record.payload['cash_flow'][0]['source_adjustment'], -7)

    def test_row_metric_declaration_overrides_source_and_keeps_independent_dimensions(self):
        row = dict(fiscal_year=2034, unit='EUR thousands', basis='GAAP',
                   source_value=11, row_value=17, other_value=13, source_component=3, row_component=4,
                   metric_definitions={
                       'IssuerCash': dict(reported_field='row_value', definition='Row definition.',
                           component_fields=['row_component'], basis='row_basis', unit='USD millions'),
                       'OtherCash': dict(reported_field='other_value', definition='Another disclosed measure.')})
        orch, rid, _ = self.build([row])
        record = orch.runs[rid].records[0]
        shared = dict(IssuerCash=dict(reported_field='source_value', definition='Source definition.',
                       component_fields=['source_component'], basis='source_basis', unit='USD thousands'))
        record.payload['cash_flow_metric_definitions'] = deepcopy(shared)
        orch.build_facts(rid)
        store = orch.runs[rid].fact_store
        metric = store.get('fact.IssuerCash.FY2034')
        self.assertEqual((metric.value, metric.source_pointer, metric.basis, metric.unit),
                         (17, 'cash_flow[0].row_value', 'row_basis', 'USD millions'))
        other = store.get('fact.OtherCash.FY2034')
        self.assertEqual((other.value, other.basis, other.unit), (13, 'unknown', 'EUR thousands'))
        context = next(c for c in record.meta['cash_flow_extraction']['metric_definitions']
                       if c['concept'] == 'IssuerCash')
        self.assertEqual(context['declaration']['definition'], 'Row definition.')
        self.assertEqual(context['components'], [dict(field='row_component',
            source_pointer='cash_flow[0].row_component', present=True, value=4)])
        self.assertEqual(record.payload['cash_flow_metric_definitions'], shared)

    def test_derived_fcf_inherits_compatible_metric_input_dimensions(self):
        row = dict(fiscal_year=2032, unit='USD thousands', basis='GAAP',
                   operating_cash_flow=100, capex=-20, metric_definitions={
                       concept: dict(reported_field=field, definition='Explicit source measure.',
                                     unit='USD millions', basis='issuer_basis')
                       for concept, field in (('OperatingCashFlow', 'operating_cash_flow'),
                                              ('Capex', 'capex'))})
        orch, rid, _ = self.build([row])
        store = orch.runs[rid].fact_store
        for concept in ('OperatingCashFlow', 'Capex', 'FreeCashFlow'):
            fact = store.get(f'fact.{concept}.FY2032')
            self.assertEqual(fact.unit, 'USD millions')
            self.assertEqual(fact.basis, 'issuer_basis')
        self.assertEqual(store.get('fact.FreeCashFlow.FY2032').value, 80)
        self.assertEqual(orch.runs[rid].calc_store.get('calc.fcf.FY2032')['unit'], 'USD millions')

    def test_metric_declaration_order_does_not_change_extraction_or_diagnostic_hash(self):
        from .cash_flow import extract_cash_flow
        from .schema import digest

        declarations = {
            'IssuerCashA': dict(reported_field='cash_a', definition='First disclosed measure.'),
            'IssuerCashB': dict(reported_field='cash_b', definition='Second disclosed measure.')}
        results = []
        for definitions in (declarations, dict(reversed(list(declarations.items())))):
            record = SourceRecord('source', 'openbb_fundamentals', 'TEST', None,
                dict(cash_flow=[dict(fiscal_year=2032, operating_cash_flow=100, capex=-20,
                                    cash_a=12, cash_b=18, metric_definitions=definitions)]), 'same-hash')
            facts, calcs, diagnostic = extract_cash_flow(record)
            results.append(digest(dict(facts=[f.to_dict() for f in facts],
                                       calcs=calcs, diagnostic=diagnostic)))
        self.assertEqual(results[0], results[1])

    def test_invalid_or_conflicting_metric_declarations_do_not_guess_a_field(self):
        base = dict(fiscal_year=2033, operating_cash_flow=100, capex=-20,
                    free_cash_flow=80, reported_free_cash_flow=70)
        with self.assertRaisesRegex(ValueError, 'conflicting aliases'):
            self.build([base])
        for definitions in ([1], {7: {}, 'IssuerCash': {}}, {'FreeCashFlow': dict(reported_field='issuer', definition='override')},
                            {'IssuerCash': dict(definition='missing field')},
                            {'IssuerCash': dict(reported_field='issuer', definition='duplicate',
                                                component_fields=['x', 'x'])}):
            with self.subTest(definitions=definitions), self.assertRaises(ValueError):
                self.build([dict(fiscal_year=2033, operating_cash_flow=100, capex=-20,
                                 metric_definitions=definitions)])
