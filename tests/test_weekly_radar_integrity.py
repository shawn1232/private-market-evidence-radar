from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import weekly_radar
from wechat_source_pool import WeChatSourcePool


class WeeklyRadarIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = weekly_radar._load_config()

    def test_discovery_query_tags_do_not_become_investment_fit(self) -> None:
        record = {
            "discovery_only": True,
            "fit_tags": ["投早", "投硬科技", "国产替代"],
        }
        self.assertEqual(weekly_radar._fit_tags("一家企业完成融资", record, self.config), [])

    def test_fit_is_inferred_from_observed_title_text(self) -> None:
        record = {
            "discovery_only": True,
            "discovery_context_tags": ["投早", "投硬科技"],
        }
        tags = weekly_radar._fit_tags("某半导体公司完成客户认证", record, self.config)
        self.assertIn("半导体与光电", tags)
        self.assertNotIn("投早", tags)

    def test_duration_before_event_verb_is_not_part_of_company_name(self) -> None:
        title = "“智元系”发力具身智能数据平台基建，觅蜂科技半年完成三轮融资"
        self.assertEqual(weekly_radar._extract_company(title, self.config), "觅蜂科技")

    def test_regional_prefix_aliases_do_not_create_duplicate_projects(self) -> None:
        config = {
            "window_days": 7,
            "max_candidates": 5,
            "investment_profile": {"principles": ["投早"]},
            "core_variables": [{"name": "资本与产业资源到位", "keywords": ["融资"]}],
            "confirmed_event_patterns": {"资本与产业资源到位": ["完成.{0,16}融资"]},
            "sector_keywords": {"半导体与光电": ["半导体"]},
            "entity_stopwords": [],
        }
        records = [
            {
                "company": "鉴芯半导体",
                "title": "鉴芯半导体完成天使轮融资",
                "url": "https://example.org/1",
                "published_at": "2026-08-19",
                "source_type": "google_news_rss",
                "discovery_only": True,
            },
            {
                "company": "常州鉴芯半导体",
                "title": "常州鉴芯半导体完成天使轮融资",
                "url": "https://example.net/2",
                "published_at": "2026-08-19",
                "source_type": "google_news_rss",
                "discovery_only": True,
            },
        ]
        report = weekly_radar._build_report(records, "2026-08-21", config, {})
        self.assertEqual(len(report["candidates"]), 1)
        self.assertCountEqual(
            report["candidates"][0]["company_aliases"],
            ["鉴芯半导体", "常州鉴芯半导体"],
        )

    def test_late_stage_financing_is_excluded_but_angel_round_is_kept(self) -> None:
        config = {
            "window_days": 7,
            "max_candidates": 5,
            "investment_profile": {"principles": ["投早"]},
            "core_variables": [{"name": "资本与产业资源到位", "keywords": ["融资"]}],
            "confirmed_event_patterns": {"资本与产业资源到位": ["完成.{0,16}融资"]},
            "sector_keywords": {"半导体与光电": ["半导体"]},
            "entity_stopwords": [],
            "excluded_late_stage_patterns": ["(?:C\\+?轮|D轮|Pre[- ]?IPO)"],
        }
        records = [
            {
                "company": "早期半导体",
                "title": "早期半导体完成天使轮融资",
                "url": "https://example.org/angel",
                "published_at": "2026-08-19",
                "source_type": "google_news_rss",
                "discovery_only": True,
            },
            {
                "company": "晚期半导体",
                "title": "晚期半导体完成C+轮融资",
                "url": "https://example.org/cplus",
                "published_at": "2026-08-19",
                "source_type": "google_news_rss",
                "discovery_only": True,
            },
        ]
        report = weekly_radar._build_report(records, "2026-08-21", config, {})
        self.assertEqual([item["company"] for item in report["candidates"]], ["早期半导体"])

    def test_wechat_pool_cached_body_is_collected_as_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "wechat_pool"
            body = root / "bodies" / "article.md"
            body.parent.mkdir(parents=True)
            body.write_text("# 原文\n\n测试半导体完成天使轮融资。", encoding="utf-8")
            pool = WeChatSourcePool(root / "pool.sqlite3")
            pool.add_urls(
                [
                    {
                        "url": "https://mp.weixin.qq.com/s/pool-exact",
                        "title": "测试半导体完成天使轮融资",
                        "publish_time": "2026-08-20",
                        "body_markdown_path": str(body),
                        "fetch_mode": "body_export",
                    }
                ],
                account_name="硬科技前沿",
            )
            missing_legacy = Path(directory) / "missing_urls.txt"
            with (
                patch.object(weekly_radar, "WeChatSourcePool", return_value=pool),
                patch.object(weekly_radar, "WECHAT_POOL_ROOT", root),
                patch.object(weekly_radar, "WECHAT_URLS_PATH", missing_legacy),
            ):
                records, status = weekly_radar._collect_wechat(self.config)

        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["discovery_only"])
        self.assertEqual(records[0]["source_type"], "wechat_pool_body")
        self.assertEqual(status["pool_total"], 1)
        self.assertEqual(status["in_window"], 1)


if __name__ == "__main__":
    unittest.main()
