from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import radar_app
from wechat_source_pool import WeChatSourcePool


class WeChatPoolApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_pool = radar_app._wechat_pool
        radar_app._wechat_pool = WeChatSourcePool(Path(self.temp.name) / "pool.sqlite3")
        radar_app.app.config.update(TESTING=True)
        self.client = radar_app.app.test_client()

    def tearDown(self) -> None:
        radar_app._wechat_pool = self.previous_pool
        self.temp.cleanup()

    def test_url_add_distinguishes_new_existing_and_invalid(self) -> None:
        payload = {
            "urls": [
                "https://mp.weixin.qq.com/s/manual-one",
                "https://mp.weixin.qq.com/s/manual-one#again",
                "https://example.com/not-wechat",
            ]
        }
        with patch.object(radar_app, "add_wechat_url", return_value={"ok": True}):
            response = self.client.post("/api/wechat/urls", json=payload)
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["added_count"], 1)
        self.assertEqual(data["existing_count"], 1)
        self.assertEqual(data["invalid_count"], 1)
        self.assertEqual(data["stats"]["total"], 1)

    def test_preview_confirm_window_listing_and_delete(self) -> None:
        raw = json.dumps(
            [
                {
                    "account_name": "硬科技前沿",
                    "title": "测试半导体完成天使轮融资",
                    "url": "https://mp.weixin.qq.com/s/import-one",
                    "publish_time": "2026-08-20",
                    "cookie": "must-not-persist",
                }
            ],
            ensure_ascii=False,
        ).encode("utf-8")
        preview_response = self.client.post(
            "/api/wechat/import/preview",
            data={"file": (io.BytesIO(raw), "history.json")},
            content_type="multipart/form-data",
        )
        preview = preview_response.get_json()
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(preview["added_count"], 1)
        self.assertEqual(preview["credential_fields_ignored"], 1)

        with patch.object(radar_app, "add_wechat_url", return_value={"ok": True}):
            import_response = self.client.post(
                "/api/wechat/import",
                data={
                    "file": (io.BytesIO(raw), "history.json"),
                    "fingerprint": preview["fingerprint"],
                },
                content_type="multipart/form-data",
            )
        imported = import_response.get_json()
        self.assertEqual(import_response.status_code, 200)
        self.assertEqual(imported["added_count"], 1)
        self.assertEqual(imported["stats"]["in_window"], 1)

        listing = self.client.get("/api/wechat/pool?scope=window&limit=50&offset=0").get_json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["rows"][0]["status"], "pending")
        article_id = listing["rows"][0]["article_id"]

        denied = self.client.delete(
            f"/api/wechat/pool/{article_id}",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(denied.status_code, 403)
        removed = self.client.delete(f"/api/wechat/pool/{article_id}")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.get_json()["stats"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
