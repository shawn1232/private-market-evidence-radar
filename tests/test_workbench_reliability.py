from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import run_pipeline
from app import app as workbench_module


class WorkbenchReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        workbench_module.app.config.update(TESTING=True)
        workbench_module._pipeline_state.update(
            running=False,
            status="idle",
            message="",
            started_at="",
            finished_at="",
            thesis="",
        )
        self.client = workbench_module.app.test_client()

    def test_home_renders_and_demo_report_is_clearly_marked(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("历史演示样例".encode("utf-8"), response.data)

    def test_mutating_routes_require_post_and_reject_external_origin(self) -> None:
        self.assertEqual(self.client.get("/generate").status_code, 405)
        self.assertEqual(self.client.get("/open-project").status_code, 405)
        self.assertEqual(self.client.get("/login/xiaohongshu").status_code, 405)
        response = self.client.post(
            "/generate",
            data={"q": "工业AI"},
            headers={"Origin": "https://malicious.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_manual_urls_can_be_saved_from_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            url_path = Path(directory) / "urls.txt"
            with patch.object(workbench_module, "URLS_PATH", url_path):
                response = self.client.post(
                    "/save-urls",
                    data={
                        "q": "工业AI",
                        "urls": "https://example.org/a\nnot-a-url\nhttps://example.org/a\n",
                    },
                )
                saved = workbench_module.load_manual_urls_text()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(saved, "https://example.org/a")

    def test_pipeline_route_starts_background_job_and_surfaces_failure(self) -> None:
        class ImmediateThread:
            def __init__(self, *, target, args, **_kwargs) -> None:
                self.target = target
                self.args = args

            def start(self) -> None:
                self.target(*self.args)

        process = Mock(
            returncode=2,
            stdout=json.dumps({"ok": False, "message": "没有可核验证据"}, ensure_ascii=False),
            stderr="",
        )
        with (
            patch.object(workbench_module.threading, "Thread", ImmediateThread),
            patch.object(workbench_module.subprocess, "run", return_value=process),
        ):
            response = self.client.post("/run", data={"q": "工业AI", "intensity": "standard"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(workbench_module._pipeline_state["running"])
        self.assertEqual(workbench_module._pipeline_state["status"], "error")
        self.assertIn("没有可核验证据", workbench_module._pipeline_state["message"])

    def test_placeholder_source_marks_report_as_demo(self) -> None:
        report = {
            "candidates": [
                {"evidence": [{"source_url": "https://news.example.org/article/123"}]}
            ]
        }
        self.assertTrue(workbench_module.is_demo_report(report))
        self.assertFalse(
            workbench_module.is_demo_report(
                {"candidates": [{"evidence": [{"source_url": "https://example.org/article/456"}]}]}
            )
        )

    def test_demo_candidates_and_scores_are_not_rendered_as_real_results(self) -> None:
        raw = {
            "thesis": "演示主题",
            "total_evidence": 9,
            "candidates": [
                {
                    "entity": "虚构公司",
                    "total_score": 99,
                    "confidence_score": 100,
                    "evidence": [{"source_url": "https://example.org/article/123"}],
                }
            ],
        }
        normalized = workbench_module.normalize_report(raw, thesis_hint="真实待评主题")
        self.assertTrue(normalized["is_demo"])
        self.assertEqual(normalized["candidates"], [])
        self.assertEqual(normalized["stats"]["total_evidence"], 0)
        self.assertEqual(normalized["title_line"], "真实待评主题")

    def test_empty_pipeline_preserves_previous_successful_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "output"
            raw = root / "raw"
            input_dir = root / "input"
            output.mkdir()
            input_dir.mkdir()
            latest = output / "latest_report.json"
            latest.write_text('{"sentinel": true}\n', encoding="utf-8")
            urls = input_dir / "urls.txt"
            urls.write_text("# no urls\n", encoding="utf-8")
            attempt = output / "latest_pipeline_attempt.json"
            discovery = output / "discovery_links.json"

            patches = (
                patch.object(run_pipeline, "OUT_DIR", output),
                patch.object(run_pipeline, "RAW_DIR", raw),
                patch.object(run_pipeline, "URLS_PATH", urls),
                patch.object(run_pipeline, "LATEST_REPORT_PATH", latest),
                patch.object(run_pipeline, "LATEST_ATTEMPT_PATH", attempt),
                patch.object(run_pipeline, "DISCOVERY_LINKS_PATH", discovery),
                patch.object(run_pipeline, "build_discovery_links", return_value=[]),
                patch.object(run_pipeline, "should_run_auto_search", return_value=False),
                patch.object(
                    run_pipeline,
                    "get_api_key_status",
                    return_value={name: False for name in run_pipeline.API_KEY_NAMES},
                ),
                patch.object(sys, "argv", ["run_pipeline.py", "--thesis", "测试主题"]),
            )
            for active_patch in patches:
                active_patch.start()
            try:
                with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()):
                    run_pipeline.main()
            finally:
                for active_patch in reversed(patches):
                    active_patch.stop()

            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(json.loads(latest.read_text(encoding="utf-8")), {"sentinel": True})
            attempt_payload = json.loads(attempt.read_text(encoding="utf-8"))
            self.assertFalse(attempt_payload["ok"])
            self.assertEqual(attempt_payload["status"], "no_verified_evidence")


if __name__ == "__main__":
    unittest.main()
