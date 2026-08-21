from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import weekly_radar


class WeeklyRadarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "window_days": 7,
            "max_candidates": 5,
            "investment_profile": {"principles": ["投早", "投硬科技"]},
            "core_variables": [
                {"name": "客户验证", "keywords": ["中标", "订单", "认证"]},
                {"name": "规模化与交付能力", "keywords": ["量产", "交付"]},
                {"name": "资本与产业资源到位", "keywords": ["融资"]},
            ],
            "sector_keywords": {"半导体与光电": ["芯片", "光电"]},
            "entity_stopwords": ["中国", "行业", "公司", "项目"],
        }
        self.as_of = date(2026, 7, 14)

    @staticmethod
    def record(company: str, published_at: object, index: int, keyword: str = "量产") -> dict:
        return {
            "company": company,
            "title": f"{company}芯片{keyword}取得新进展{index}",
            "url": f"https://news.example.com/{index}",
            "published_at": published_at,
            "publisher": "测试来源",
            "source_type": "google_news_rss",
            "discovery_only": True,
            "fit_tags": ["投硬科技"],
        }

    def build(self, records: list[dict]) -> dict:
        return weekly_radar._build_report(records, self.as_of, self.config, {})

    def test_seven_day_window_is_inclusive_on_both_boundaries(self) -> None:
        records = [
            self.record("起点光电", "2026-07-08", 1),
            self.record("终点光电", "2026-07-14", 2),
            self.record("过早光电", "2026-07-07", 3),
        ]
        report = self.build(records)

        self.assertEqual(report["window"]["start_date"], "2026-07-08")
        self.assertEqual(report["window"]["end_date"], "2026-07-14")
        self.assertEqual({item["company"] for item in report["candidates"]}, {"起点光电", "终点光电"})
        self.assertTrue(
            all(item["date_basis"] == "以文章发布日期代替，待核实事件日" for item in report["candidates"])
        )

    def test_explicit_event_date_has_clear_date_basis_and_precedence(self) -> None:
        record = self.record("实证光电", "2026-07-30", 1)
        record["event_date"] = "2026-07-12"

        report = self.build([record])

        self.assertEqual(report["candidates"][0]["event_date"], "2026-07-12")
        self.assertEqual(report["candidates"][0]["date_basis"], "事件发生日")

    def test_future_event_is_excluded(self) -> None:
        report = self.build([self.record("未来光电", "2026-07-15", 1)])

        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["empty_slots"], 5)

    def test_same_company_is_strictly_deduplicated(self) -> None:
        records = [
            self.record("星辰科技有限公司", "2026-07-10", 1, "融资"),
            self.record("星辰科技股份有限公司", "2026-07-13", 2, "量产"),
        ]
        report = self.build(records)

        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["candidates"][0]["event_date"], "2026-07-13")

    def test_report_never_exceeds_five_candidates(self) -> None:
        companies = ["甲芯科技", "乙芯科技", "丙芯科技", "丁芯科技", "戊芯科技", "己芯科技", "庚芯科技"]
        report = self.build(
            [self.record(company, f"2026-07-{8 + index:02d}", index) for index, company in enumerate(companies)]
        )

        self.assertEqual(len(report["candidates"]), 5)
        self.assertEqual(report["empty_slots"], 0)
        self.assertTrue(all("score" not in item and "total_score" not in item for item in report["candidates"]))

    def test_insufficient_candidates_are_not_padded(self) -> None:
        report = self.build(
            [
                self.record("甲光电", "2026-07-12", 1),
                self.record("乙光电", "2026-07-13", 2),
            ]
        )

        self.assertEqual(len(report["candidates"]), 2)
        self.assertEqual(report["empty_slots"], 3)
        self.assertTrue(all(item["source"]["evidence_level"] == "discovery_only" for item in report["candidates"]))
        self.assertTrue(all(item["verification_status"] == "待核实" for item in report["candidates"]))

    def test_captured_at_can_never_create_an_event_date(self) -> None:
        only_captured = self.record("抓取时点科技", None, 1)
        only_captured.pop("published_at")
        only_captured["captured_at"] = "2026-07-13T12:00:00+08:00"
        old_event = self.record("旧闻科技", "2026-07-01", 2)
        old_event["captured_at"] = "2026-07-13T12:00:00+08:00"

        report = self.build([only_captured, old_event])

        self.assertEqual(report["candidates"], [])
        self.assertIsNone(weekly_radar._extract_event_date(only_captured))

    def test_failed_refresh_does_not_overwrite_last_successful_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "weekly_radar.json"
            previous = self.build([self.record("上一成功科技", "2026-07-13", 1)])
            output_path.write_text(json.dumps(previous, ensure_ascii=False), encoding="utf-8")
            before = output_path.read_bytes()
            failure = ([], {"status": "error", "errors": [{"error": "offline"}]})

            with (
                patch.object(weekly_radar, "OUTPUT_PATH", output_path),
                patch.object(weekly_radar, "_load_config", return_value=self.config),
                patch.object(weekly_radar, "_collect_google_news", return_value=failure),
                patch.object(weekly_radar, "_collect_wechat", return_value=failure),
            ):
                result = weekly_radar.refresh_report(self.as_of)

            self.assertEqual(result["status"], "stale_cache")
            self.assertEqual(output_path.read_bytes(), before)

    def test_degraded_empty_refresh_keeps_previous_nonempty_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "weekly_radar.json"
            previous = self.build([self.record("上一次成功科技", "2026-07-13", 1)])
            output_path.write_text(json.dumps(previous, ensure_ascii=False), encoding="utf-8")
            before = output_path.read_bytes()
            partial_empty = (
                [],
                {
                    "status": "partial",
                    "queries_total": 5,
                    "queries_succeeded": 1,
                    "items_seen": 0,
                    "errors": [{"error": "proxy reset"}],
                },
            )
            skipped = ([], {"status": "skipped", "items_seen": 0, "errors": []})

            with (
                patch.object(weekly_radar, "OUTPUT_PATH", output_path),
                patch.object(weekly_radar, "_load_config", return_value=self.config),
                patch.object(weekly_radar, "_collect_google_news", return_value=partial_empty),
                patch.object(weekly_radar, "_collect_wechat", return_value=skipped),
            ):
                result = weekly_radar.refresh_report(self.as_of)

            self.assertEqual(result["status"], "stale_cache")
            self.assertEqual(len(result["candidates"]), 1)
            self.assertEqual(output_path.read_bytes(), before)

    def test_add_wechat_url_returns_ui_friendly_ok_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "wechat_urls.txt"
            with patch.object(weekly_radar, "WECHAT_URLS_PATH", input_path):
                added = weekly_radar.add_wechat_url("https://mp.weixin.qq.com/s/example#fragment")
                exists = weekly_radar.add_wechat_url("https://mp.weixin.qq.com/s/example")
                invalid = weekly_radar.add_wechat_url("https://example.com/article")

            self.assertTrue(added["ok"])
            self.assertEqual(added["status"], "added")
            self.assertTrue(exists["ok"])
            self.assertEqual(exists["status"], "exists")
            self.assertFalse(invalid["ok"])
            self.assertEqual(invalid["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
