from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import llm_extract
import score_engine


class EvidenceExtractionTrustTests(unittest.TestCase):
    def _raw_file(self, directory: str, **overrides: object) -> Path:
        raw = {
            "final_url": "https://36kr.com/p/test",
            "title": "原始页面标题",
            "platform": "36氪",
            "source_tier": "mainstream_media",
            "text": "测试公司完成B轮融资，融资金额为2亿元。",
            "published_at": "2026-08-20",
            "captured_at": "2026-08-21T10:00:00+08:00",
        }
        raw.update(overrides)
        path = Path(directory) / "raw.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return path

    def test_cli_failure_returns_empty_result_with_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw_path = self._raw_file(directory)
            with patch.object(llm_extract.subprocess, "run", side_effect=FileNotFoundError("claude missing")):
                result = llm_extract.extract_batch([raw_path], "工业科技")

        self.assertEqual(list(result), [])
        self.assertEqual(result.status, "error")
        self.assertEqual(result.diagnostics[0]["error_type"], "FileNotFoundError")
        self.assertEqual(llm_extract.get_last_extraction_diagnostics()[0]["status"], "error")
        self.assertEqual(llm_extract.heuristic_extract({"title": "任意网页"}), [])

    def test_source_provenance_is_forced_and_quote_must_match_raw_text(self) -> None:
        candidate = {
            "entity": "测试公司",
            "claim_type": "funding_signal",
            "stance": "positive",
            "source_tier": "primary_official",
            "importance": 4,
            "source_url": "https://fabricated.example/fake",
            "source_title": "伪造标题",
            "quote": "测试公司完成B轮融资",
            "published_at": "2099-01-01",
            "platform": "伪造平台",
            "tags": ["融资"],
        }
        payload = {"structured_output": {"evidence": [candidate]}}
        with tempfile.TemporaryDirectory() as directory:
            raw_path = self._raw_file(directory)
            completed = SimpleNamespace(stdout=json.dumps(payload, ensure_ascii=False))
            with patch.object(llm_extract.subprocess, "run", return_value=completed):
                result = llm_extract.extract_one(raw_path, "工业科技")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result[0]["source_url"], "https://36kr.com/p/test")
        self.assertEqual(result[0]["source_title"], "原始页面标题")
        self.assertEqual(result[0]["source_tier"], "mainstream_media")
        self.assertEqual(result[0]["platform"], "36氪")
        self.assertEqual(result[0]["published_at"], "2026-08-20")
        self.assertTrue(result[0]["quote_verified"])
        self.assertTrue(result[0]["entity_verified"])

        candidate["quote"] = "原文中不存在的句子"
        payload = {"structured_output": {"evidence": [candidate]}}
        with tempfile.TemporaryDirectory() as directory:
            raw_path = self._raw_file(directory)
            completed = SimpleNamespace(stdout=json.dumps(payload, ensure_ascii=False))
            with patch.object(llm_extract.subprocess, "run", return_value=completed):
                rejected = llm_extract.extract_one(raw_path, "工业科技")

        self.assertEqual(list(rejected), [])
        self.assertEqual(rejected.status, "empty")
        self.assertEqual(rejected.diagnostics[0]["rejected_count"], 1)

        candidate["quote"] = "测试公司完成B轮融资"
        candidate["entity"] = "正文不存在科技"
        payload = {"structured_output": {"evidence": [candidate]}}
        with tempfile.TemporaryDirectory() as directory:
            raw_path = self._raw_file(directory)
            completed = SimpleNamespace(stdout=json.dumps(payload, ensure_ascii=False))
            with patch.object(llm_extract.subprocess, "run", return_value=completed):
                rejected_entity = llm_extract.extract_one(raw_path, "工业科技")

        self.assertEqual(list(rejected_entity), [])
        self.assertEqual(rejected_entity.status, "empty")


class ScoringTrustTests(unittest.TestCase):
    @staticmethod
    def evidence(
        index: int,
        quote: str,
        *,
        entity: str = "测试公司",
        claim_type: str = "product_signal",
        stance: str = "positive",
        published_at: str | None = None,
        event_date: str | None = None,
        captured_at: str | None = None,
    ) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        return {
            "entity": entity,
            "claim_type": claim_type,
            "stance": stance,
            "source_tier": "mainstream_media",
            "importance": 3,
            "source_url": f"https://source{index}.example.com/article",
            "source_title": f"报道{index}",
            "quote": quote,
            "published_at": published_at if published_at is not None else today,
            "event_date": event_date,
            "captured_at": captured_at,
            "platform": "general",
            "tags": [],
            "quote_verified": True,
        }

    def candidate(self, thesis: str, items: list[dict]) -> dict:
        return score_engine.build_report(thesis, [], items)["candidates"][0]

    def test_collection_time_and_future_dates_never_create_recency(self) -> None:
        today = datetime.now(timezone.utc).date()
        old = (today - timedelta(days=365)).isoformat()
        future = (today + timedelta(days=365)).isoformat()
        undated = self.evidence(1, "无发布日期的历史介绍", captured_at=today.isoformat())
        undated["published_at"] = None
        items = [
            undated,
            self.evidence(2, "未来日期异常", published_at=future),
            self.evidence(3, "旧事件在今天被转载", event_date=old, published_at=today.isoformat()),
        ]

        candidate = self.candidate("工业科技", items)
        stats = candidate["diagnostics"]["signal_stats"]

        self.assertEqual(stats["dated_evidence"], 1)
        self.assertEqual(stats["recent_evidence_90d"], 0)
        self.assertEqual(stats["recent_evidence_180d"], 0)
        normalized = candidate["evidence"]
        undated_normalized = next(item for item in normalized if item["quote"] == "无发布日期的历史介绍")
        self.assertIsNone(undated_normalized.get("published_at"))
        self.assertTrue(any(item.get("captured_at") for item in normalized))

    def test_thesis_does_not_change_company_fact_scores(self) -> None:
        items = [self.evidence(i, "该公司发布企业介绍。") for i in range(1, 5)]
        plain = self.candidate("工业科技", items)
        loaded = self.candidate(
            "未上市 B轮 核心算法 国产替代 平台底座 股权结构 估值 3-5年 IPO 产业方",
            items,
        )

        self.assertEqual(plain["total_score"], loaded["total_score"])
        self.assertEqual(plain["priority_score"], loaded["priority_score"])
        self.assertEqual(plain["diagnostics"]["sector_type"], loaded["diagnostics"]["sector_type"])
        self.assertEqual(
            {key: value["raw_level"] for key, value in plain["dimension_details"].items()},
            {key: value["raw_level"] for key, value in loaded["dimension_details"].items()},
        )

    def test_negative_or_failed_events_never_add_positive_momentum(self) -> None:
        items = [
            self.evidence(1, "本轮融资失败，估值下调。", claim_type="funding_signal", stance="neutral"),
            self.evidence(2, "订单取消，客户终止合作。", claim_type="commercial_signal", stance="neutral"),
            self.evidence(3, "量产延期，产品需要召回。", claim_type="product_signal", stance="neutral"),
            self.evidence(4, "尚未实现收入，暂无复购客户。", claim_type="commercial_signal", stance="neutral"),
        ]

        candidate = self.candidate("工业科技", items)

        self.assertEqual(candidate["deal_momentum"]["bonus"], 0.0)
        self.assertEqual(candidate["deal_momentum"]["signals"], [])
        self.assertLessEqual(candidate["dimension_details"]["customer_validation"]["objective_level"], 1)
        self.assertLessEqual(candidate["dimension_details"]["commercialization_progress"]["objective_level"], 1)

    def test_unknown_listing_status_is_not_treated_as_unlisted(self) -> None:
        unknown = self.candidate("工业科技", [self.evidence(1, "该公司发布企业介绍。")])
        self.assertEqual(unknown["diagnostics"]["listed_status"], "未知")
        self.assertEqual(unknown["diagnostics"]["score_cap"], 35.0)
        self.assertIn("一级属性门槛", {item["name"] for item in unknown["gate_details"]})

        unlisted = self.candidate(
            "工业科技",
            [self.evidence(2, "测试公司完成B轮融资。", claim_type="funding_signal")],
        )
        self.assertEqual(unlisted["diagnostics"]["listed_status"], "未上市")
        self.assertNotIn("一级属性门槛", {item["name"] for item in unlisted["gate_details"]})

    def test_irrelevant_evidence_does_not_saturate_confidence_or_raise_priority(self) -> None:
        items = [self.evidence(i, "该公司发布企业介绍。") for i in range(1, 5)]
        candidate = self.candidate("工业科技", items)

        self.assertEqual(candidate["dimension_details"]["customer_validation"]["confidence"], "low")
        self.assertLess(candidate["confidence_score"], 90.0)
        self.assertLessEqual(candidate["priority_score"], candidate["total_score"])

    def test_nonnumeric_commercial_mentions_do_not_bypass_financial_gate(self) -> None:
        items = [
            self.evidence(1, "产品正式发布并开始商业化。", claim_type="commercial_signal"),
            self.evidence(2, "项目已经交付客户。", claim_type="commercial_signal"),
            self.evidence(3, "公司获得订单并完成出货。", claim_type="commercial_signal"),
        ]
        candidate = self.candidate("工业科技", items)

        self.assertEqual(candidate["diagnostics"]["financial_profile"]["estimated_revenue"], "待核实")
        self.assertIn("财务数据门槛", {item["name"] for item in candidate["gate_details"]})


if __name__ == "__main__":
    unittest.main()
