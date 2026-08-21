from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import auto_search
from collectors import url_capture


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        url: str = "https://8.8.8.8/page",
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 0):
        del chunk_size
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class UrlSafetyTests(unittest.TestCase):
    def test_direct_private_loopback_and_link_local_addresses_are_rejected(self) -> None:
        blocked = (
            "http://127.0.0.1/admin",
            "http://10.0.0.5/data",
            "http://172.16.0.1/data",
            "http://192.168.1.5/data",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/",
            "http://localhost/",
        )
        for url in blocked:
            with self.subTest(url=url), self.assertRaises(ValueError):
                auto_search.validate_public_http_url(url)

    def test_hostname_resolving_to_private_address_is_rejected(self) -> None:
        resolved = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch.object(auto_search.socket, "getaddrinfo", return_value=resolved):
            with self.assertRaises(ValueError):
                auto_search.validate_public_http_url("https://public-looking.example/path")

    def test_redirect_target_is_revalidated_before_second_request(self) -> None:
        response = FakeResponse(
            status_code=302,
            headers={"Location": "http://127.0.0.1/private"},
            url="https://8.8.8.8/start",
        )
        getter = Mock(return_value=response)
        with patch.object(auto_search.requests, "get", getter):
            with self.assertRaises(ValueError):
                auto_search._fetch_public_page("https://8.8.8.8/start", {})
        self.assertEqual(getter.call_count, 1)
        self.assertTrue(response.closed)

    def test_non_text_and_oversized_responses_are_rejected(self) -> None:
        image = FakeResponse(
            headers={"Content-Type": "image/png"},
            chunks=[b"not a page"],
        )
        with patch.object(auto_search.requests, "get", return_value=image):
            with self.assertRaisesRegex(ValueError, "页面类型"):
                auto_search._fetch_public_page("https://8.8.8.8/page", {})

        oversized = FakeResponse(
            headers={
                "Content-Type": "text/html",
                "Content-Length": str(auto_search.MAX_PAGE_RESPONSE_BYTES + 1),
            },
        )
        with patch.object(auto_search.requests, "get", return_value=oversized):
            with self.assertRaisesRegex(ValueError, "大小限制"):
                auto_search._fetch_public_page("https://8.8.8.8/page", {})


class DiscoveryEvidenceBoundaryTests(unittest.TestCase):
    def test_query_pack_is_discovery_metadata_not_observed_source_tier(self) -> None:
        record = auto_search._search_record_from_result(
            {
                "title": "搜索结果",
                "summary": "摘要",
                "source_url": "https://unclassified.example/article",
                "domain": "unclassified.example",
                "published_at": "",
            },
            {
                "query": '"某公司" 官网',
                "source_pack": "official",
            },
            "brave",
        )
        self.assertEqual(record["source_tier"], "T3")
        self.assertEqual(record["source_pack"], "")
        self.assertEqual(record["discovery_source_pack"], "official")
        self.assertEqual(record["discovery_source_tier"], "T1")
        self.assertTrue(record["discovery_only"])
        self.assertFalse(record["evidence_eligible"])

    def test_multi_engine_retrieval_is_not_independent_corroboration(self) -> None:
        base = {
            "title": "同一页面",
            "source_url": "https://example.com/same",
            "domain": "example.com",
            "query": "q",
            "discovery_source_pack": "news",
            "discovery_source_pack_label": "媒体",
            "discovery_source_tier": "T2",
        }
        records = auto_search.dedupe_source_records([
            {**base, "provider": "brave"},
            {**base, "provider": "serper"},
        ])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["retrieval_provider_count"], 2)
        self.assertTrue(records[0]["multi_provider_retrieval"])
        self.assertEqual(records[0]["independent_source_count"], 1)
        self.assertFalse(records[0]["independently_corroborated"])
        self.assertFalse(records[0]["cross_validated"])

    def test_search_summary_without_fetched_body_never_enters_extractor(self) -> None:
        source = {
            "source_url": "https://example.com/result",
            "canonical_url": "",
            "summary": "搜索引擎摘要声称公司已经量产",
            "title": "摘要标题",
            "evidence_eligible": False,
            "discovery_only": True,
        }
        with patch.object(auto_search.subprocess, "run") as runner:
            evidence = auto_search.extract_evidence_from_sources([source], "芯片")
        self.assertEqual(evidence, [])
        runner.assert_not_called()

    def test_extractor_failure_does_not_create_positive_fallback(self) -> None:
        source = self._eligible_source()
        error = subprocess.CalledProcessError(1, ["claude"])
        with patch.object(auto_search.subprocess, "run", side_effect=error):
            evidence = auto_search.extract_evidence_from_sources([source], "芯片")
        self.assertEqual(evidence, [])
        self.assertEqual(source["evidence_status"], "extractor_failed")

    def test_source_fields_are_locked_and_quote_must_match_body(self) -> None:
        source = self._eligible_source()
        model_item = {
            "entity": "示例科技",
            "claim_type": "product_signal",
            "stance": "positive",
            "source_tier": "primary_official",
            "importance": 5,
            "source_url": "https://attacker.invalid/fake",
            "source_title": "伪造标题",
            "quote": "示例科技已经完成首批量产交付",
            "published_at": "2099-01-01",
            "platform": "伪造平台",
            "tags": ["量产"],
        }
        output = Mock(stdout=json.dumps({"structured_output": {"evidence": [model_item]}}))
        with patch.object(auto_search.subprocess, "run", return_value=output):
            evidence = auto_search.extract_evidence_from_sources([source], "芯片")
        self.assertEqual(len(evidence), 1)
        item = evidence[0]
        self.assertEqual(item["source_url"], source["canonical_url"])
        self.assertEqual(item["source_title"], source["fetched_title"])
        self.assertEqual(item["published_at"], source["published_at"])
        self.assertEqual(item["platform"], source["platform"])
        self.assertEqual(item["source_tier"], "social_post")
        self.assertEqual(item["quote"], "示例科技已经完成首批量产交付")
        self.assertTrue(item["quote_verified"])
        self.assertTrue(item["entity_verified"])
        self.assertFalse(item["independently_corroborated"])

        bad_item = {**model_item, "quote": "正文中不存在的虚构引用"}
        bad_output = Mock(stdout=json.dumps({"structured_output": {"evidence": [bad_item]}}))
        with patch.object(auto_search.subprocess, "run", return_value=bad_output):
            rejected = auto_search.extract_evidence_from_sources([self._eligible_source()], "芯片")
        self.assertEqual(rejected, [])

        wrong_entity = {**model_item, "entity": "正文不存在科技"}
        wrong_entity_output = Mock(stdout=json.dumps({"structured_output": {"evidence": [wrong_entity]}}))
        with patch.object(auto_search.subprocess, "run", return_value=wrong_entity_output):
            rejected_entity = auto_search.extract_evidence_from_sources([self._eligible_source()], "芯片")
        self.assertEqual(rejected_entity, [])

    def test_run_auto_search_preserves_pipeline_metadata(self) -> None:
        collected = {
            "profile": "standard",
            "query_plan": [{"query": "q"}],
            "records": [{"source_url": "https://example.com"}],
            "providers_used": ["brave"],
            "provider_status": {"brave": {"available": True}},
            "coverage": {"result_count": 1},
        }
        with (
            patch.object(auto_search, "collect_sources", return_value=collected),
            patch.object(auto_search, "extract_evidence_from_sources", return_value=[{"entity": "A"}]),
        ):
            result = auto_search.run_auto_search("芯片", intensity="standard")
        self.assertEqual(result["evidence"], [{"entity": "A"}])
        self.assertEqual(result["sources"], collected["records"])
        self.assertEqual(result["providers_used"], ["brave"])
        self.assertEqual(result["query_plan"], collected["query_plan"])
        self.assertEqual(result["source_coverage"], collected["coverage"])

    @staticmethod
    def _eligible_source() -> dict:
        body = "示例科技已经完成首批量产交付，并披露客户验收情况。" * 4
        return {
            "source_url": "https://mp.weixin.qq.com/s/example",
            "canonical_url": "https://mp.weixin.qq.com/s/example",
            "domain": "mp.weixin.qq.com",
            "source_pack": "wechat",
            "source_tier": "T2/T3",
            "discovery_source_pack": "news",
            "discovery_source_tier": "T2",
            "platform": "微信公众号",
            "fetched_title": "示例科技量产公告",
            "title": "搜索标题",
            "clean_text": body,
            "published_at": "2026-08-20",
            "evidence_eligible": True,
            "discovery_only": False,
            "provider": "brave",
            "providers": ["brave", "serper"],
            "retrieval_providers": ["brave", "serper"],
            "provider_count": 2,
            "retrieval_provider_count": 2,
            "independent_source_count": 1,
            "independently_corroborated": False,
        }


class CaptureCanonicalizationTests(unittest.TestCase):
    def test_one_bad_url_does_not_abort_the_rest_of_the_batch(self) -> None:
        class FakePage:
            url = "https://example.org/good"

            def goto(self, *_args, **_kwargs):
                return object()

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

            def content(self) -> str:
                return "<html><title>示例</title><body>示例科技完成产品验证。</body></html>"

            def title(self) -> str:
                return "示例科技公告"

            def screenshot(self, **_kwargs) -> None:
                return None

            def close(self) -> None:
                return None

        class FakeContext:
            def new_page(self):
                return FakePage()

            def close(self) -> None:
                return None

        class FakeBrowser:
            def new_context(self, **_kwargs):
                return FakeContext()

            def close(self) -> None:
                return None

        class FakeChromium:
            def launch(self, **_kwargs):
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakeManager:
            def __enter__(self):
                return FakePlaywright()

            def __exit__(self, *_args) -> None:
                return None

        def validate(url: str, **_kwargs) -> str:
            if "127.0.0.1" in url:
                raise ValueError("private address")
            return url

        def classify(url: str) -> dict:
            return {
                "source_url": url,
                "canonical_url": url,
                "domain": "example.org",
                "source_pack": "",
                "source_pack_label": "未知来源",
                "source_tier": "T3",
                "credibility": "low",
                "platform": "example.org",
            }

        helpers = (
            lambda _tier: "low",
            lambda domain: domain,
            lambda _domain: ("", {}),
            lambda url: url,
            classify,
            validate,
            lambda _value: True,
            2_000_000,
        )
        canonical = {
            "requested_url": "https://example.org/good",
            "source_url": "https://example.org/good",
            "canonical_url": "https://example.org/good",
            "domain": "example.org",
            "source_pack": "",
            "source_tier": "T3",
            "platform": "example.org",
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(url_capture, "sync_playwright", return_value=FakeManager()),
                patch.object(url_capture, "_auto_search_helpers", return_value=helpers),
                patch.object(url_capture, "_install_navigation_guard"),
                patch.object(url_capture, "_validate_navigation_response"),
                patch.object(url_capture, "_canonical_capture_metadata", return_value=canonical),
                patch.object(url_capture, "clean_text", return_value="示例科技完成产品验证。"),
                patch.object(url_capture, "_try_export_wechat_article", return_value=None),
            ):
                result = url_capture.capture_urls(
                    ["http://127.0.0.1/private", "https://example.org/good"],
                    Path(directory),
                )

        self.assertEqual(len(result), 1)
        self.assertEqual([item["status"] for item in result.diagnostics], ["error", "ok"])

    def test_browser_route_guard_blocks_private_subresources(self) -> None:
        class FakeContext:
            handler = None

            def route(self, pattern, handler) -> None:
                self.pattern = pattern
                self.handler = handler

        class FakeRoute:
            def __init__(self, url: str) -> None:
                self.request = Mock(url=url)
                self.aborted = False
                self.continued = False

            def abort(self, error_code: str) -> None:
                self.aborted = error_code == "blockedbyclient"

            def continue_(self) -> None:
                self.continued = True

        context = FakeContext()
        url_capture._install_navigation_guard(context)
        route = FakeRoute("http://169.254.169.254/latest/meta-data")
        context.handler(route)
        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)

    def test_final_url_reclassifies_source_and_preserves_discovery_metadata(self) -> None:
        item = url_capture._normalize_capture_input(
            {
                "requested_url": "https://unclassified.example/redirect",
                "source_url": "https://unclassified.example/redirect",
                "discovery_source_pack": "official",
                "discovery_source_pack_label": "官方/监管/交易所",
                "discovery_source_tier": "T1",
                "provider": "brave",
            }
        )
        final_url = "https://www.gov.cn/zhengce/example"
        with patch.object(auto_search, "validate_public_http_url", return_value=final_url):
            canonical = url_capture._canonical_capture_metadata(item, final_url)
        self.assertEqual(canonical["requested_url"], item["requested_url"])
        self.assertEqual(canonical["source_url"], final_url)
        self.assertEqual(canonical["canonical_url"], final_url)
        self.assertEqual(canonical["domain"], "www.gov.cn")
        self.assertEqual(canonical["source_pack"], "official")
        self.assertEqual(canonical["source_tier"], "T1")
        self.assertEqual(canonical["discovery_source_pack"], "official")
        self.assertTrue(canonical["source_fields_locked"])
        self.assertTrue(canonical["quote_match_required"])


if __name__ == "__main__":
    unittest.main()
