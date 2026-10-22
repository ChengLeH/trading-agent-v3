"""The existing review calls carry the added semantic scope obligations."""
import unittest
from .financial_review import FINANCIAL_REVIEW_CONTRACT, FINANCIAL_REVIEW_VERSION


class FinancialScope(unittest.TestCase):
    def test_contract_requires_horizon_bridge_and_executable_exit_assumptions(self):
        for obligation in ('預測期間與交易持有期間', '時間橋接', '停損觸發價不是保證成交價',
                           '滑價', '流動性', '歷史樣本不是未來最大損失的硬上限'):
            self.assertIn(obligation, FINANCIAL_REVIEW_CONTRACT)

    def test_contract_requires_current_report_coverage_without_banning_history(self):
        for obligation in ('截至 as_of 已發布', '年度與期中', '單季、累計 YTD、滾動 TTM',
                           '歷史分析仍可使用舊報表', 'received', 'structured'):
            self.assertIn(obligation, FINANCIAL_REVIEW_CONTRACT)

    def test_existing_role_and_review_prompts_share_new_contract(self):
        from .runner import ROLE_CONTRACT
        from .atomic_phase6 import build_atomic_prompt
        from .test_integration import seeded
        runner = seeded()
        self.assertIn(FINANCIAL_REVIEW_CONTRACT, ROLE_CONTRACT)
        self.assertIn(FINANCIAL_REVIEW_CONTRACT, build_atomic_prompt(runner._tasks()['a']))
        self.assertIn(FINANCIAL_REVIEW_VERSION, FINANCIAL_REVIEW_CONTRACT)


if __name__ == '__main__':
    unittest.main()
