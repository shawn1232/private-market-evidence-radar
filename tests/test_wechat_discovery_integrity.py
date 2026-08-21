from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_search
import score_engine
import weekly_radar
from wechat_source_pool import WeChatSourcePool


class WechatDiscoveryIntegrityTests(unittest.TestCase):
    def test_search_summary_is_discovery_only_and_never_enters_scoring(self) -> None:
        discovery = {
            "entity": "测试科技",
            "claim_type": "commercial_signal",
            "stance": "positive",
            "source_tier": "mainstream_media",
            "importance": 5,
            "source_url": "https://mp.weixin.qq.com/s/discovery-only",
            "source_title": "搜索结果标题",
            "quote": "搜索引擎摘要，不是已读取正文",
            "published_at": "2026-08-20",
            "discovery_only": True,
            "evidence_eligible": False,
            "quote_verified": False,
            "platform": "微信公众号",
            "tags": [],
        }

        report = score_engine.build_report("工业科技", [], [discovery])

        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["total_evidence"], 0)
        self.assertEqual(report["ignored_unverified_or_unassigned_evidence"], 1)

    def test_same_article_from_multiple_queries_is_one_retrieved_source(self) -> None:
        url = "https://mp.weixin.qq.com/s/same-article"
        records = [
            {
                "source_url": url,
                "title": "同一篇文章",
                "provider": "brave",
                "query": "查询一",
                "discovery_source_pack": "wechat",
            },
            {
                "source_url": url,
                "title": "同一篇文章",
                "provider": "serper",
                "query": "查询二",
                "discovery_source_pack": "wechat",
            },
        ]

        deduped = auto_search.dedupe_source_records(records)

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["retrieval_provider_count"], 2)
        self.assertEqual(set(deduped[0]["query_hits"]), {"查询一", "查询二"})
        self.assertEqual(deduped[0]["independent_source_count"], 1)
        self.assertFalse(deduped[0]["independently_corroborated"])
        self.assertFalse(deduped[0]["cross_validated"])

    def test_pool_ready_state_requires_body_and_real_publish_time(self) -> None:
        url = "https://mp.weixin.qq.com/s/state-contract"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "wechat_pool"
            pool = WeChatSourcePool(root / "pool.sqlite3")

            pool.add_urls(
                [
                    {
                        "url": url,
                        "title": "搜索发现标题",
                        "fetch_mode": "web_discovery",
                        "discovery_query": "site:mp.weixin.qq.com 工业机器人",
                        "discovery_provider": "brave",
                        "discovered_at": "2026-08-21T08:00:00+08:00",
                    }
                ],
                account_name="公众号A",
                dedupe_globally=True,
            )
            discovered_stats = pool.get_stats()
            self.assertEqual(discovered_stats["ready"], 0)
            self.assertEqual(discovered_stats["pending"], 1)
            self.assertEqual(
                pool.records_for_window("2026-08-15", "2026-08-21", scope="ready"),
                [],
            )

            body = root / "bodies" / "run-1" / "articles" / "001-body.md"
            body.parent.mkdir(parents=True)
            body.write_text("# 正文标题\n\n这是已下载的公众号正文，发布日期仍待从文章元数据确认。", encoding="utf-8")
            pool.add_urls(
                [
                    {
                        "url": url,
                        "title": "正文标题",
                        "body_markdown_path": str(body),
                        "fetch_mode": "body_export",
                        "credential_status": "not_required",
                    }
                ],
                account_name="公众号A",
                dedupe_globally=True,
            )
            body_only_stats = pool.get_stats()
            self.assertEqual(body_only_stats["ready"], 0)
            self.assertEqual(body_only_stats["pending"], 1)
            with patch.object(weekly_radar, "WECHAT_POOL_ROOT", root):
                self.assertIsNone(weekly_radar._pool_row_to_radar_record(pool.list_articles()[0]))

            pool.add_urls(
                [
                    {
                        "url": url,
                        "title": "正文标题",
                        "publish_time": "2026-08-20T09:30:00+08:00",
                        "body_markdown_path": str(body),
                        "fetch_mode": "known_url",
                        "credential_status": "not_required",
                    }
                ],
                account_name="公众号A",
                dedupe_globally=True,
            )

            ready = pool.records_for_window("2026-08-15", "2026-08-21", scope="ready")
            self.assertEqual(len(ready), 1)
            self.assertEqual(pool.get_stats()["stored_article_rows"], 1)
            with patch.object(weekly_radar, "WECHAT_POOL_ROOT", root):
                radar_record = weekly_radar._pool_row_to_radar_record(ready[0])

        self.assertIsNotNone(radar_record)
        assert radar_record is not None
        self.assertFalse(radar_record["discovery_only"])
        self.assertEqual(radar_record["source_type"], "wechat_pool_body")
        self.assertEqual(radar_record["verification_status"], "已读取原文，关键事实待核实")


if __name__ == "__main__":
    unittest.main()
