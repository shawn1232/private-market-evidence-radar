from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

from werkzeug.test import Client
from werkzeug.wrappers import Response


class CloudApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.environment = patch.dict(
            os.environ,
            {
                "DEALSCOPE_MODE": "public_readonly",
                "DEALSCOPE_DISABLE_NETWORK": "1",
                "DEALSCOPE_DEEP_BASE_URL": "/workbench/",
                "DEALSCOPE_RADAR_BASE_URL": "/",
            },
        )
        cls.environment.start()
        module = importlib.import_module("cloud_app")
        cls.client = Client(module.application, Response)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.environment.stop()

    def test_real_radar_and_workbench_share_one_public_origin(self) -> None:
        radar = self.client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.8"})
        self.assertEqual(radar.status_code, 200)
        radar_html = radar.get_data(as_text=True)
        self.assertIn("一级市场证据雷达", radar_html)
        self.assertIn("真实 Flask 在线版", radar_html)
        self.assertIn('href="/workbench/"', radar_html)
        self.assertIn("澄光微纳（虚构）", radar_html)

        workbench = self.client.get(
            "/workbench/?q=澄光微纳（虚构）&company=澄光微纳（虚构）",
            environ_base={"REMOTE_ADDR": "203.0.113.8"},
        )
        self.assertEqual(workbench.status_code, 200)
        workbench_html = workbench.get_data(as_text=True)
        self.assertIn("一级市场证据工作台", workbench_html)
        self.assertIn("同一套 Flask 程序实时渲染", workbench_html)
        self.assertIn('href="/"', workbench_html)
        self.assertIn("/workbench/api/pipeline-status", workbench_html)

    def test_public_mode_exposes_only_synthetic_read_routes(self) -> None:
        report = self.client.get("/api/report", environ_base={"REMOTE_ADDR": "203.0.113.8"})
        self.assertEqual(report.status_code, 200)
        payload = report.get_json()
        self.assertTrue(payload["is_demo"])
        self.assertTrue(all("虚构" in row["company_name"] for row in payload["candidates"]))

        pool = self.client.get("/api/wechat/pool", environ_base={"REMOTE_ADDR": "203.0.113.8"})
        self.assertEqual(pool.status_code, 200)
        self.assertEqual(pool.get_json()["rows"], [])
        self.assertTrue(pool.get_json()["public_readonly"])

        radar_health = self.client.get("/health", environ_base={"REMOTE_ADDR": "203.0.113.8"})
        workbench_health = self.client.get(
            "/workbench/health", environ_base={"REMOTE_ADDR": "203.0.113.8"}
        )
        self.assertEqual(radar_health.get_json()["mode"], "public_readonly")
        self.assertEqual(workbench_health.get_json()["mode"], "public_readonly")
        self.assertNotIn("pipeline", workbench_health.get_json())

    def test_public_mode_blocks_every_mutating_route(self) -> None:
        blocked = [
            ("/api/refresh", "POST"),
            ("/api/wechat/add", "POST"),
            ("/api/wechat/discover", "POST"),
            ("/api/wechat/pool/1", "DELETE"),
            ("/workbench/generate", "POST"),
            ("/workbench/save-urls", "POST"),
            ("/workbench/run", "POST"),
            ("/workbench/login/weixin", "POST"),
            ("/workbench/open-project", "POST"),
        ]
        for path, method in blocked:
            with self.subTest(path=path):
                response = self.client.open(
                    path,
                    method=method,
                    environ_base={"REMOTE_ADDR": "203.0.113.8"},
                )
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
