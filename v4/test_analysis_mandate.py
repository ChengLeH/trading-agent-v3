import json
import subprocess
import sys
import unittest
from types import SimpleNamespace

from v4.analysis_mandate import normalize_mandate, plan_data
from v4.atomic_phase6 import build_atomic_tasks
from v4.review_context import scoped_review_state
from v4.test_atomic_phase6 import make_run


class AnalysisMandateTests(unittest.TestCase):
    def test_windows_are_independent_and_calendar_clamped(self):
        plan = plan_data('2024-08-31', lookback_months=6, horizon_months=3)
        self.assertEqual(plan['analysis_mandate']['history_start'], '2024-02-29')
        self.assertEqual(plan['analysis_mandate']['analysis_end'], '2024-11-30')
        self.assertIsNone(plan_data('2024-08-31', horizon_months=3)['analysis_mandate']['history_start'])
        self.assertIsNone(plan_data('2024-08-31', lookback_months=6)['analysis_mandate']['analysis_end'])
        self.assertIsNone(plan_data('2024-08-31')['analysis_mandate']['history_start'])

    def test_invalid_and_backwards_dates_rejected(self):
        for data in ({'history_start': '2025-01-01'}, {'analysis_end': '2023-01-01'}, {'unknown': 1}):
            with self.assertRaises(ValueError):
                normalize_mandate(data, '2024-01-01')
        for count in (0, -1, True):
            with self.assertRaises(ValueError):
                plan_data('2024-01-01', lookback_months=count)

    def test_cli_outputs_plan_without_fetching(self):
        p = subprocess.run([sys.executable, '-m', 'v4.agent_cli', 'plan-data', '--as-of', '2024-08-31', '--lookback-months', '6'], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(p.stdout)['market_history']['start_inclusive'], '2024-02-29')

    def test_review_scope_keeps_mandate(self):
        state = {'claims': [], 'analysis_mandate': {'analysis_end': '2025-01-01'}}
        self.assertEqual(scoped_review_state(state, [])['analysis_mandate'], state['analysis_mandate'])

    def test_mandate_changes_review_hash_and_appears_in_report(self):
        from v4.test_integration import seeded
        from v4.reporting import render_report
        runner = seeded()
        old_hash = runner._hash()
        runner.run.manifest['analysis_mandate'] = {'history_start': '2026-04-16', 'analysis_end': '2027-01-16'}
        self.assertNotEqual(old_hash, runner._hash())
        rendered = render_report(runner.orch.snapshot(runner.run_id))
        self.assertEqual(rendered['sidecar']['analysis_mandate'], runner.run.manifest['analysis_mandate'])
        self.assertIn('2027-01-16', rendered['report_markdown'])

    def test_atomic_tasks_carry_mandate(self):
        claim = {'claim_id': 'bull.1', 'author_role': 'bull', 'text': 'x', 'claim_type': 'interpretation', 'refs': [], 'fact_refs': [], 'dependencies': [], 'bindings': [], 'assertion': {'kind': 'interpretation'}}
        run = make_run([claim])
        run.manifest = {'as_of': '2024-01-01', 'analysis_mandate': {'analysis_end': '2024-04-01'}}
        run.events = {}
        self.assertEqual(build_atomic_tasks(run)[0].context['analysis_mandate'], run.manifest['analysis_mandate'])


if __name__ == '__main__':
    unittest.main()
