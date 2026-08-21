from __future__ import annotations

import unittest
from pathlib import Path

from app import radar_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "app" / "templates" / "radar.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "app" / "static" / "radar.js").read_text(encoding="utf-8")
STYLE = (ROOT / "app" / "static" / "radar.css").read_text(encoding="utf-8")


class WechatDiscoveryUiTests(unittest.TestCase):
    def test_home_renders_discovery_action_metrics_and_evidence_boundary(self) -> None:
        response = radar_app.app.test_client().get("/")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(page.count("全网拓源一次"), 2)
        self.assertIn("主动全网发现", page)
        self.assertIn("发现 ≠ 证据", page)
        self.assertIn("公众号带动复核", TEMPLATE)
        self.assertIn("followup_news", TEMPLATE)
        self.assertIn('id="wechatDiscoveredTotal"', page)
        self.assertIn('id="wechatDiscoveredAccounts"', page)
        self.assertIn('id="wechatDiscoveredWindow"', page)
        self.assertIn('id="wechatLastDiscovery"', page)
        self.assertIn('data-wechat-scope="discovered"', page)

    def test_copy_describes_discovery_plus_manual_sourcing_without_overclaiming(self) -> None:
        self.assertIn("主动全网发现与手工补充共同构成", TEMPLATE)
        self.assertIn("未取得正文和真实发布日期不会进入七日评分", TEMPLATE)
        self.assertIn("主动拓源不会触发扫码、登录", TEMPLATE)
        self.assertIn("不代表已核验证据", TEMPLATE)
        self.assertNotIn("公众号仅覆盖你主动粘贴或从历史文件导入", TEMPLATE)
        self.assertNotIn("扫码授权", TEMPLATE)

    def test_script_calls_discovery_api_and_handles_queued_or_unavailable_states(self) -> None:
        self.assertIn("async function discoverWechatSources()", SCRIPT)
        self.assertIn("fetch('/api/wechat/discover'", SCRIPT)
        self.assertIn("method: 'POST'", SCRIPT)
        self.assertIn("data.queued", SCRIPT)
        self.assertIn("主动拓源服务暂不可用", SCRIPT)
        self.assertIn("仍可粘贴原文或导入历史文件", SCRIPT)
        self.assertIn("querySelectorAll('[data-wechat-discover]')", SCRIPT)

    def test_discovery_stats_and_row_provenance_are_rendered_as_text(self) -> None:
        for field in (
            "discovered_total",
            "discovered_accounts",
            "discovered_in_window",
            "last_discovery_at",
            "discovery_query",
            "provider",
        ):
            self.assertIn(field, SCRIPT)
        self.assertIn("discovery: '全网发现'", SCRIPT)
        self.assertIn("['discovery_provider', 'provider']", SCRIPT)
        self.assertIn("detail.textContent = `${provider} · ${query}`", SCRIPT)
        self.assertNotIn("innerHTML", SCRIPT)

    def test_discovery_visuals_are_responsive(self) -> None:
        for selector in (
            ".source-pill-button.discovery-pill",
            ".discovery-panel",
            ".evidence-boundary-badge",
            ".discovery-metrics",
            ".discovery-last-run",
            ".pool-source-detail",
            ".status-discovered",
        ):
            self.assertIn(selector, STYLE)
        self.assertIn(".discovery-panel { align-items: stretch; flex-direction: column; }", STYLE)


if __name__ == "__main__":
    unittest.main()
