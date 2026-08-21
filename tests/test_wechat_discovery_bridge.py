from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from app import radar_app
import weekly_radar
from wechat_source_pool import WeChatSourcePool


DISCOVERED_URL = "https://mp.weixin.qq.com/s/unfollowed-new-source"


class FakeSearchBackend:
    provider = "exa"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: str, *, limit: int):
        self.calls += 1
        return [
            {
                "title": "未关注公众号发现的新项目",
                "url": DISCOVERED_URL,
                "author": "未关注的新公众号",
                "summary": "搜索摘要声称完成融资，但这里只能作为发现线索。",
            }
        ]


class FakeHtmlResponse:
    def __init__(self, html: str) -> None:
        self.text = html
        self.content = html.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"
        self.url = "https://down.mptext.top/api/public/v1/download"

    def raise_for_status(self) -> None:
        return None


class WechatDiscoveryBridgeTests(unittest.TestCase):
    def test_wechat_lead_seeds_dated_open_web_followup_without_becoming_evidence(self) -> None:
        config = weekly_radar._load_config()
        seed = {
            "title": "巽霖科技完成近亿元A轮融资并启动玻璃基板量产",
            "url": DISCOVERED_URL,
            "account_name": "陌生公众号",
            "discovered_at": "2026-08-21T02:00:00Z",
            "discovery_rank": 1,
        }
        dated_news = {
            "title": "巽霖科技完成近亿元A轮融资",
            "url": "https://news.google.com/rss/articles/example",
            "published_at": "Thu, 20 Aug 2026 02:00:00 GMT",
            "publisher": "产业媒体",
            "source_type": "google_news_rss",
            "discovery_only": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(weekly_radar, "WECHAT_FOLLOWUP_CACHE_PATH", Path(directory) / "followup.json"),
                patch.object(weekly_radar, "_fetch_google_query", return_value=[dated_news]) as fetch,
            ):
                records, status = weekly_radar._collect_wechat_followup_news(
                    config,
                    [seed],
                    as_of=weekly_radar.date(2026, 8, 21),
                )

        self.assertEqual(status["status"], "ok")
        self.assertEqual(len(records), 1)
        self.assertIn("巽霖科技", fetch.call_args.args[0]["query"])
        self.assertEqual(records[0]["published_at"], dated_news["published_at"])
        self.assertEqual(records[0]["wechat_seed_url"], DISCOVERED_URL)
        self.assertEqual(records[0]["discovery_origin"], "wechat_pool_followup")
        self.assertTrue(records[0]["discovery_only"])

    def test_discovery_persists_only_auditable_non_evidence_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool = WeChatSourcePool(root / "pool.sqlite3")
            backend = FakeSearchBackend()
            with patch.object(weekly_radar, "WECHAT_DISCOVERY_STATE_PATH", root / "state.json"):
                result = weekly_radar.discover_wechat_sources(
                    force=True,
                    pool=pool,
                    search_backend=backend,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["pool_write"]["added"], 1)
            self.assertGreaterEqual(backend.calls, 1)
            rows = pool.list_articles(scope="discovered")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["account_name"], "未关注的新公众号")
            self.assertEqual(row["fetch_mode"], "web_discovery")
            self.assertEqual(row["discovery_provider"], "exa")
            self.assertTrue(row["discovery_query"])
            self.assertIsNone(row["publish_time"])
            self.assertEqual(row["body_markdown_path"], "")
            self.assertEqual(row["digest"], "")

    def test_collector_fetches_new_discovery_and_only_ready_body_enters_radar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "wechat_pool"
            pool = WeChatSourcePool(root / "pool.sqlite3")

            def discover(*_args, **kwargs):
                kwargs["pool"].add_urls(
                    [
                        {
                            "url": DISCOVERED_URL,
                            "title": "搜索发现标题",
                            "account_name": "未关注的新公众号",
                            "fetch_mode": "web_discovery",
                            "discovery_query": "site:mp.weixin.qq.com/s 机器人 融资",
                            "discovery_provider": "exa",
                            "discovered_at": "2026-08-21T00:00:00Z",
                        }
                    ],
                    account_name="",
                    dedupe_globally=True,
                )
                return {"ok": True, "status": "ok", "stats": {"unique_results": 1}}

            fetched = {
                "title": "拓源科技完成天使轮融资",
                "url": DISCOVERED_URL,
                "published_at": "2026-08-20",
                "publisher": "未关注的新公众号",
                "publisher_url": DISCOVERED_URL,
                "summary": "拓源科技宣布完成天使轮融资，资金用于机器人产品量产和客户交付。",
                "body_text": "拓源科技宣布完成天使轮融资，资金用于机器人产品量产和客户交付。" * 8,
                "source_type": "wechat_exact_url",
                "discovery_only": False,
                "verification_status": "已读取原文，关键事实待核实",
                "fit_tags": [],
            }
            config = {"window_days": 7, "request_timeout_seconds": 2, "wechat_exact_fetch_limit_per_refresh": 20}
            with (
                patch.object(weekly_radar, "WeChatSourcePool", return_value=pool),
                patch.object(weekly_radar, "WECHAT_POOL_ROOT", root),
                patch.object(weekly_radar, "_read_wechat_urls", return_value=[]),
                patch.object(weekly_radar, "discover_wechat_sources", side_effect=discover),
                patch.object(weekly_radar, "_fetch_wechat_url", return_value=fetched),
            ):
                records, status = weekly_radar._collect_wechat(config, as_of="2026-08-21")

            self.assertEqual(status["urls_succeeded"], 1)
            self.assertEqual(status["discovery"]["stats"]["unique_results"], 1)
            self.assertEqual(len(records), 1)
            self.assertFalse(records[0]["discovery_only"])
            ready = pool.records_for_window("2026-08-15", "2026-08-21", scope="ready")
            self.assertEqual(len(ready), 1)
            self.assertTrue(ready[0]["discovered_at"])

    def test_public_exporter_body_without_real_date_stays_discovery_only(self) -> None:
        html = """
        <html><body>
          <h1 id="activity-name">公开回退正文</h1>
          <span id="js_author_name_text">陌生公众号</span>
          <div id="js_content">这是公开回退取得的完整公众号正文，但页面没有真实发布日期，因此绝不能进入七日评分。文章还包含项目产品、客户、融资用途、研发进度、生产计划与团队情况等公开信息；这些正文可以留在待核验文章池中，只有将来取得原始发布日期后才能进入七日窗口。</div>
        </body></html>
        """
        fallback = FakeHtmlResponse(html)
        with patch.object(
            weekly_radar.requests,
            "get",
            side_effect=requests.RequestException("direct blocked"),
        ) as direct_only:
            with self.assertRaisesRegex(ValueError, "第三方公开正文回退默认关闭"):
                weekly_radar._fetch_wechat_url(DISCOVERED_URL, 5)
        self.assertEqual(direct_only.call_count, 1)

        with (
            patch.dict("os.environ", {"DEALSCOPE_ALLOW_PUBLIC_WECHAT_FALLBACK": "1"}),
            patch.object(
                weekly_radar.requests,
                "get",
                side_effect=[requests.RequestException("direct blocked"), fallback],
            ) as mocked_get,
        ):
            record = weekly_radar._fetch_wechat_url(DISCOVERED_URL, 5)

        self.assertEqual(mocked_get.call_count, 2)
        self.assertEqual(record["source_type"], "wechat_public_exporter_body")
        self.assertIsNone(record["published_at"])
        self.assertTrue(record["discovery_only"])
        self.assertIn("真实发布日期待补", record["verification_status"])

    def test_discovery_api_returns_pool_stats_without_exposing_search_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_pool = radar_app._wechat_pool
            radar_app._wechat_pool = WeChatSourcePool(Path(directory) / "pool.sqlite3")
            radar_app.app.config.update(TESTING=True)
            client = radar_app.app.test_client()
            try:
                mocked = {
                    "ok": True,
                    "status": "ok",
                    "message": "全网发现完成：新增 2 篇公众号线索。",
                    "stats": {"unique_results": 2},
                    "pool_write": {"added": 2, "exists": 0, "errors": 0},
                    "errors": [{"error": "internal transport detail"}],
                }
                with patch.object(radar_app, "discover_wechat_sources", return_value=mocked):
                    response = client.post("/api/wechat/discover", json={})
                payload = response.get_json()
            finally:
                radar_app._wechat_pool = previous_pool

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["discovery_stats"]["unique_results"], 2)
        self.assertNotIn("errors", payload)
        self.assertNotIn("internal transport detail", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
