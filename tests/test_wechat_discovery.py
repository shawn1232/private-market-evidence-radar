from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from wechat_discovery import (
    ExaMcpHttpBackend,
    McporterExaBackend,
    SearchBackendError,
    build_config_query_plan,
    build_query_plan,
    canonicalize_discovered_url,
    discover_wechat_articles,
    load_discovery_config,
    normalize_search_results,
)


ARTICLE_A = "https://mp.weixin.qq.com/s?__biz=MzA1&mid=100&idx=1&sn=abc"
ARTICLE_B = "https://mp.weixin.qq.com/s/article-slug"


class FakeBackend:
    provider = "mock_search"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def search(self, query: str, *, limit: int):
        self.calls.append((query, limit))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, BaseException):
            raise response
        return response


class FakeHttpResponse:
    def __init__(self, body, *, status=200, headers=None):
        self.status_code = status
        self.content = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.headers = dict(headers or {})

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class WeChatDiscoveryTests(unittest.TestCase):
    def test_query_plan_is_sector_by_event_and_bounded(self):
        plan = build_query_plan(
            {"\u534a\u5bfc\u4f53": ["\u82af\u7247"], "\u673a\u5668\u4eba": ["\u5177\u8eab\u667a\u80fd"]},
            {"\u878d\u8d44": ["\u878d\u8d44"], "\u91cf\u4ea7": ["\u6295\u4ea7"]},
            max_queries=3,
        )

        self.assertEqual(3, len(plan))
        self.assertEqual(
            [("\u534a\u5bfc\u4f53", "\u878d\u8d44"), ("\u534a\u5bfc\u4f53", "\u91cf\u4ea7"), ("\u673a\u5668\u4eba", "\u878d\u8d44")],
            [(item["sector"], item["event"]) for item in plan],
        )
        self.assertTrue(all("site:mp.weixin.qq.com/s" in item["query"] for item in plan))
        self.assertTrue(all(item["discovery_only"] for item in plan))

    def test_url_boundary_is_exact_and_canonical(self):
        tracked = ARTICLE_A + "&utm_source=search#fragment"
        canonical = canonicalize_discovered_url(tracked)
        self.assertEqual(ARTICLE_A, canonical)
        self.assertEqual(ARTICLE_B, canonicalize_discovered_url("http://mp.weixin.qq.com/s/article-slug?utm_source=x"))

        rejected = (
            "https://mp.weixin.qq.com.evil.test/s/article",
            "https://mp.weixin.qq.com@127.0.0.1/s/article",
            "http://127.0.0.1/s/article",
            "https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum",
            "https://mp.weixin.qq.com/s",
            "https://mp.weixin.qq.com/s/${article_id}",
            "https://mp.weixin.qq.com/s/article%0d%0aHost:127.0.0.1",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonicalize_discovered_url(value)

    def test_encoded_redirect_target_is_extracted_without_accepting_wrapper_host(self):
        wrapper = (
            "https://search.example/redirect?url="
            "https%3A%2F%2Fmp.weixin.qq.com%2Fs%3F__biz%3DMzA1%26mid%3D100%26idx%3D1%26sn%3Dabc"
        )
        self.assertEqual(ARTICLE_A, canonicalize_discovered_url(wrapper))
        with self.assertRaises(ValueError):
            canonicalize_discovered_url("https://search.example/redirect?url=http%3A%2F%2F127.0.0.1%2Fsecret")

    def test_discovery_dedupes_and_keeps_all_query_provider_provenance(self):
        plan = [
            {"query_id": "q1", "query": "query one", "sector": "\u82af\u7247", "event": "\u878d\u8d44"},
            {"query_id": "q2", "query": "query two", "sector": "\u82af\u7247", "event": "\u91cf\u4ea7"},
        ]
        backend = FakeBackend(
            [
                [
                    {"title": "A", "url": ARTICLE_A + "&utm_source=exa", "summary": "\u641c\u7d22\u6458\u8981\u58f0\u79f0\u5df2\u878d\u8d44", "provider": "exa"},
                    {"title": "not WeChat", "url": "https://example.com/a"},
                ],
                [{"title": "A duplicate", "link": ARTICLE_A, "provider": "brave"}],
            ]
        )

        result = discover_wechat_articles(
            search_backend=backend,
            query_plan=plan,
            results_per_query=4,
            discovered_at=datetime(2026, 8, 21, 1, 2, 3, tzinfo=timezone.utc),
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual([(item["query"], 4) for item in plan], backend.calls)
        self.assertEqual(1, result["stats"]["unique_results"])
        self.assertEqual(1, result["stats"]["duplicates_removed"])
        self.assertEqual(1, result["stats"]["rejected_non_wechat"])
        row = result["results"][0]
        self.assertEqual(ARTICLE_A, row["url"])
        self.assertEqual(["query one", "query two"], row["query_hits"])
        self.assertEqual(["exa", "brave"], row["providers"])
        self.assertEqual(2, row["retrieval_provider_count"])
        self.assertEqual("retrieval_channels", row["provider_count_kind"])
        self.assertEqual(0, row["independent_source_count"])
        self.assertFalse(row["independently_corroborated"])
        self.assertEqual("2026-08-21T01:02:03Z", row["discovered_at"])
        self.assertEqual(2, len(row["discoveries"]))

    def test_search_snippet_never_becomes_evidence(self):
        backend = FakeBackend([[{"title": "headline", "url": ARTICLE_B, "summary": "\u5ba2\u6237\u5df2\u8ba4\u8bc1\uff0c\u5b8c\u6210\u878d\u8d44"}]])
        result = discover_wechat_articles(
            search_backend=backend,
            query_plan=[{"query": "one", "sector": "s", "event": "e"}],
            discovered_at="2026-08-21T00:00:00Z",
        )

        row = result["results"][0]
        self.assertEqual("\u5ba2\u6237\u5df2\u8ba4\u8bc1\uff0c\u5b8c\u6210\u878d\u8d44", row["search_snippet"])
        self.assertTrue(row["discovery_only"])
        self.assertFalse(row["evidence_eligible"])
        self.assertEqual("search_result_only", row["evidence_status"])
        self.assertEqual("source_page_not_fetched", row["verification_status"])
        self.assertNotIn("clean_text", row)
        self.assertNotIn("quote", row)
        self.assertIn("discovery metadata only", result["evidence_policy"])

    def test_backend_failures_are_structured_and_do_not_fabricate_results(self):
        error = SearchBackendError("backend_timeout", "token=top-secret timeout", retriable=True)
        backend = FakeBackend([error])
        result = discover_wechat_articles(
            search_backend=backend,
            query_plan=[{"query": "one", "sector": "s", "event": "e"}],
        )

        self.assertEqual("error", result["status"])
        self.assertEqual([], result["results"])
        self.assertEqual(1, result["stats"]["queries_failed"])
        self.assertEqual("backend_timeout", result["errors"][0]["code"])
        self.assertTrue(result["errors"][0]["retriable"])
        self.assertNotIn("top-secret", json.dumps(result, ensure_ascii=False))

    def test_one_query_failure_yields_partial_results(self):
        backend = FakeBackend(
            [
                RuntimeError("temporary failure"),
                [{"title": "found", "url": ARTICLE_B}],
            ]
        )
        result = discover_wechat_articles(
            search_backend=backend,
            query_plan=[{"query": "one"}, {"query": "two"}],
            discovered_at="fixed",
        )

        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["stats"]["queries_succeeded"])
        self.assertEqual(1, result["stats"]["queries_failed"])
        self.assertEqual(1, len(result["results"]))
        self.assertEqual("backend_exception", result["errors"][0]["code"])

    def test_mcporter_backend_disables_oauth_and_parses_json_envelope(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            stdout = json.dumps(
                {
                    "content": [
                        {"type": "text", "text": f"[Article title]({ARTICLE_B})"},
                    ]
                }
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        backend = McporterExaBackend(runner=runner, timeout=5)
        rows = backend.search("\u82af\u7247 \u878d\u8d44", limit=3)

        command = captured["command"]
        self.assertEqual(["mcporter", "call", "exa.web_search_exa"], command[:3])
        self.assertIn("--no-oauth", command)
        self.assertEqual("json", command[command.index("--output") + 1])
        arguments = json.loads(command[command.index("--args") + 1])
        self.assertEqual({"query": "\u82af\u7247 \u878d\u8d44", "numResults": 3}, arguments)
        self.assertFalse(captured["kwargs"]["shell"])
        self.assertEqual(ARTICLE_B, rows[0]["url"])
        self.assertEqual("Article title", rows[0]["title"])

    def test_exa_mcp_http_backend_initializes_session_and_parses_utf8_sse(self):
        init_body = 'event: message\ndata: {"result":{"protocolVersion":"2025-03-26"},"jsonrpc":"2.0","id":1}\n\n'
        tool_text = "\n".join(
            [
                "Title: 新发现公众号文章",
                f"URL: {ARTICLE_B}",
                "Published: N/A",
                "Author: 新来源账号",
                "Highlights: 仅为搜索线索",
            ]
        )
        tool_body = "event: message\ndata: " + json.dumps(
            {"result": {"content": [{"type": "text", "text": tool_text}]}, "jsonrpc": "2.0", "id": 2},
            ensure_ascii=False,
        ) + "\n\n"
        session = FakeHttpSession(
            [
                FakeHttpResponse(
                    init_body,
                    headers={"Content-Type": "text/event-stream", "mcp-session-id": "session-1"},
                ),
                FakeHttpResponse(b"", status=202),
                FakeHttpResponse(tool_body, headers={"Content-Type": "text/event-stream"}),
            ]
        )
        backend = ExaMcpHttpBackend(session=session, timeout=5)

        rows = backend.search("site:mp.weixin.qq.com/s 芯片 融资", limit=3)

        self.assertEqual(1, len(rows))
        self.assertEqual("新发现公众号文章", rows[0]["title"])
        self.assertEqual("新来源账号", rows[0]["author"])
        self.assertEqual(ARTICLE_B, rows[0]["url"])
        self.assertEqual(3, len(session.calls))
        init_headers = session.calls[0][1]["headers"]
        call_headers = session.calls[2][1]["headers"]
        self.assertNotIn("Mcp-Session-Id", init_headers)
        self.assertEqual("session-1", call_headers["Mcp-Session-Id"])
        arguments = session.calls[2][1]["json"]["params"]["arguments"]
        self.assertEqual(3, arguments["numResults"])
        self.assertIn("site:mp.weixin.qq.com/s", arguments["query"])

    def test_public_exa_profile_is_bounded_and_uses_http_transport(self):
        payload = {
            "window_days": 7,
            "wechat_discovery": {
                "enabled": True,
                "provider": "mcporter_exa",
                "interval_hours": 1,
                "max_queries_per_run": 30,
                "num_results_per_query": 50,
                "max_new_urls_per_run": 100,
                "timeout_seconds": 120,
                "queries": [{"name": "one", "query": "芯片 融资"}],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.dict(os.environ, {"DEALSCOPE_PUBLIC_EXA_MCP": "1"}):
                settings = load_discovery_config(path)

        self.assertEqual("exa_mcp_http", settings["provider"])
        self.assertEqual(6.0, settings["interval_hours"])
        self.assertEqual(3, settings["max_queries_per_run"])
        self.assertEqual(8, settings["num_results_per_query"])
        self.assertEqual(24, settings["max_new_urls_per_run"])
        self.assertEqual(30.0, settings["timeout_seconds"])

    def test_result_normalizer_handles_nested_mcp_text_without_claim_fields(self):
        rows = normalize_search_results(
            {
                "content": [
                    {"type": "text", "text": f"URL: {ARTICLE_A}\nA search-only description"},
                ]
            }
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(ARTICLE_A, rows[0]["url"])
        self.assertNotIn("evidence", rows[0])

    def test_exa_text_blocks_keep_title_and_author_as_discovery_metadata(self):
        rows = normalize_search_results(
            "\n".join(
                [
                    "Title: 新公众号发现的融资文章",
                    f"URL: {ARTICLE_B}",
                    "Published: N/A",
                    "Author: 未关注的新公众号",
                    "Highlights:",
                    "搜索摘要只用于发现，不是证据。",
                ]
            )
        )

        self.assertEqual(1, len(rows))
        self.assertEqual("新公众号发现的融资文章", rows[0]["title"])
        self.assertEqual("未关注的新公众号", rows[0]["author"])
        self.assertEqual("", rows[0]["search_published_at"])
        self.assertIn("不是证据", rows[0]["summary"])
        self.assertNotIn("evidence", rows[0])

    def test_config_queries_render_placeholders_and_bound_the_run(self):
        payload = {
            "window_days": 9,
            "wechat_discovery": {
                "enabled": True,
                "provider": "mcporter_exa",
                "interval_hours": 12,
                "max_queries_per_run": 1,
                "num_results_per_query": 2,
                "max_new_urls_per_run": 1,
                "timeout_seconds": 7,
                "queries": [
                    {"name": "monthly", "query": "\u82af\u7247 {year}-{month} \u8fd1{window_days}\u5929 \u878d\u8d44"},
                    {"name": "unused", "query": "\u673a\u5668\u4eba \u91cf\u4ea7"},
                ],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            settings = load_discovery_config(path)
            plan = build_config_query_plan(settings, as_of="2026-08-21")
            backend = FakeBackend([[{"url": ARTICLE_A}, {"url": ARTICLE_B}]])
            result = discover_wechat_articles(
                search_backend=backend,
                config_path=path,
                as_of="2026-08-21",
                discovered_at="fixed",
            )

        self.assertEqual("wechat_discovery.queries", settings["query_source"])
        self.assertEqual(12.0, settings["interval_hours"])
        self.assertEqual(1, len(plan))
        self.assertIn("site:mp.weixin.qq.com/s", plan[0]["query"])
        self.assertIn("2026-08", plan[0]["query"])
        self.assertIn("\u8fd19\u5929", plan[0]["query"])
        self.assertEqual([(plan[0]["query"], 2)], backend.calls)
        self.assertEqual(1, result["stats"]["unique_results"])
        self.assertEqual(1, result["stats"]["new_urls_capped"])
        self.assertEqual(1, result["config"]["max_new_urls_per_run"])

    def test_disabled_config_skips_backend_and_google_queries_are_a_fallback(self):
        disabled = {
            "window_days": 7,
            "google_news_queries": [{"name": "sector seed", "query": "(\u82af\u7247 OR \u534a\u5bfc\u4f53) \u878d\u8d44 when:7d"}],
            "wechat_discovery": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(json.dumps(disabled, ensure_ascii=False), encoding="utf-8")
            settings = load_discovery_config(path)
            backend = FakeBackend([])
            result = discover_wechat_articles(
                search_backend=backend,
                config_path=path,
                as_of="2026-08-21",
                discovered_at="fixed",
            )

        self.assertEqual("google_news_queries", settings["query_source"])
        self.assertEqual("skipped", result["status"])
        self.assertEqual([], backend.calls)
        self.assertEqual(1, result["stats"]["queries_skipped"])
        self.assertIn("site:mp.weixin.qq.com/s", result["query_plan"][0]["query"])


if __name__ == "__main__":
    unittest.main()
