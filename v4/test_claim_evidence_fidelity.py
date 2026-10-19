"""Contract regression: authored Claims and P6 repairs must use complete source evidence.

These tests assert instructions and pipeline handoff, not a brittle keyword
validator for generated financial prose. Semantic correctness remains reviewed.
"""
import unittest

from .test_integration import seeded, review_all


class EvidenceFidelityContract(unittest.TestCase):
    def test_role_generation_requires_balanced_source_attribution_and_provenance(self):
        from .runner import PipelineRunnerV4

        runner = seeded()
        prompts = dict(runner._role_prompts(("bull", "market_analyst")))
        for prompt in prompts.values():
            self.assertIn("反向或不利證據", prompt)
            self.assertIn("不得選擇性引用", prompt)
            self.assertIn("轉述與原始統計", prompt)
            self.assertIn("統計截止日", prompt)
            self.assertIn("極值發生日", prompt)
            self.assertIn("bindings", prompt)

    def test_p6_revision_prompt_requests_full_evidence_correction_not_ref_removal(self):
        runner = seeded()
        review_all(runner)
        result = runner.prepare_phase6_revision_v4([
            dict(claim_id="a", issue="Source contradicts conclusion",
                 evidence={"support": "positive", "dissent": "nonzero"},
                 suggested_fix="Acknowledge disagreement")])
        self.assertIn("revision_prompts", result)
        _, prompt = result["revision_prompts"][0]
        self.assertIn("不得選擇性引用", prompt)
        self.assertIn("相反證據", prompt)
        self.assertIn("不得只刪除引用", prompt)
        self.assertIn("bindings.period", prompt)
        self.assertIn("Source contradicts conclusion", prompt)
        self.assertEqual(result["allowed_claim_ids"], ["a"])


if __name__ == "__main__":
    unittest.main()
