"""Credential-free discovery of public WeChat article URLs.

This module deliberately stops at discovery. Search titles and snippets are kept
as routing metadata, never promoted to article-body evidence. It neither opens
WeChat pages nor reuses browser/login state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit
import inspect
import json
import os
import re
import shutil
import subprocess

import requests

from wechat_source_pool import canonicalize_wechat_url


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "weekly_radar_config.json"
DEFAULT_RESULTS_PER_QUERY = 8
DEFAULT_MAX_QUERIES = 30
MAX_QUERY_COUNT = 200
MAX_RESULT_COUNT = 50
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_DIRECT_URL_KEYS = ("url", "source_url", "link", "href", "canonical_url")
_TEXT_KEYS = ("text", "snippet", "summary", "description", "content")
_CONTAINER_KEYS = ("results", "items", "records", "data", "output", "content")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_WECHAT_HINT_RE = re.compile(r"mp(?:\.|%2e)weixin(?:\.|%2e)qq(?:\.|%2e)com", re.IGNORECASE)
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(auth[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|token|cookie|"
    r"pass[-_ ]?ticket|uin|secret|password|authorization)\b\s*[:=]\s*[^\s,;]+"
)
_TRAILING_URL_PUNCTUATION = "\t\r\n .,;:!?\uff0c\u3002\uff1b\uff1a\uff01\uff1f\u3001)]}\u3009\u300b\u300d\u300f"

_FALLBACK_SECTORS: dict[str, list[str]] = {
    "\u534a\u5bfc\u4f53\u4e0e\u5149\u7535": ["\u534a\u5bfc\u4f53", "\u82af\u7247", "\u5149\u901a\u4fe1", "\u5148\u8fdb\u5c01\u88c5"],
    "\u5177\u8eab\u667a\u80fd": ["\u673a\u5668\u4eba", "\u5177\u8eab\u667a\u80fd", "\u4e16\u754c\u6a21\u578b", "\u7a7a\u95f4\u667a\u80fd"],
    "\u9ad8\u7aef\u88c5\u5907": ["\u9ad8\u7aef\u88c5\u5907", "\u9ad8\u7aef\u4eea\u5668", "\u91cf\u5b50", "\u6838\u805a\u53d8"],
    "\u751f\u547d\u79d1\u6280": ["AI\u5236\u836f", "\u751f\u7269\u533b\u836f", "\u533b\u7597\u5668\u68b0", "\u4e34\u5e8a"],
    "\u65b0\u6750\u6599": ["\u65b0\u6750\u6599", "\u7279\u79cd\u6750\u6599", "\u590d\u5408\u6750\u6599", "\u7a00\u571f"],
}

_FALLBACK_EVENTS: dict[str, list[str]] = {
    "\u5ba2\u6237\u9a8c\u8bc1": ["\u5ba2\u6237\u8ba4\u8bc1", "\u5b9a\u70b9", "\u4e2d\u6807", "\u8ba2\u5355", "\u9996\u5355"],
    "\u89c4\u6a21\u5316\u4e0e\u4ea4\u4ed8\u80fd\u529b": ["\u91cf\u4ea7", "\u6295\u4ea7", "\u4ea4\u4ed8", "\u51fa\u8d27", "\u6269\u4ea7"],
    "\u76d1\u7ba1\u6216\u4e34\u5e8a\u91cc\u7a0b\u7891": ["\u83b7\u6279", "\u4e34\u5e8a", "\u6ce8\u518c\u8bc1", "\u5907\u6848"],
    "\u6280\u672f\u53bb\u98ce\u9669": ["\u6d41\u7247", "\u6280\u672f\u7a81\u7834", "\u9a8c\u8bc1", "\u6837\u673a", "\u9996\u5957"],
    "\u8d44\u672c\u4e0e\u4ea7\u4e1a\u8d44\u6e90\u5230\u4f4d": ["\u878d\u8d44", "\u589e\u8d44", "\u6218\u7565\u6295\u8d44", "\u5b8c\u6210\u4ea4\u5272"],
    "\u9000\u51fa\u53ef\u89c1\u6027": ["\u4e0a\u5e02\u8f85\u5bfc", "\u9012\u8868", "IPO", "\u5e76\u8d2d", "\u6302\u724c"],
}


class SearchBackend(Protocol):
    """Minimal injectable backend contract used by :func:`discover_wechat_articles`."""

    provider: str

    def search(self, query: str, *, limit: int) -> Any:
        """Return search rows or an envelope containing a ``results`` list."""


class SearchBackendError(RuntimeError):
    """A safe, structured search transport failure."""

    def __init__(self, code: str, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable


def _text(value: Any, limit: int = 1_000) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", unescape(str(value))).strip()[:limit]


def _safe_error(value: Any) -> str:
    text = _text(value, 500)
    if not text:
        return "unknown search backend failure"
    return _CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)


def _optional_metadata(value: Any, limit: int) -> str:
    text = _text(value, limit)
    return "" if text.casefold() in {"n/a", "na", "none", "null", "unknown", "未知"} else text


def _safe_url_hint(value: Any) -> str:
    """Return host/path only so rejected redirect parameters cannot leak secrets."""

    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return "invalid-url"
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path[:160]
    if parsed.scheme.lower() in {"http", "https"} and host:
        return f"{parsed.scheme.lower()}://{host}{path}"
    return "invalid-url"


def _load_dimensions(config_path: str | Path | None = None) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    sectors = {key: list(value) for key, value in _FALLBACK_SECTORS.items()}
    events = {key: list(value) for key, value in _FALLBACK_EVENTS.items()}
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return sectors, events
    if not isinstance(payload, Mapping):
        return sectors, events

    configured_sectors = payload.get("sector_keywords")
    if isinstance(configured_sectors, Mapping):
        parsed_sectors = _normalize_dimension_input(configured_sectors)
        if parsed_sectors:
            sectors = parsed_sectors

    configured_events = payload.get("core_variables")
    if isinstance(configured_events, Sequence) and not isinstance(configured_events, (str, bytes, bytearray)):
        parsed_events: dict[str, list[str]] = {}
        for item in configured_events:
            if not isinstance(item, Mapping):
                continue
            name = _text(item.get("name"), 100)
            terms = _normalize_terms(item.get("keywords"))
            if name:
                parsed_events[name] = terms or [name]
        if parsed_events:
            events = parsed_events
    return sectors, events


def _read_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _boolean(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def load_discovery_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load and bound the optional ``wechat_discovery`` configuration block.

    Explicit discovery queries win. When they are absent, existing
    ``google_news_queries`` are reused as discovery seeds and constrained to the
    exact WeChat article host/path by :func:`build_config_query_plan`.
    """

    root = _read_config(config_path)
    raw = root.get("wechat_discovery")
    block = dict(raw) if isinstance(raw, Mapping) else {}
    configured_queries = block.get("queries")
    query_source = "generated_matrix"
    query_rows: list[dict[str, str]] = []
    if isinstance(configured_queries, Sequence) and not isinstance(configured_queries, (str, bytes, bytearray)):
        for index, item in enumerate(configured_queries, start=1):
            if isinstance(item, Mapping):
                query = _text(item.get("query"), 2_000)
                name = _text(item.get("name"), 150) or f"wechat-{index}"
            else:
                query = _text(item, 2_000)
                name = f"wechat-{index}"
            if query:
                query_rows.append({"name": name, "query": query})
        if query_rows:
            query_source = "wechat_discovery.queries"
    if not query_rows:
        google_queries = root.get("google_news_queries")
        if isinstance(google_queries, Sequence) and not isinstance(google_queries, (str, bytes, bytearray)):
            for index, item in enumerate(google_queries, start=1):
                if not isinstance(item, Mapping):
                    continue
                query = _text(item.get("query"), 2_000)
                if query:
                    query_rows.append(
                        {
                            "name": _text(item.get("name"), 150) or f"google-news-{index}",
                            "query": query,
                        }
                    )
        if query_rows:
            query_source = "google_news_queries"

    settings = {
        "enabled": _boolean(block.get("enabled"), True),
        "provider": _text(block.get("provider"), 80) or "mcporter_exa",
        "interval_hours": _bounded_float(block.get("interval_hours"), 24.0, 0.25, 720.0),
        "max_queries_per_run": _bounded_int(
            block.get("max_queries_per_run"), DEFAULT_MAX_QUERIES, 1, MAX_QUERY_COUNT
        ),
        "num_results_per_query": _bounded_int(
            block.get("num_results_per_query"), DEFAULT_RESULTS_PER_QUERY, 1, MAX_RESULT_COUNT
        ),
        "max_new_urls_per_run": _bounded_int(block.get("max_new_urls_per_run"), 100, 1, 10_000),
        "timeout_seconds": _bounded_float(block.get("timeout_seconds"), 45.0, 1.0, 300.0),
        "window_days": _bounded_int(root.get("window_days"), 7, 1, 365),
        "queries": query_rows,
        "query_source": query_source,
    }
    if os.getenv("DEALSCOPE_PUBLIC_EXA_MCP", "").strip() == "1":
        settings.update(
            provider="exa_mcp_http",
            interval_hours=max(6.0, float(settings["interval_hours"])),
            max_queries_per_run=min(3, int(settings["max_queries_per_run"])),
            num_results_per_query=min(8, int(settings["num_results_per_query"])),
            max_new_urls_per_run=min(24, int(settings["max_new_urls_per_run"])),
            timeout_seconds=min(30.0, float(settings["timeout_seconds"])),
        )
    return settings


def _calendar_date(value: Any = None) -> date:
    if value is None:
        return datetime.now(SHANGHAI_TZ).date()
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=SHANGHAI_TZ)
        return moment.astimezone(SHANGHAI_TZ).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip()[:10])
    raise ValueError("as_of must be date, datetime, YYYY-MM-DD, or None")


def build_config_query_plan(settings: Mapping[str, Any], *, as_of: Any = None) -> list[dict[str, Any]]:
    """Render configured/derived queries and their supported date placeholders."""

    current = _calendar_date(as_of)
    replacements = {
        "{year}": str(current.year),
        "{month}": f"{current.month:02d}",
        "{window_days}": str(_bounded_int(settings.get("window_days"), 7, 1, 365)),
    }
    budget = _bounded_int(settings.get("max_queries_per_run"), DEFAULT_MAX_QUERIES, 1, MAX_QUERY_COUNT)
    rows = settings.get("queries")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return []
    plan: list[dict[str, Any]] = []
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, Mapping):
            continue
        query = _text(item.get("query"), 2_000)
        for placeholder, replacement in replacements.items():
            query = query.replace(placeholder, replacement)
        if not query:
            continue
        if "site:mp.weixin.qq.com/s" not in query.casefold():
            query = "site:mp.weixin.qq.com/s " + query
        name = _text(item.get("name"), 150) or f"wechat-{index}"
        plan.append(
            {
                "query_id": f"wechat-config:{len(plan) + 1:03d}",
                "name": name,
                "query": query,
                "sector": name,
                "event": "configured_discovery",
                "sector_terms": [],
                "event_terms": [],
                "source_scope": "mp.weixin.qq.com/s",
                "discovery_only": True,
                "query_source": _text(settings.get("query_source"), 100) or "configured",
            }
        )
        if len(plan) >= budget:
            break
    return plan


def _normalize_terms(value: Any) -> list[str]:
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        values = value
    else:
        values = []
    terms: list[str] = []
    seen: set[str] = set()
    for raw in values:
        term = _text(raw, 80).replace('"', "")
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def _normalize_dimension_input(value: Any) -> dict[str, list[str]]:
    dimensions: dict[str, list[str]] = {}
    if isinstance(value, Mapping):
        items: Iterable[tuple[Any, Any]] = value.items()
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        items = ((item, [item]) for item in value)
    else:
        return dimensions
    for raw_name, raw_terms in items:
        name = _text(raw_name, 100)
        if not name:
            continue
        terms = _normalize_terms(raw_terms)
        dimensions[name] = terms or [name]
    return dimensions


def _query_expression(name: str, terms: Sequence[str]) -> str:
    selected: list[str] = []
    for value in (name, *terms):
        text = _text(value, 80).replace('"', "")
        if text and text.casefold() not in {item.casefold() for item in selected}:
            selected.append(text)
        if len(selected) >= 6:
            break
    return "(" + " OR ".join(f'\"{item}\"' for item in selected) + ")"


def build_query_plan(
    sectors: Mapping[str, Sequence[str] | str] | Sequence[str] | None = None,
    events: Mapping[str, Sequence[str] | str] | Sequence[str] | None = None,
    *,
    max_queries: int = DEFAULT_MAX_QUERIES,
    config_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Build the deterministic sector-by-event WeChat discovery matrix."""

    default_sectors, default_events = _load_dimensions(config_path)
    sector_map = default_sectors if sectors is None else _normalize_dimension_input(sectors)
    event_map = default_events if events is None else _normalize_dimension_input(events)
    if not sector_map or not event_map:
        raise ValueError("at least one sector and one event are required")
    budget = max(1, min(int(max_queries), MAX_QUERY_COUNT))
    plan: list[dict[str, Any]] = []
    for sector, sector_terms in sector_map.items():
        for event, event_terms in event_map.items():
            query = (
                "site:mp.weixin.qq.com/s "
                f"{_query_expression(sector, sector_terms)} "
                f"{_query_expression(event, event_terms)}"
            )
            plan.append(
                {
                    "query_id": f"wechat:{len(plan) + 1:03d}",
                    "query": query,
                    "sector": sector,
                    "event": event,
                    "sector_terms": list(sector_terms),
                    "event_terms": list(event_terms),
                    "source_scope": "mp.weixin.qq.com/s",
                    "discovery_only": True,
                }
            )
            if len(plan) >= budget:
                return plan
    return plan


def _decoded_variants(value: Any) -> list[str]:
    raw = str(value or "").strip().strip("'\"")
    if not raw or len(raw) > 50_000 or any(character in raw for character in "\x00\r\n"):
        return []
    if re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", raw, re.IGNORECASE):
        return []
    variants: list[str] = []
    current = unescape(raw).replace("\\/", "/")
    for _ in range(3):
        if any(ord(character) < 32 or ord(character) == 127 for character in current):
            break
        if current not in variants:
            variants.append(current)
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return variants


def _url_candidates(value: Any) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for variant in _decoded_variants(value):
        possible = list(_URL_RE.findall(variant))
        try:
            parsed = urlsplit(variant)
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname
            and _URL_RE.fullmatch(variant)
        ):
            possible.insert(0, variant)
            for key, nested in parse_qsl(parsed.query, keep_blank_values=False):
                if key.casefold() in {"url", "target", "redirect", "redirect_url", "u"}:
                    possible.extend(_decoded_variants(nested))
        for raw in possible:
            cleaned = raw.strip().rstrip(_TRAILING_URL_PUNCTUATION)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                candidates.append(cleaned)
    return candidates


def canonicalize_discovered_url(value: Any) -> str:
    """Extract and canonicalize an exact public ``mp.weixin.qq.com/s`` URL."""

    for candidate in _url_candidates(value):
        try:
            parsed = urlsplit(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or host != "mp.weixin.qq.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        try:
            canonical = canonicalize_wechat_url(candidate)
        except ValueError:
            continue
        final = urlsplit(canonical)
        if final.hostname == "mp.weixin.qq.com" and (final.path == "/s" or final.path.startswith("/s/")):
            return canonical
    raise ValueError("no exact public mp.weixin.qq.com/s article URL found")


def _contains_wechat_hint(value: Any) -> bool:
    return bool(_WECHAT_HINT_RE.search(str(value or "")))


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _rows_from_text(value: str) -> list[dict[str, Any]]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        nested = json.loads(text)
    except json.JSONDecodeError:
        nested = None
    if nested is not None:
        return normalize_search_results(nested)

    rows: list[dict[str, Any]] = []
    used_urls: set[str] = set()
    # Exa's MCP text format uses repeated Title/URL/Published/Author blocks.
    # Parse those labels so the article library has a useful title/account
    # while keeping Highlights strictly as non-evidentiary search metadata.
    for block in re.split(r"\n\s*---+\s*\n", text):
        url_match = re.search(r"(?im)^URL:\s*(https?://\S+)", block)
        if not url_match:
            continue
        url = url_match.group(1).rstrip(_TRAILING_URL_PUNCTUATION)
        title_match = re.search(r"(?im)^Title:\s*(.+)$", block)
        author_match = re.search(r"(?im)^Author:\s*(.+)$", block)
        published_match = re.search(r"(?im)^Published:\s*(.+)$", block)
        highlights_match = re.search(r"(?ims)^Highlights:\s*(.+)$", block)
        row = {
            "title": _text(title_match.group(1), 300) if title_match else "",
            "url": url,
            "author": _optional_metadata(author_match.group(1), 200) if author_match else "",
            "search_published_at": _optional_metadata(published_match.group(1), 100) if published_match else "",
            "summary": _text(highlights_match.group(1), 1_000) if highlights_match else "",
        }
        rows.append(row)
        used_urls.add(url)
    markdown_links = re.findall(r"\[([^\]]{0,300})\]\((https?://[^)\s]+)\)", text, re.IGNORECASE)
    for title, url in markdown_links:
        if url not in used_urls:
            rows.append({"title": _text(title, 300), "url": url})
            used_urls.add(url)
    for url in _URL_RE.findall(text):
        cleaned = url.rstrip(_TRAILING_URL_PUNCTUATION)
        if cleaned not in used_urls:
            rows.append({"url": cleaned, "text": text[:1_000]})
            used_urls.add(cleaned)
    return rows


def normalize_search_results(payload: Any) -> list[dict[str, Any]]:
    """Normalize common search/MCPorter envelopes without treating text as evidence."""

    if payload is None:
        return []
    if isinstance(payload, str):
        return _rows_from_text(payload)
    if isinstance(payload, Mapping):
        if any(payload.get(key) not in (None, "") for key in _DIRECT_URL_KEYS):
            return [dict(payload)]
        for key in _CONTAINER_KEYS:
            child = payload.get(key)
            if child not in (None, ""):
                rows = normalize_search_results(child)
                if rows:
                    return rows
        for key in _TEXT_KEYS:
            child = payload.get(key)
            if isinstance(child, str):
                rows = _rows_from_text(child)
                if rows:
                    return rows
        return []
    if isinstance(payload, Iterable) and not isinstance(payload, (bytes, bytearray)):
        rows: list[dict[str, Any]] = []
        for item in payload:
            rows.extend(normalize_search_results(item))
        return rows
    return []


def parse_mcporter_output(output: str) -> list[dict[str, Any]]:
    """Parse JSON output first, then a conservative Markdown/plain-text fallback."""

    text = str(output or "").strip()
    if not text:
        return []
    try:
        return normalize_search_results(json.loads(text))
    except json.JSONDecodeError:
        return _rows_from_text(text)


class McporterExaBackend:
    """Exa search over MCPorter with OAuth/login prompts explicitly disabled."""

    provider = "exa"
    transport = "mcporter"

    def __init__(
        self,
        executable: str = "mcporter",
        *,
        timeout: float = 45.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.executable = (shutil.which(executable) or executable) if runner is None else executable
        self.timeout = max(1.0, float(timeout))
        self._runner = runner or subprocess.run

    def search(self, query: str, *, limit: int = DEFAULT_RESULTS_PER_QUERY) -> list[dict[str, Any]]:
        count = max(1, min(int(limit), MAX_RESULT_COUNT))
        arguments = json.dumps({"query": _text(query, 2_000), "numResults": count}, ensure_ascii=False)
        command = [
            self.executable,
            "call",
            "exa.web_search_exa",
            "--args",
            arguments,
            "--output",
            "json",
            "--no-oauth",
            "--timeout",
            str(int(self.timeout * 1_000)),
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout + 2,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise SearchBackendError("backend_unavailable", "mcporter executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise SearchBackendError("backend_timeout", "mcporter Exa search timed out", retriable=True) from exc
        except OSError as exc:
            raise SearchBackendError("backend_unavailable", _safe_error(exc)) from exc
        if completed.returncode != 0:
            detail = _safe_error(completed.stderr) if completed.stderr else f"mcporter exited with code {completed.returncode}"
            raise SearchBackendError("backend_failed", detail, retriable=True)
        return parse_mcporter_output(completed.stdout)


def _mcp_message(response: requests.Response) -> dict[str, Any]:
    if len(response.content) > 2 * 1024 * 1024:
        raise SearchBackendError("backend_response_too_large", "Exa MCP response exceeded 2MB")
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SearchBackendError("backend_failed", _safe_error(exc), retriable=True) from exc
    text = response.content.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if "application/json" in str(response.headers.get("Content-Type") or "").lower():
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SearchBackendError("backend_invalid_response", "Exa MCP returned invalid JSON") from exc
        return dict(payload) if isinstance(payload, Mapping) else {}
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            return dict(payload)
    raise SearchBackendError("backend_invalid_response", "Exa MCP returned no JSON-RPC message")


class ExaMcpHttpBackend:
    """Bounded, credential-free Exa MCP client for the public cloud profile."""

    provider = "exa"
    transport = "mcp_http"
    protocol_version = "2025-03-26"

    def __init__(
        self,
        endpoint: str = "https://mcp.exa.ai/mcp",
        *,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != "mcp.exa.ai":
            raise ValueError("Exa MCP endpoint must be https://mcp.exa.ai/mcp")
        self.endpoint = endpoint
        self.timeout = max(1.0, min(float(timeout), 45.0))
        self._session = session or requests.Session()
        self._session_id = ""
        self._request_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
            "User-Agent": "DealScope-Evidence-Radar/1.0",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _initialize(self) -> None:
        if self._session_id:
            return
        request_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "dealscope", "version": "1.0"},
            },
        }
        try:
            response = self._session.post(
                self.endpoint,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SearchBackendError("backend_unavailable", _safe_error(exc), retriable=True) from exc
        message = _mcp_message(response)
        if message.get("error") or not isinstance(message.get("result"), Mapping):
            raise SearchBackendError("backend_failed", _safe_error(message.get("error")))
        self._session_id = str(response.headers.get("mcp-session-id") or "").strip()
        if not self._session_id:
            raise SearchBackendError("backend_invalid_response", "Exa MCP omitted its session id")
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        try:
            acknowledged = self._session.post(
                self.endpoint,
                headers=self._headers(),
                json=notification,
                timeout=self.timeout,
            )
            if acknowledged.status_code not in {200, 202, 204}:
                _mcp_message(acknowledged)
        except requests.RequestException as exc:
            raise SearchBackendError("backend_unavailable", _safe_error(exc), retriable=True) from exc

    def search(self, query: str, *, limit: int = DEFAULT_RESULTS_PER_QUERY) -> list[dict[str, Any]]:
        self._initialize()
        count = max(1, min(int(limit), MAX_RESULT_COUNT))
        request_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {"query": _text(query, 2_000), "numResults": count},
            },
        }
        try:
            response = self._session.post(
                self.endpoint,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise SearchBackendError("backend_timeout", "Exa MCP search timed out", retriable=True) from exc
        except requests.RequestException as exc:
            raise SearchBackendError("backend_unavailable", _safe_error(exc), retriable=True) from exc
        message = _mcp_message(response)
        if message.get("error"):
            raise SearchBackendError("backend_failed", _safe_error(message.get("error")), retriable=True)
        result = message.get("result")
        if not isinstance(result, Mapping):
            raise SearchBackendError("backend_invalid_response", "Exa MCP returned no tool result")
        return normalize_search_results(result)


def _backend_provider(backend: Any) -> str:
    provider = _text(getattr(backend, "provider", ""), 80)
    if provider:
        return provider
    name = getattr(backend, "__name__", "") or backend.__class__.__name__
    return _text(name, 80) or "injected_search"


def _invoke_backend(backend: Any, query: str, limit: int) -> Any:
    search = getattr(backend, "search", None)
    if search is None and callable(backend):
        search = backend
    if not callable(search):
        raise SearchBackendError("invalid_backend", "search backend must be callable or expose search()")
    try:
        signature = inspect.signature(search)
    except (TypeError, ValueError):
        return search(query, limit=limit)
    parameters = signature.parameters.values()
    accepts_keyword = "limit" in signature.parameters or any(item.kind == item.VAR_KEYWORD for item in parameters)
    accepts_second_positional = any(item.kind == item.VAR_POSITIONAL for item in parameters) or sum(
        item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD) for item in parameters
    ) >= 2
    if accepts_keyword:
        return search(query, limit=limit)
    if accepts_second_positional:
        return search(query, limit)
    return search(query)


def _result_urls(row: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in _DIRECT_URL_KEYS:
        if row.get(key) not in (None, ""):
            values.append(row[key])
    for key in _TEXT_KEYS:
        if isinstance(row.get(key), str):
            values.append(row[key])
    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        for candidate in _url_candidates(value):
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def _discovery_timestamp(value: Any = None) -> str:
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
    else:
        text = _text(value, 100)
        if not text:
            raise ValueError("discovered_at must be a datetime or non-empty string")
        return text
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_plan(plan: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(plan, start=1):
        query = _text(item.get("query"), 2_000)
        if not query:
            continue
        normalized.append(
            {
                "query_id": _text(item.get("query_id"), 100) or f"wechat:{index:03d}",
                "name": _text(item.get("name"), 150),
                "query": query,
                "sector": _text(item.get("sector"), 100),
                "event": _text(item.get("event"), 100),
                "sector_terms": _normalize_terms(item.get("sector_terms")),
                "event_terms": _normalize_terms(item.get("event_terms")),
                "source_scope": "mp.weixin.qq.com/s",
                "discovery_only": True,
                "query_source": _text(item.get("query_source"), 100) or "injected_query_plan",
            }
        )
    if not normalized:
        raise ValueError("query_plan contains no valid query")
    return normalized[:MAX_QUERY_COUNT]


def discover_wechat_articles(
    sectors: Mapping[str, Sequence[str] | str] | Sequence[str] | None = None,
    events: Mapping[str, Sequence[str] | str] | Sequence[str] | None = None,
    *,
    search_backend: SearchBackend | Callable[..., Any] | None = None,
    query_plan: Sequence[Mapping[str, Any]] | None = None,
    max_queries: int | None = None,
    results_per_query: int | None = None,
    max_new_urls_per_run: int | None = None,
    config_path: str | Path | None = None,
    as_of: Any = None,
    discovered_at: Any = None,
    max_errors: int = 100,
) -> dict[str, Any]:
    """Run WeChat URL discovery and return only non-evidentiary search leads.

    Search backend failures are isolated per query. Duplicate URLs merge their
    query/provider provenance, but multiple retrieval providers never count as
    independent evidence corroboration.
    """

    settings = load_discovery_config(config_path)
    effective_max_queries = _bounded_int(
        max_queries,
        int(settings["max_queries_per_run"]),
        1,
        MAX_QUERY_COUNT,
    ) if max_queries is not None else int(settings["max_queries_per_run"])
    if query_plan is not None:
        plan = _normalize_plan(query_plan)[:effective_max_queries]
    elif sectors is not None or events is not None:
        plan = build_query_plan(
            sectors,
            events,
            max_queries=effective_max_queries,
            config_path=config_path,
        )
    else:
        configured_plan = build_config_query_plan(settings, as_of=as_of)
        plan = configured_plan[:effective_max_queries] or build_query_plan(
            max_queries=effective_max_queries,
            config_path=config_path,
        )
    per_query_limit = _bounded_int(
        results_per_query,
        int(settings["num_results_per_query"]),
        1,
        MAX_RESULT_COUNT,
    ) if results_per_query is not None else int(settings["num_results_per_query"])
    new_url_limit = _bounded_int(
        max_new_urls_per_run,
        int(settings["max_new_urls_per_run"]),
        1,
        10_000,
    ) if max_new_urls_per_run is not None else int(settings["max_new_urls_per_run"])
    run_timestamp = _discovery_timestamp(discovered_at)
    error_limit = max(0, min(int(max_errors), 1_000))

    def empty_stats(*, failed: int = 0, skipped: int = 0) -> dict[str, Any]:
        return {
            "queries_total": len(plan),
            "queries_succeeded": 0,
            "queries_failed": failed,
            "queries_skipped": skipped,
            "raw_results": 0,
            "url_candidates": 0,
            "accepted_hits": 0,
            "unique_results": 0,
            "duplicates_removed": 0,
            "rejected_non_wechat": 0,
            "rejected_invalid_wechat": 0,
            "results_without_url": 0,
            "new_urls_capped": 0,
            "providers_used": [],
        }

    if not settings["enabled"]:
        return {
            "status": "skipped",
            "discovered_at": run_timestamp,
            "query_plan": plan,
            "config": settings,
            "stats": empty_stats(skipped=len(plan)),
            "results": [],
            "errors": [],
            "evidence_policy": (
                "Search titles/snippets are discovery metadata only. No article body was fetched; "
                "all results remain discovery_only and are ineligible for evidence scoring."
            ),
        }

    if search_backend is None:
        provider_key = str(settings["provider"]).strip().casefold().replace("-", "_")
        if provider_key not in {"exa", "mcporter", "mcporter_exa", "exa_mcp_http"}:
            error = {
                "type": "search_backend_error",
                "code": "unsupported_provider",
                "query_id": "",
                "query": "",
                "sector": "",
                "event": "",
                "provider": str(settings["provider"]),
                "retriable": False,
                "error": "configured discovery provider is not supported",
            }
            return {
                "status": "error",
                "discovered_at": run_timestamp,
                "query_plan": plan,
                "config": settings,
                "stats": empty_stats(failed=len(plan)),
                "results": [],
                "errors": [error],
                "evidence_policy": (
                    "Search titles/snippets are discovery metadata only. No article body was fetched; "
                    "all results remain discovery_only and are ineligible for evidence scoring."
                ),
            }
        if provider_key == "exa_mcp_http":
            backend = ExaMcpHttpBackend(timeout=float(settings["timeout_seconds"]))
        else:
            backend = McporterExaBackend(timeout=float(settings["timeout_seconds"]))
    else:
        backend = search_backend
    default_provider = _backend_provider(backend)

    records_by_url: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    providers_used: list[str] = []
    queries_succeeded = 0
    queries_failed = 0
    raw_results = 0
    url_candidates = 0
    accepted_hits = 0
    duplicates_removed = 0
    rejected_non_wechat = 0
    rejected_invalid = 0
    results_without_url = 0
    queries_skipped = 0
    new_urls_capped = 0

    def add_error(payload: dict[str, Any]) -> None:
        if len(errors) < error_limit:
            errors.append(payload)

    for query_index, query_item in enumerate(plan):
        if len(records_by_url) >= new_url_limit:
            queries_skipped = len(plan) - query_index
            break
        try:
            payload = _invoke_backend(backend, query_item["query"], per_query_limit)
            if isinstance(payload, Mapping) and payload.get("error") and not payload.get("results"):
                raise SearchBackendError("backend_failed", _safe_error(payload.get("error")), retriable=True)
            rows = normalize_search_results(payload)
            queries_succeeded += 1
        except Exception as exc:
            queries_failed += 1
            code = exc.code if isinstance(exc, SearchBackendError) else "backend_exception"
            add_error(
                {
                    "type": "search_backend_error",
                    "code": code,
                    "query_id": query_item["query_id"],
                    "query": query_item["query"],
                    "sector": query_item["sector"],
                    "event": query_item["event"],
                    "provider": default_provider,
                    "retriable": bool(getattr(exc, "retriable", False)),
                    "error": _safe_error(exc),
                }
            )
            continue

        raw_results += len(rows)
        for row in rows:
            candidates = _result_urls(row)
            if not candidates:
                results_without_url += 1
                continue
            row_accepted = False
            for candidate in candidates:
                url_candidates += 1
                try:
                    canonical_url = canonicalize_discovered_url(candidate)
                except ValueError:
                    if _contains_wechat_hint(candidate):
                        rejected_invalid += 1
                        add_error(
                            {
                                "type": "url_rejected",
                                "code": "invalid_wechat_url",
                                "query_id": query_item["query_id"],
                                "provider": default_provider,
                                "candidate": _safe_url_hint(candidate),
                                "error": "candidate is outside the exact mp.weixin.qq.com/s boundary",
                            }
                        )
                    else:
                        rejected_non_wechat += 1
                    continue

                row_accepted = True
                accepted_hits += 1
                provider = _text(row.get("provider"), 80) or default_provider
                if provider not in providers_used:
                    providers_used.append(provider)
                title = _text(_first(row, ("title", "name")), 500)
                snippet = _safe_error(_first(row, ("snippet", "summary", "description", "text")))
                if snippet == "unknown search backend failure":
                    snippet = ""
                discovery = {
                    "query_id": query_item["query_id"],
                    "query": query_item["query"],
                    "sector": query_item["sector"],
                    "event": query_item["event"],
                    "provider": provider,
                    "discovered_at": run_timestamp,
                }
                existing = records_by_url.get(canonical_url)
                if existing is None:
                    if len(records_by_url) >= new_url_limit:
                        new_urls_capped += 1
                        continue
                    records_by_url[canonical_url] = {
                        "title": title,
                        "author": _text(row.get("author"), 200),
                        "url": canonical_url,
                        "canonical_url": canonical_url,
                        "search_snippet": snippet,
                        "search_published_at": _text(row.get("search_published_at"), 100),
                        "query": query_item["query"],
                        "query_id": query_item["query_id"],
                        "sector": query_item["sector"],
                        "event": query_item["event"],
                        "provider": provider,
                        "discovered_at": run_timestamp,
                        "query_hits": [query_item["query"]],
                        "providers": [provider],
                        "discoveries": [discovery],
                        "retrieval_provider_count": 1,
                        "provider_count": 1,
                        "provider_count_kind": "retrieval_channels",
                        "independent_source_count": 0,
                        "independently_corroborated": False,
                        "source_type": "web_search_discovery",
                        "discovery_only": True,
                        "evidence_eligible": False,
                        "evidence_status": "search_result_only",
                        "verification_status": "source_page_not_fetched",
                    }
                    continue

                duplicates_removed += 1
                if title and not existing["title"]:
                    existing["title"] = title
                if row.get("author") and not existing.get("author"):
                    existing["author"] = _text(row.get("author"), 200)
                if snippet and not existing["search_snippet"]:
                    existing["search_snippet"] = snippet
                if row.get("search_published_at") and not existing.get("search_published_at"):
                    existing["search_published_at"] = _text(row.get("search_published_at"), 100)
                if query_item["query"] not in existing["query_hits"]:
                    existing["query_hits"].append(query_item["query"])
                if provider not in existing["providers"]:
                    existing["providers"].append(provider)
                if discovery not in existing["discoveries"]:
                    existing["discoveries"].append(discovery)
                existing["retrieval_provider_count"] = len(existing["providers"])
                existing["provider_count"] = len(existing["providers"])
            if not row_accepted and not any(_contains_wechat_hint(value) for value in candidates):
                # The per-candidate counter above already captures every ordinary
                # non-WeChat URL. This flag is retained only for readable control flow.
                continue

    results = list(records_by_url.values())
    if queries_failed and not queries_succeeded:
        status = "error"
    elif queries_failed:
        status = "partial"
    elif results:
        status = "ok"
    else:
        status = "empty"
    return {
        "status": status,
        "discovered_at": run_timestamp,
        "query_plan": plan,
        "config": settings,
        "stats": {
            "queries_total": len(plan),
            "queries_succeeded": queries_succeeded,
            "queries_failed": queries_failed,
            "queries_skipped": queries_skipped,
            "raw_results": raw_results,
            "url_candidates": url_candidates,
            "accepted_hits": accepted_hits,
            "unique_results": len(results),
            "duplicates_removed": duplicates_removed,
            "rejected_non_wechat": rejected_non_wechat,
            "rejected_invalid_wechat": rejected_invalid,
            "results_without_url": results_without_url,
            "new_urls_capped": new_urls_capped,
            "providers_used": providers_used,
        },
        "results": results,
        "errors": errors,
        "evidence_policy": (
            "Search titles/snippets are discovery metadata only. No article body was fetched; "
            "all results remain discovery_only and are ineligible for evidence scoring."
        ),
    }


discover = discover_wechat_articles


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ExaMcpHttpBackend",
    "McporterExaBackend",
    "SearchBackend",
    "SearchBackendError",
    "build_config_query_plan",
    "build_query_plan",
    "canonicalize_discovered_url",
    "discover",
    "discover_wechat_articles",
    "load_discovery_config",
    "normalize_search_results",
    "parse_mcporter_output",
]
