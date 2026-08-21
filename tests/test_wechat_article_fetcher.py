from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import wechat_article_fetcher as fetcher


class WechatArticleFetcherTests(unittest.TestCase):
    @staticmethod
    def _fake_script(directory: str) -> Path:
        script = Path(directory) / "download_urls.py"
        script.write_text("# mocked by unit tests\n", encoding="utf-8")
        return script

    @staticmethod
    def _write_index(output_dir: Path, rows: list[dict[str, str]]) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "index.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["seq", "title", "source_url", "format", "path", "status", "error", "downloaded_at"],
            )
            writer.writeheader()
            writer.writerows(rows)

    def test_only_exact_public_https_host_is_accepted_and_sensitive_url_is_redacted(self) -> None:
        urls = [
            "http://mp.weixin.qq.com/s/a",
            "https://evil.example/s/a",
            "https://mp.weixin.qq.com.evil.example/s/a",
            "https://mp.weixin.qq.com/s/a?token=secret-value",
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(fetcher.subprocess, "run") as run:
            result = fetcher.fetch_known_wechat_articles(
                urls,
                output_root=directory,
                run_id="invalid-only",
            )

        run.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["failure_count"], 4)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-value", serialized)
        self.assertIn("[REDACTED_INVALID_URL]", result["failed_urls"])
        self.assertNotIn("json-secret", fetcher._sanitize_error('{"token":"json-secret"}'))

    def test_downloader_script_can_be_injected_explicitly_or_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skill"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            script = self._fake_script(str(scripts))

            explicit = fetcher.resolve_downloader_script(script)
            from_env = fetcher.resolve_downloader_script(
                environment={fetcher.SKILL_DIR_ENV: str(root)}
            )

        self.assertEqual(explicit, script.resolve())
        self.assertEqual(from_env, script.resolve())

    def test_partial_batch_parses_index_errors_and_returns_pool_friendly_rows(self) -> None:
        url_ok = "https://mp.weixin.qq.com/s/success#fragment"
        canonical_ok = "https://mp.weixin.qq.com/s/success"
        url_failed = "https://mp.weixin.qq.com/s/failed"
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
            captured["command"] = command
            captured["env"] = kwargs["env"]
            output_dir = Path(command[command.index("--output-dir") + 1])
            article_dir = output_dir / "articles"
            article_dir.mkdir(parents=True)
            (article_dir / "001-title.md").write_text("# 测试文章\n\n正文", encoding="utf-8")
            self._write_index(
                output_dir,
                [
                    {
                        "seq": "001",
                        "title": "测试文章",
                        "source_url": canonical_ok,
                        "format": "markdown",
                        "path": "articles/001-title.md",
                        "status": "success",
                        "error": "",
                        "downloaded_at": "2026-08-21T00:00:00+00:00",
                    },
                    {
                        "seq": "002",
                        "title": "",
                        "source_url": url_failed,
                        "format": "markdown",
                        "path": "",
                        "status": "failed",
                        "error": "token=secret-value upstream denied",
                        "downloaded_at": "2026-08-21T00:00:01+00:00",
                    },
                ],
            )
            (output_dir / "errors.json").write_text(
                json.dumps(
                    [{"seq": "002", "source_url": url_failed, "error": "auth_key=another-secret"}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=1, stdout="cookie=stdout-secret", stderr="token=stderr-secret")

        environment = {
            "PATH": "safe-path",
            "SAFE_VALUE": "keep-me",
            "WECHAT_TOKEN": "do-not-pass",
            "BRAVE_API_KEY": "do-not-pass-either",
            "AUTH_KEY": "do-not-pass",
        }
        with tempfile.TemporaryDirectory() as directory:
            script = self._fake_script(directory)
            with patch.object(fetcher.subprocess, "run", side_effect=fake_run):
                result = fetcher.fetch_known_wechat_articles(
                    [url_ok, canonical_ok, url_failed],
                    output_root=Path(directory) / "output",
                    run_id="partial-run",
                    script_path=script,
                    python_executable="python-test",
                    request_timeout_seconds=17,
                    process_timeout_seconds=33,
                    sleep_seconds=0,
                    environment=environment,
                )

            body_path = Path(result["successes"][0]["body_markdown_path"])
            self.assertTrue(body_path.is_file())

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["requested_count"], 3)
        self.assertEqual(result["accepted_count"], 2)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 1)
        success = result["successes"][0]
        self.assertEqual(success["url"], canonical_ok)
        self.assertIn("account_name", success)
        self.assertIn("publish_time", success)
        self.assertIn("comment_replies_path", success)
        self.assertEqual(success["fetch_mode"], "known_url_public_exporter")
        self.assertEqual(success["credential_status"], "not_required")
        command = captured["command"]
        self.assertEqual(command[0], "python-test")
        self.assertIn("--file", command)
        self.assertEqual(command[command.index("--format") + 1], "markdown")
        self.assertEqual(command[command.index("--timeout") + 1], "17")
        child_env = captured["env"]
        self.assertEqual(child_env["SAFE_VALUE"], "keep-me")
        self.assertNotIn("WECHAT_TOKEN", child_env)
        self.assertNotIn("BRAVE_API_KEY", child_env)
        self.assertNotIn("AUTH_KEY", child_env)
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in ["secret-value", "another-secret", "stdout-secret", "stderr-secret", "do-not-pass"]:
            self.assertNotIn(secret, serialized)

    def test_index_path_traversal_is_rejected(self) -> None:
        url = "https://mp.weixin.qq.com/s/path-check"

        def fake_run(command: list[str], **_: object) -> SimpleNamespace:
            output_dir = Path(command[command.index("--output-dir") + 1])
            self._write_index(
                output_dir,
                [
                    {
                        "seq": "001",
                        "title": "越界文件",
                        "source_url": url,
                        "format": "markdown",
                        "path": "../../outside.md",
                        "status": "success",
                        "error": "",
                        "downloaded_at": "2026-08-21T00:00:00+00:00",
                    }
                ],
            )
            (output_dir / "errors.json").write_text("[]", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            script = self._fake_script(directory)
            with patch.object(fetcher.subprocess, "run", side_effect=fake_run):
                result = fetcher.fetch_known_wechat_articles(
                    [url],
                    output_root=Path(directory) / "output",
                    run_id="path-check",
                    script_path=script,
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failures"][0]["error_code"], "invalid_body_path")

    def test_process_timeout_returns_structured_failures_without_process_output(self) -> None:
        urls = ["https://mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com/s/b"]
        timeout = subprocess.TimeoutExpired(
            cmd=["python", "download_urls.py"],
            timeout=2,
            output="token=secret-output",
            stderr="cookie=secret-error",
        )
        with tempfile.TemporaryDirectory() as directory:
            script = self._fake_script(directory)
            with patch.object(fetcher.subprocess, "run", side_effect=timeout):
                result = fetcher.fetch_known_wechat_articles(
                    urls,
                    output_root=Path(directory) / "output",
                    run_id="timeout-run",
                    script_path=script,
                    process_timeout_seconds=2,
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_count"], 2)
        self.assertEqual({item["error_code"] for item in result["failures"]}, {"timeout"})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-output", serialized)
        self.assertNotIn("secret-error", serialized)

    def test_missing_index_after_process_failure_is_structured(self) -> None:
        url = "https://mp.weixin.qq.com/s/no-index"
        completed = SimpleNamespace(returncode=2, stdout="auth_key=secret", stderr="token=secret")
        with tempfile.TemporaryDirectory() as directory:
            script = self._fake_script(directory)
            with patch.object(fetcher.subprocess, "run", return_value=completed):
                result = fetcher.fetch_known_wechat_articles(
                    [url],
                    output_root=Path(directory) / "output",
                    run_id="missing-index",
                    script_path=script,
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failures"][0]["error_code"], "process_failed")
        self.assertNotIn("secret", json.dumps(result, ensure_ascii=False))

    def test_run_id_cannot_escape_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(fetcher.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                fetcher.fetch_known_wechat_articles(
                    ["https://mp.weixin.qq.com/s/a"],
                    output_root=directory,
                    run_id="../escape",
                    script_path=Path(directory) / "download_urls.py",
                )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
