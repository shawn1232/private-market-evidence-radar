from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from app import radar_app
import weekly_radar


class PublicLiveRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "DEALSCOPE_MODE": "public_live",
                "DEALSCOPE_PUBLIC_RSS_ONLY": "1",
                "DEALSCOPE_PUBLIC_EXA_MCP": "1",
                "DEALSCOPE_ALLOW_PUBLIC_WECHAT_FALLBACK": "0",
                "DEALSCOPE_REFRESH_COOLDOWN_SECONDS": "900",
                "DEALSCOPE_DEEP_BASE_URL": "/workbench/",
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_public_live_page_enables_refresh_but_keeps_other_writes_locked(self) -> None:
        no_cache = weekly_radar._no_cache_report(weekly_radar._load_config())
        with patch.object(radar_app, "load_cached_report", return_value=no_cache):
            response = radar_app.app.test_client().get(
                "/", base_url="https://dealscope.example", environ_base={"REMOTE_ADDR": "203.0.113.8"}
            )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("会真正检索无凭据公开 RSS", html)
        refresh_tag = re.search(r'<button[^>]+id="refreshButton"[^>]*>', html)
        self.assertIsNotNone(refresh_tag)
        self.assertNotIn("disabled", refresh_tag.group(0))
        discovery_tags = re.findall(r'<button[^>]+data-wechat-discover[^>]*>', html)
        self.assertEqual(2, len(discovery_tags))
        self.assertTrue(all("disabled" not in tag for tag in discovery_tags))

        client = radar_app.app.test_client()
        expected = {"ok": True, "busy": False, "candidate_count": 2, "message": "updated"}
        with patch.object(radar_app, "_refresh_now", return_value=expected):
            allowed = client.post(
                "/api/refresh",
                base_url="https://dealscope.example",
                headers={"Origin": "https://dealscope.example"},
                json={},
                environ_base={"REMOTE_ADDR": "203.0.113.8"},
            )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json()["candidate_count"], 2)

        blocked_cross_origin = client.post(
            "/api/refresh",
            base_url="https://dealscope.example",
            headers={"Origin": "https://attacker.example"},
            json={},
            environ_base={"REMOTE_ADDR": "203.0.113.8"},
        )
        discovery_result = {
            "ok": True,
            "status": "ok",
            "message": "全网发现完成",
            "stats": {"unique_results": 2},
            "pool_write": {"added": 2, "exists": 0, "errors": 0},
            "finished_at": "2026-08-24T00:00:00Z",
        }
        with (
            patch.object(radar_app, "discover_wechat_sources", return_value=discovery_result) as discover,
            patch.object(radar_app, "_get_wechat_pool", return_value=object()),
            patch.object(radar_app, "_wechat_stats", return_value={"total": 2}),
        ):
            allowed_discovery = client.post(
                "/api/wechat/discover",
                base_url="https://dealscope.example",
                headers={"Origin": "https://dealscope.example"},
                json={},
                environ_base={"REMOTE_ADDR": "203.0.113.8"},
            )
        blocked_add = client.post(
            "/api/wechat/add",
            base_url="https://dealscope.example",
            headers={"Origin": "https://dealscope.example"},
            json={"url": "https://mp.weixin.qq.com/s/example"},
            environ_base={"REMOTE_ADDR": "203.0.113.8"},
        )
        self.assertEqual(blocked_cross_origin.status_code, 403)
        self.assertEqual(allowed_discovery.status_code, 200)
        self.assertEqual(2, allowed_discovery.get_json()["pool_write"]["added"])
        discover.assert_called_once_with(force=False, pool=ANY)
        self.assertEqual(blocked_add.status_code, 403)

    def test_public_refresh_cooldown_reuses_current_real_report(self) -> None:
        report = weekly_radar._no_cache_report(weekly_radar._load_config())
        report.update(
            {
                "status": "empty",
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source_status": {"google_news": {"status": "ok"}},
            }
        )
        with (
            patch.object(radar_app, "load_cached_report", return_value=report),
            patch.object(radar_app, "refresh_report") as live_refresh,
        ):
            result = radar_app._refresh_now()
        self.assertTrue(result["ok"])
        self.assertTrue(result["cached"])
        self.assertGreater(result["retry_after_seconds"], 0)
        live_refresh.assert_not_called()

    def test_public_rss_profile_never_calls_wechat_collector(self) -> None:
        google_status = {
            "status": "ok",
            "queries_total": 1,
            "queries_succeeded": 1,
            "items_seen": 0,
            "errors": [],
        }
        wechat_collector = Mock(side_effect=AssertionError("wechat collector must not run"))
        with (
            patch.object(weekly_radar, "_collect_google_news", return_value=([], google_status)),
            patch.object(weekly_radar, "_collect_wechat", wechat_collector),
            patch.object(weekly_radar, "_atomic_write_json"),
        ):
            report = weekly_radar.refresh_report("2026-08-24")
        self.assertEqual(report["source_status"]["wechat"]["status"], "skipped")
        wechat_collector.assert_not_called()

    def test_public_rss_profile_ignores_synthetic_cache(self) -> None:
        payload = weekly_radar._no_cache_report(weekly_radar._load_config())
        payload["synthetic"] = True
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "weekly_radar.json"
            output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(weekly_radar, "OUTPUT_PATH", output):
                loaded = weekly_radar.load_cached_report()
        self.assertEqual(loaded["status"], "no_cache")
        self.assertEqual(loaded["candidates"], [])


if __name__ == "__main__":
    unittest.main()
