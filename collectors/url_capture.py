from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse
import copy
import json
import re

import trafilatura
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
SESSIONS = ROOT / "sessions"
LAST_CAPTURE_DIAGNOSTICS: List[Dict[str, Any]] = []


class CapturePaths(list[Path]):
    def __init__(self, values: Iterable[Path] = (), *, diagnostics: List[Dict[str, Any]] | None = None) -> None:
        super().__init__(values)
        self.diagnostics = diagnostics or []


def _display_platform_from_session(platform: str) -> str:
    mapping = {
        "xiaohongshu": "小红书",
        "zsxq": "知识星球",
        "weixin": "微信公众号",
        "wechat_open": "微信开放平台",
        "yuque": "语雀",
        "feishu": "飞书",
        "general": "通用网页",
    }
    return mapping.get(platform, platform)


def _auto_search_helpers():
    try:
        from auto_search import (
            MAX_PAGE_RESPONSE_BYTES,
            classify_source_url,
            credibility_from_tier,
            domain_to_platform,
            infer_source_pack_from_domain,
            is_allowed_page_content_type,
            normalize_source_url,
            validate_public_http_url,
        )
        return (
            credibility_from_tier,
            domain_to_platform,
            infer_source_pack_from_domain,
            normalize_source_url,
            classify_source_url,
            validate_public_http_url,
            is_allowed_page_content_type,
            MAX_PAGE_RESPONSE_BYTES,
        )
    except Exception:
        def _credibility_from_tier(tier: str) -> str:
            if tier == "T1":
                return "high"
            if tier == "T1/T2":
                return "medium_high"
            if tier == "T2":
                return "medium"
            if tier == "T2/T3":
                return "medium_low"
            return "low"

        def _domain_to_platform(domain: str) -> str:
            return domain or "未知来源"

        def _infer_source_pack_from_domain(domain: str):
            return "", {}

        def _normalize_source_url(url: str) -> str:
            text = (url or "").strip()
            return text if re.match(r"^https?://", text, re.I) else ""

        def _classify_source_url(url: str) -> dict[str, Any]:
            normalized = _normalize_source_url(url)
            domain = (urlparse(normalized).hostname or "").lower() if normalized else ""
            pack_key, pack_meta = _infer_source_pack_from_domain(domain)
            tier = str(pack_meta.get("tier") or "T3")
            return {
                "source_url": normalized,
                "canonical_url": normalized,
                "domain": domain,
                "source_pack": pack_key,
                "source_pack_label": str(pack_meta.get("label") or "未知来源"),
                "source_tier": tier,
                "credibility": _credibility_from_tier(tier),
                "platform": _domain_to_platform(domain),
            }

        def _validate_public_http_url(url: str, **_: Any) -> str:
            raise RuntimeError("URL 安全校验器不可用，已拒绝页面抓取")

        def _is_allowed_page_content_type(value: str | None) -> bool:
            return str(value or "").split(";", 1)[0].strip().lower() in {
                "text/html", "text/plain", "application/xhtml+xml",
            }

        return (
            _credibility_from_tier,
            _domain_to_platform,
            _infer_source_pack_from_domain,
            _normalize_source_url,
            _classify_source_url,
            _validate_public_http_url,
            _is_allowed_page_content_type,
            2_000_000,
        )


def guess_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")

    def matches(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if matches("xiaohongshu.com"):
        return "xiaohongshu"
    if matches("zsxq.com"):
        return "zsxq"
    if matches("open.weixin.qq.com") or matches("developers.weixin.qq.com"):
        return "wechat_open"
    if matches("yuque.com"):
        return "yuque"
    if matches("feishu.cn"):
        return "feishu"
    if matches("weixin.qq.com") or matches("weixin.sogou.com"):
        return "weixin"
    return "general"


def clean_text(html: str) -> str:
    text = trafilatura.extract(html, include_links=True, include_images=False, include_tables=False)
    if text:
        return text.strip()
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _try_export_wechat_article(_final_url: str, _raw_dir: Path) -> None:
    """Compatibility hook for optional integrations in downstream deployments.

    The public portfolio edition intentionally ships without executable,
    vendored third-party collectors. The built-in Playwright capture remains
    the only active path unless a downstream fork supplies a reviewed adapter.
    """

    return None


def _normalize_capture_input(item: str | Dict[str, Any]) -> Dict[str, Any]:
    (
        credibility_from_tier,
        domain_to_platform,
        infer_source_pack_from_domain,
        normalize_source_url,
        classify_source_url,
        _validate_public_http_url,
        _is_allowed_page_content_type,
        _max_page_response_bytes,
    ) = _auto_search_helpers()

    if isinstance(item, str):
        requested_url = normalize_source_url(item)
        observed = classify_source_url(requested_url)
        session_platform = guess_platform(requested_url)
        return {
            **observed,
            "requested_url": requested_url,
            "source_url": requested_url,
            "canonical_url": "",
            "session_platform": session_platform,
            "provider": "",
            "providers": [],
            "retrieval_providers": [],
            "provider_count": 0,
            "retrieval_provider_count": 0,
            "provider_count_kind": "retrieval_channels",
            "independent_source_count": 1 if requested_url else 0,
            "independently_corroborated": False,
            "query": "",
            "query_hits": [],
            "discovery_source_pack": "",
            "discovery_source_pack_label": "",
            "discovery_source_tier": "",
            "published_at": None,
            "summary": "",
            "discovery_source": "manual_url",
        }

    row = copy.deepcopy(item)
    requested_url = normalize_source_url(str(row.get("requested_url") or row.get("source_url") or row.get("url") or ""))
    observed = classify_source_url(requested_url)
    session_platform = guess_platform(requested_url)
    provider = str(row.get("provider") or "")
    providers = [x for x in (row.get("retrieval_providers") or row.get("providers") or ([provider] if provider else [])) if x]
    return {
        **observed,
        "requested_url": requested_url,
        "source_url": requested_url,
        "canonical_url": "",
        "session_platform": session_platform,
        "provider": provider,
        "providers": providers,
        "retrieval_providers": providers,
        "provider_count": int(row.get("retrieval_provider_count") or row.get("provider_count") or len(providers)),
        "retrieval_provider_count": int(row.get("retrieval_provider_count") or row.get("provider_count") or len(providers)),
        "provider_count_kind": "retrieval_channels",
        "independent_source_count": int(row.get("independent_source_count") or (1 if requested_url else 0)),
        "independently_corroborated": bool(row.get("independently_corroborated")),
        "query": str(row.get("query") or ""),
        "query_hits": [x for x in (row.get("query_hits") or ([row["query"]] if row.get("query") else [])) if x],
        "discovery_source_pack": str(row.get("discovery_source_pack") or ""),
        "discovery_source_pack_label": str(row.get("discovery_source_pack_label") or ""),
        "discovery_source_tier": str(row.get("discovery_source_tier") or ""),
        "published_at": row.get("published_at"),
        "summary": str(row.get("summary") or ""),
        "discovery_source": str(row.get("discovery_source") or "auto_search"),
    }


def _canonical_capture_metadata(item: Dict[str, Any], final_url: str) -> Dict[str, Any]:
    (
        _credibility_from_tier,
        _domain_to_platform,
        _infer_source_pack_from_domain,
        _normalize_source_url,
        classify_source_url,
        validate_public_http_url,
        _is_allowed_page_content_type,
        _max_page_response_bytes,
    ) = _auto_search_helpers()
    canonical_url = validate_public_http_url(final_url)
    observed = classify_source_url(canonical_url)
    return {
        **observed,
        "requested_url": item.get("requested_url", ""),
        "canonical_url": canonical_url,
        "final_url": canonical_url,
        "source_url": canonical_url,
        "discovery_source_pack": item.get("discovery_source_pack", ""),
        "discovery_source_pack_label": item.get("discovery_source_pack_label", ""),
        "discovery_source_tier": item.get("discovery_source_tier", ""),
        "source_fields_locked": True,
        "quote_match_required": True,
    }


def _install_navigation_guard(context: BrowserContext) -> None:
    *_, validate_public_http_url, _is_allowed_page_content_type, _max_page_response_bytes = _auto_search_helpers()
    checked_origins: dict[str, bool] = {}

    def guard(route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if parsed.scheme.lower() not in {"http", "https"}:
            route.continue_()
            return
        try:
            origin = f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}:{parsed.port or ''}"
        except ValueError:
            route.abort("blockedbyclient")
            return
        allowed = checked_origins.get(origin)
        if allowed is None:
            try:
                validate_public_http_url(request.url)
                allowed = True
            except Exception:
                allowed = False
            checked_origins[origin] = allowed
        if not allowed:
            route.abort("blockedbyclient")
            return
        route.continue_()

    context.route("**/*", guard)


def _validate_navigation_response(response: Any) -> None:
    *_, is_allowed_page_content_type, max_page_response_bytes = _auto_search_helpers()
    if response is None:
        raise ValueError("页面导航未返回可验证响应")
    headers = response.headers or {}
    content_type = headers.get("content-type", "")
    if not is_allowed_page_content_type(content_type):
        raise ValueError(f"不支持的页面类型: {content_type or 'missing'}")
    content_length = headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError):
            declared_length = 0
        if declared_length > max_page_response_bytes:
            raise ValueError("页面响应超过大小限制")


def capture_urls(urls: Iterable[str | Dict[str, Any]], raw_dir: Path | None = None) -> CapturePaths:
    raw_dir = raw_dir or RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    normalized_items = [_normalize_capture_input(item) for item in urls if item]
    normalized_items = [
        item for item in normalized_items
        if item.get("requested_url") and not str(item["requested_url"]).strip().startswith("#")
    ]
    if not normalized_items:
        LAST_CAPTURE_DIAGNOSTICS[:] = []
        return CapturePaths()
    *_, validate_public_http_url, _is_allowed_page_content_type, max_page_response_bytes = _auto_search_helpers()
    records: List[Path] = []
    diagnostics: List[Dict[str, Any]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            contexts: Dict[str, BrowserContext] = {}

            def get_context(platform: str) -> BrowserContext:
                if platform in contexts:
                    return contexts[platform]
                state_path = SESSIONS / f"{platform}.json"
                kwargs = {"storage_state": str(state_path)} if state_path.exists() else {}
                contexts[platform] = browser.new_context(**kwargs)
                _install_navigation_guard(contexts[platform])
                return contexts[platform]

            for idx, item in enumerate(normalized_items, start=1):
                url = str(item["requested_url"])
                page = None
                try:
                    validate_public_http_url(url)
                    session_platform = item["session_platform"]
                    state_path = SESSIONS / f"{session_platform}.json"
                    context = get_context(session_platform)
                    page = context.new_page()
                    response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
                    _validate_navigation_response(response)
                    page.wait_for_timeout(2500)
                    canonical = _canonical_capture_metadata(item, page.url)
                    html = page.content()
                    if len(html.encode("utf-8", "ignore")) > max_page_response_bytes:
                        raise ValueError("页面内容超过大小限制")
                    text = clean_text(html)
                    title = page.title()
                    final_url = canonical["canonical_url"]

                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    stem = f"{ts}_{idx:03d}"
                    screenshot_path: Path | None = raw_dir / f"{stem}.png"
                    screenshot_error = ""
                    try:
                        page.screenshot(path=str(screenshot_path), full_page=True)
                    except Exception as exc:
                        screenshot_error = f"{type(exc).__name__}: {exc}"[:300]
                        screenshot_path = None
                    payload = {
                        **canonical,
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "session_platform": session_platform,
                        "title": title,
                        "text": text[:50000],
                        "html_sha256": sha256(html.encode("utf-8", "ignore")).hexdigest(),
                        "screenshot": str(screenshot_path) if screenshot_path is not None else "",
                        "screenshot_error": screenshot_error,
                        "provider": item["provider"],
                        "providers": item["providers"],
                        "retrieval_providers": item["retrieval_providers"],
                        "provider_count": item["provider_count"],
                        "retrieval_provider_count": item["retrieval_provider_count"],
                        "provider_count_kind": "retrieval_channels",
                        "independent_source_count": item["independent_source_count"],
                        "independently_corroborated": item["independently_corroborated"],
                        "query": item["query"],
                        "query_hits": item["query_hits"],
                        "published_at": item["published_at"],
                        "summary": item["summary"],
                        "discovery_source": item["discovery_source"],
                        "collector_method": "logged_in_browser" if state_path.exists() else "browser_capture",
                        "session_state_used": state_path.exists(),
                        "session_state_path": str(state_path) if state_path.exists() else "",
                    }
                    record_path = raw_dir / f"{stem}.json"
                    temporary_path = record_path.with_suffix(".json.tmp")
                    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    temporary_path.replace(record_path)
                    records.append(record_path)
                    diagnostics.append(
                        {
                            "requested_url": url,
                            "canonical_url": final_url,
                            "status": "ok",
                            "raw_record": str(record_path),
                            "screenshot_error": screenshot_error,
                        }
                    )
                except Exception as exc:
                    diagnostics.append(
                        {
                            "requested_url": url,
                            "status": "error",
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        }
                    )
                finally:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass

            for context in contexts.values():
                try:
                    context.close()
                except Exception:
                    pass
            browser.close()
    except Exception as exc:
        diagnostics.append(
            {
                "requested_url": "",
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "scope": "browser_startup",
            }
        )

    LAST_CAPTURE_DIAGNOSTICS[:] = diagnostics
    return CapturePaths(records, diagnostics=diagnostics)
