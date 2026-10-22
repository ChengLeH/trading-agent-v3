"""Typed trade premises are distinct from semantic proof and execution guarantees."""
import unittest
from copy import deepcopy

from .test_integration import seeded


def plan():
    return dict(direction='BUY', entry=100, stop=90, target=120, position_pct=10,
        evidence_contract='trade-evidence/1',
        target_basis=dict(use='trade_target', source_refs=['target.source'], bridge_refs=[]),
        source_horizon=dict(start='2030-01-01', end='2031-01-01'),
        trade_valid_until='2030-01-09',
        execution_assumption=dict(order_type='stop_market', assumed_fill_price=90,
                                  fill_guaranteed=False),
        planned_stop_loss=dict(value=1, unit='portfolio_pct'),
        gap_stress_loss=dict(value=3, unit='portfolio_pct', scenario_return_pct=-30),
        maximum_loss_bound=None)


class TradeEvidence(unittest.TestCase):
    def test_same_response_upstream_claim_can_supply_a_reviewable_bridge(self):
        from .test_contracts import claim
        r = seeded()
        r.run.claims = [c for c in r.run.claims if c['author_role'] not in {'trader', 'risk_manager'}]
        r.run.analyses = {}
        r.orch._sync_views(r.run)
        trade = plan()
        trade['target_basis'] = dict(use='trade_target', source_refs=[], bridge_refs=['trader.bridge'])
        bridge = claim('trader.bridge'); bridge['author_role'] = 'trader'
        c = claim('trader.plan', ['trader.bridge']); c['author_role'] = 'trader'
        c['assertion'] = dict(kind='trade_plan', trade=trade)
        r.collect_trader([bridge, c], trade=trade)
        self.assertEqual(r.run.trade, trade)
        self.assertIn('trader.plan', {c['claim_id'] for c in r.run.claims})
        self.assertFalse(r.receipts)  # Accepting a premise is not semantic verification.

    def test_declared_loss_rounding_is_supported_without_global_tolerance(self):
        trade = plan(); trade.update(entry=91.69, stop=84, source_horizon=None)
        trade['target_basis'] = dict(use='unknown', source_refs=[], bridge_refs=[])
        trade['execution_assumption']['assumed_fill_price'] = 84
        trade['planned_stop_loss'] = dict(value=.84, unit='portfolio_pct',
            output_rounding=dict(decimal_places=2, rounding='half_up'))
        r = seeded(); r.orch.register_trade(r.run_id, trade)
        self.assertEqual(r.run.trade['planned_stop_loss']['value'], .84)

    def test_new_role_output_cannot_omit_trade_evidence_contract(self):
        trade = dict(direction='BUY', entry=100, stop=90, target=120, position_pct=10)
        r = seeded()
        with self.assertRaisesRegex(ValueError, 'evidence_contract'):
            r.collect_trader([], trade=trade)

    def test_bridge_is_reviewable_only_when_declared_by_trade_claim(self):
        from .pipeline import validate_trade_claim
        from .test_contracts import claim
        trade = plan(); trade['target_basis']['bridge_refs'] = ['upstream']
        c = claim('trader.plan'); c['author_role'] = 'trader'
        c['assertion'] = dict(kind='trade_plan', trade=trade)
        with self.assertRaisesRegex(ValueError, 'declared'):
            validate_trade_claim([c], trade)

    def test_long_target_requires_declared_bridge_without_rewriting_prices(self):
        r = seeded(); before = deepcopy(r.run.trade)
        with self.assertRaisesRegex(ValueError, 'bridge_refs'):
            r.orch.register_trade(r.run_id, plan())
        self.assertEqual(r.run.trade, before)

    def test_source_and_bridge_refs_must_resolve_but_are_not_semantic_passes(self):
        r = seeded(); trade = plan()
        trade['target_basis']['bridge_refs'] = ['invented']
        with self.assertRaisesRegex(ValueError, 'unknown.*ref'):
            r.orch.register_trade(r.run_id, trade)

    def test_historical_scenario_cannot_certify_a_hard_bound(self):
        r = seeded(); trade = plan()
        trade['source_horizon'] = None
        trade['target_basis'] = dict(use='valuation_context', source_refs=[], bridge_refs=[])
        trade['maximum_loss_bound'] = dict(value=3, unit='portfolio_pct', proof_refs=[])
        with self.assertRaisesRegex(ValueError, 'proof_refs'):
            r.orch.register_trade(r.run_id, trade)

    def test_stop_guarantee_cannot_be_inferred_from_an_assumed_fill(self):
        r = seeded(); trade = plan()
        trade['source_horizon'] = None
        trade['target_basis'] = dict(use='valuation_context', source_refs=[], bridge_refs=[])
        trade['execution_assumption']['fill_guaranteed'] = True
        with self.assertRaisesRegex(ValueError, 'guarantee_refs'):
            r.orch.register_trade(r.run_id, trade)

    def test_unknown_scope_preserved_for_semantic_review_and_wait_exempt(self):
        r = seeded(); trade = plan()
        trade['source_horizon'] = None
        trade['target_basis'] = dict(use='unknown', source_refs=[], bridge_refs=[])
        r.orch.register_trade(r.run_id, trade)
        self.assertIsNone(r.run.trade['maximum_loss_bound'])
        self.assertEqual(r.run.trade['planned_stop_loss']['value'], 1)
        r.orch.register_trade(r.run_id, dict(direction='WAIT'))
        self.assertEqual(r.run.trade, dict(direction='WAIT'))

    def test_new_contract_requires_fields_and_valid_dates(self):
        for key in ('target_basis', 'source_horizon', 'trade_valid_until', 'execution_assumption'):
            r = seeded(); trade = plan(); del trade[key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                r.orch.register_trade(r.run_id, trade)
        trade = plan(); trade['trade_valid_until'] = 'tomorrow'
        with self.assertRaisesRegex(ValueError, 'trade_valid_until'):
            seeded().orch.register_trade('fixture', trade)

    def test_risk_loss_magnitudes_are_not_interchangeable(self):
        trade = plan(); trade['source_horizon'] = None
        trade['target_basis'] = dict(use='unknown', source_refs=[], bridge_refs=[])
        trade['planned_stop_loss']['value'] = 3  # 1% under this declared fill
        with self.assertRaisesRegex(ValueError, 'planned_stop_loss'):
            seeded().orch.register_trade('fixture', trade)


if __name__ == '__main__':
    unittest.main()
