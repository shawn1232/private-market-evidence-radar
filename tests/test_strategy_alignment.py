from __future__ import annotations

import unittest

import score_engine


def evidence(
    *,
    entity: str = "测试科技有限公司",
    url: str = "https://example.org/a",
    claim_type: str = "product_signal",
    provider_count: int = 1,
    independently_corroborated: bool = False,
    independent_source_count: int = 1,
) -> dict:
    quote = "测试科技完成产品验证。"
    return {
        "entity": entity,
        "claim_type": claim_type,
        "stance": "positive",
        "source_url": url,
        "source_title": "测试科技完成产品验证",
        "quote": quote,
        "quote_verified": True,
        "published_at": "2026-08-20",
        "source_tier": "T2",
        "importance": 3,
        "provider_count": provider_count,
        "retrieval_provider_count": provider_count,
        "provider_count_kind": "retrieval_channels",
        "independently_corroborated": independently_corroborated,
        "independent_source_count": independent_source_count,
        "tags": [],
    }


class StrategyAlignmentTests(unittest.TestCase):
    def test_early_stage_is_preferred_over_b_round(self) -> None:
        self.assertGreater(score_engine.infer_stage_fit_level("天使轮"), score_engine.infer_stage_fit_level("B轮"))
        self.assertGreater(score_engine.infer_stage_fit_level("Pre-A"), score_engine.infer_stage_fit_level("C轮"))

    def test_multiple_search_engines_are_not_cross_validation(self) -> None:
        company = {
            "evidence": [score_engine.normalize_evidence_item(evidence(provider_count=3))]
        }
        stats = score_engine.collect_evidence_stats(company)
        self.assertEqual(stats["multi_provider_retrieval_count"], 1)
        self.assertEqual(stats["independently_corroborated_count"], 0)
        self.assertEqual(stats["cross_validation_score"], 0.0)

    def test_explicit_independent_source_confirmation_is_counted(self) -> None:
        company = {
            "evidence": [
                score_engine.normalize_evidence_item(
                    evidence(independently_corroborated=True, independent_source_count=2)
                )
            ]
        }
        stats = score_engine.collect_evidence_stats(company)
        self.assertEqual(stats["independently_corroborated_count"], 1)
        self.assertGreater(stats["cross_validation_score"], 0.0)

    def test_legal_suffix_aliases_merge_and_unassigned_leads_do_not_become_companies(self) -> None:
        first = evidence(entity="视觉智能科技有限公司", url="https://example.org/a")
        second = evidence(entity="视觉智能科技", url="https://example.net/b")
        unassigned = evidence(entity="", url="https://example.com/c")
        report = score_engine.build_report("工业AI", [], [first, second, unassigned])
        self.assertEqual(report["total_candidates"], 1)
        self.assertEqual(report["total_evidence"], 2)
        self.assertEqual(report["ignored_unverified_or_unassigned_evidence"], 1)
        self.assertCountEqual(
            report["candidates"][0]["entity_aliases"],
            ["视觉智能科技有限公司", "视觉智能科技"],
        )

    def test_discovery_only_record_never_enters_scoring(self) -> None:
        item = evidence()
        item.update(discovery_only=True, evidence_eligible=False, quote_verified=False)
        report = score_engine.build_report("工业AI", [], [item])
        self.assertEqual(report["total_candidates"], 0)
        self.assertEqual(report["total_evidence"], 0)


if __name__ == "__main__":
    unittest.main()
