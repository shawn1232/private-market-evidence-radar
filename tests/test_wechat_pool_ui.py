from __future__ import annotations

import unittest
from pathlib import Path

from app import radar_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "app" / "templates" / "radar.html"
SCRIPT_PATH = ROOT / "app" / "static" / "radar.js"
STYLE_PATH = ROOT / "app" / "static" / "radar.css"


class WechatPoolUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE_PATH.read_text(encoding="utf-8")
        cls.script = SCRIPT_PATH.read_text(encoding="utf-8")
        cls.style = STYLE_PATH.read_text(encoding="utf-8")

    def test_home_renders_all_three_library_tabs(self) -> None:
        response = radar_app.app.test_client().get("/")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("公众号文章库", page)
        self.assertIn('id="wechatPanelUrls"', page)
        self.assertIn('id="wechatPanelImport"', page)
        self.assertIn('id="wechatPanelLibrary"', page)
        self.assertIn("粘贴文章链接", page)
        self.assertIn("导入历史文件", page)
        self.assertIn("查看文章库", page)

    def test_template_has_stats_preview_and_explicit_coverage_boundary(self) -> None:
        for element_id in (
            "wechatStatTotal",
            "wechatStatWindow",
            "wechatStatPending",
            "wechatStatFailed",
            "wechatImportPreview",
            "wechatPoolRows",
            "refreshAfterWechatButton",
        ):
            self.assertIn(f'id="{element_id}"', self.template)

        self.assertIn("文件只在本机处理，不会上传到外部服务", self.template)
        self.assertIn("不代表账号全量历史", self.template)
        self.assertIn("不会自动拉取公众号账号全量历史", self.template)
        self.assertNotIn("扫码授权", self.template)
        self.assertNotIn("自动拉取近 7 天历史", self.template)

    def test_script_uses_the_agreed_pool_api_contract(self) -> None:
        expected_fragments = (
            "/api/wechat/pool?",
            "/api/wechat/urls",
            "/api/wechat/import/preview",
            "/api/wechat/import",
            "method: 'DELETE'",
            "JSON.stringify({ urls })",
            "form.append('fingerprint', wechatImportFingerprint)",
            "scope: summaryOnly ? 'all' : wechatPoolState.scope",
            "limit: String(summaryOnly ? 1 : wechatPoolState.limit)",
            "offset: String(summaryOnly ? 0 : wechatPoolState.offset)",
        )
        for fragment in expected_fragments:
            self.assertIn(fragment, self.script)

        self.assertNotIn("/api/wechat/add", self.script)

    def test_dynamic_article_fields_are_written_as_text_not_html(self) -> None:
        self.assertIn("titleCell.textContent = title", self.script)
        self.assertIn("cell.textContent = value", self.script)
        self.assertIn("item.textContent = issue && typeof issue === 'object'", self.script)
        self.assertNotIn("innerHTML", self.script)

    def test_styles_cover_dialog_tabs_stats_table_and_mobile_layout(self) -> None:
        for selector in (
            ".pool-stats",
            ".pool-tabs",
            ".pool-panel[hidden]",
            ".file-drop",
            ".import-preview",
            ".pool-table-wrap",
            ".pool-pagination",
            ".boundary-note",
        ):
            self.assertIn(selector, self.style)
        self.assertIn("@media (max-width: 520px)", self.style)


if __name__ == "__main__":
    unittest.main()
