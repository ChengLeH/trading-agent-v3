import copy
import unittest
from v4.decision_comparison import validate_comparison
from v4.trade_evidence import trade_evidence_refs, validate_trade_evidence
from v4.test_integration import comparable_wait

class DecisionComparisonTests(unittest.TestCase):
    def test_wait_allows_unknown_without_inventing_trades(self):
        validate_trade_evidence(comparable_wait(), set(), require_comparison=True)

    def test_missing_comparison_and_missing_direction_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'decision_comparison required'):
            validate_trade_evidence({'direction': 'WAIT'}, set(), require_comparison=True)
        trade = comparable_wait()
        trade['decision_comparison']['alternatives'].pop()
        with self.assertRaisesRegex(ValueError, 'BUY and one SHORT'):
            validate_comparison(trade, set(), required=True)

    def test_references_resolve_without_guessing_from_prose(self):
        trade = comparable_wait()
        trade['decision_comparison']['alternatives'][0]['support_refs'] = ['claim.one']
        self.assertEqual(trade_evidence_refs(trade), {'claim.one'})
        with self.assertRaisesRegex(ValueError, 'unknown refs'):
            validate_trade_evidence(trade, set(), require_comparison=True)
        validate_trade_evidence(trade, {'claim.one'}, require_comparison=True)

    def test_legacy_wait_is_accepted_without_pretending_it_has_comparison(self):
        validate_trade_evidence({'direction': 'WAIT'}, set())

    def test_prose_is_not_a_deterministic_veto(self):
        trade = comparable_wait()
        trade['decision_comparison']['wait']['reason'] = 'anything; no keyword changes a semantic verdict'
        validate_trade_evidence(trade, set(), require_comparison=True)
