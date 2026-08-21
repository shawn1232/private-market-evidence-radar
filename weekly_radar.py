"""DealScope 一级市场证据雷达。

本模块刻意独立于原有评分引擎：只保留过去七个自然日内发生的一个
高价值事件，每家公司最多一个事件，最多展示五家公司。Google News RSS
与全网公众号搜索都先作为发现入口；公众号必须取得正文和真实发布日期后
才可进入七日筛选，搜索摘要始终标记为 ``discovery_only / 待核实``。
"""

from __future__ import annotations

import argparse
import copy
import html
import hashlib
import json
import os
import re
import tempfile
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from wechat_discovery import discover_wechat_articles, load_discovery_config
from wechat_source_pool import WeChatSourcePool, canonicalize_wechat_url


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "weekly_radar_config.json"
WECHAT_URLS_PATH = ROOT / "data" / "input" / "wechat_urls.txt"
WECHAT_POOL_ROOT = ROOT / "data" / "wechat_pool"
WECHAT_DISCOVERY_STATE_PATH = WECHAT_POOL_ROOT / "discovery_state.json"
WECHAT_FOLLOWUP_CACHE_PATH = WECHAT_POOL_ROOT / "followup_news_cache.json"
WECHAT_PUBLIC_EXPORTER_BASE = "https://down.mptext.top"
OUTPUT_PATH = ROOT / "data" / "output" / "weekly_radar.json"
_WECHAT_DISCOVERY_LOCK = threading.Lock()

_DEFAULT_MAX = 5
_DEFAULT_WINDOW_DAYS = 7
_ALLOWED_DATE_FIELDS = ("event_date", "published_at", "publish_date", "date")
_COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "股份公司",
    "有限公司",
    "公司",
    "集团",
    "控股",
)
_LEADING_NOISE = re.compile(
    r"^(?:重磅|独家|快讯|消息|首发|聚焦|最新|官宣|融资快报|投融资观察|"
    r"国产|国内|中国|一家|机器人企业|半导体企业|创新企业|科技企业)+[：:｜|\s]*"
)


def _shanghai_tz() -> tzinfo:
    """Return Shanghai time without requiring the optional Windows tzdata package."""

    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


SHANGHAI_TZ = _shanghai_tz()


def _load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("weekly_radar_config.json 顶层必须是对象")
    return data


def _coerce_as_of(value: Any = None) -> date:
    if value is None:
        return datetime.now(SHANGHAI_TZ).date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI_TZ)
        return value.astimezone(SHANGHAI_TZ).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip()[:10])
    raise TypeError("as_of 只接受 date、datetime、YYYY-MM-DD 或 None")


def _window(as_of: date, days: int) -> dict[str, Any]:
    days = max(1, int(days))
    start = as_of - timedelta(days=days - 1)
    return {
        "timezone": "Asia/Shanghai",
        "as_of": as_of.isoformat(),
        "start_date": start.isoformat(),
        "end_date": as_of.isoformat(),
        "days": days,
        "boundary": "inclusive",
    }


def _parse_date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.astimezone(SHANGHAI_TZ).date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(SHANGHAI_TZ).date()
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        return _parse_date_value(int(text))
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.astimezone(SHANGHAI_TZ).date()
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.astimezone(SHANGHAI_TZ).date()
    except (TypeError, ValueError, OverflowError):
        pass

    match = re.search(r"(?P<year>20\d{2})[年/.-](?P<month>\d{1,2})[月/.-](?P<day>\d{1,2})日?", text)
    if match:
        try:
            return date(int(match["year"]), int(match["month"]), int(match["day"]))
        except ValueError:
            return None
    return None


def _extract_event_date_with_basis(record: dict[str, Any]) -> tuple[date | None, str]:
    """Read real event/publication fields and disclose exactly which date was used."""

    basis = {
        "event_date": "事件发生日",
        "published_at": "以文章发布日期代替，待核实事件日",
        "publish_date": "以文章发布日期代替，待核实事件日",
        "date": "来源日期，待核实事件日",
    }
    for key in _ALLOWED_DATE_FIELDS:
        parsed = _parse_date_value(record.get(key))
        if parsed is not None:
            return parsed, basis[key]
    return None, "缺少可用日期"


def _extract_event_date(record: dict[str, Any]) -> date | None:
    """Read only real publication/event fields; captured_at is deliberately ignored."""

    return _extract_event_date_with_basis(record)[0]


def _normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _normalize_title(title: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.lower())


def _canonical_company(company: str) -> str:
    value = _normalize_space(company).lower()
    value = re.sub(r"[（(][^）)]{0,40}[）)]", "", value)
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if value.endswith(suffix.lower()) and len(value) > len(suffix) + 1:
                value = value[: -len(suffix)]
                changed = True
                break
    return value


def _same_company_alias(left: str, right: str) -> bool:
    first = _canonical_company(left)
    second = _canonical_company(right)
    if not first or not second:
        return False
    if first == second:
        return True
    shorter, longer = sorted((first, second), key=len)
    return len(shorter) >= 4 and len(longer) - len(shorter) <= 4 and (
        longer.startswith(shorter) or longer.endswith(shorter)
    )


def _sanitize_company(value: Any) -> str:
    company = _normalize_space(value)
    company = company.strip("《》【】「」『』“”‘’'\"：:，,；;｜|—- ")
    company = _LEADING_NOISE.sub("", company)
    company = re.sub(r"^(?:融资|项目|快讯)[丨｜|：:\s]+", "", company)
    company = re.sub(r"^(?:由|据悉|日前|近日)", "", company).strip()
    # Headlines often put a duration immediately before the event verb, for
    # example “觅蜂科技半年完成三轮融资”.  The duration describes how quickly
    # the event happened; it is not part of the legal or brand name.
    company = re.sub(
        r"(?:不到|不足|超过|逾|仅用|用时|历时|在)?"
        r"(?:近|过去|短短)?"
        r"(?:半|一|两|二|三|四|五|六|七|八|九|十|\d+)"
        r"(?:年|个月|月|周|天)(?:内|间|来)?$",
        "",
        company,
    ).strip()
    company = re.sub(r"(?:宣布|完成|获得|获|通过|签署|中标|实现|启动|发布)$", "", company).strip()
    return company[:40]


def _valid_company(company: str, config: dict[str, Any]) -> bool:
    canonical = _canonical_company(company)
    if len(canonical) < 2 or len(canonical) > 30:
        return False
    stopwords = {_canonical_company(item) for item in config.get("entity_stopwords", [])}
    if canonical in stopwords:
        return False
    excluded = {_canonical_company(item) for item in config.get("excluded_mature_entities", [])}
    if canonical in excluded:
        return False
    for pattern in config.get("generic_entity_patterns", []):
        try:
            if re.search(str(pattern), company, re.IGNORECASE):
                return False
        except re.error:
            continue
    if company.endswith(("项目", "产品", "方案", "技术", "行业", "市场", "创新药")):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?(?:亿|万|元)?", canonical):
        return False
    return True


def _extract_company(title: str, config: dict[str, Any]) -> str | None:
    title = re.sub(r"\s+-\s+[^-]{1,40}$", "", _normalize_space(title))
    quoted_patterns = (
        r"[「『【“‘]([^」』】”’]{2,30})[」』】”’](?=.{0,18}(?:融资|量产|投产|中标|获批|发布|交付|认证|签署|递表|挂牌))",
        r"《([^》]{2,30})》(?=.{0,18}(?:融资|量产|投产|中标|获批|发布|交付|认证|签署|递表|挂牌))",
    )
    for pattern in quoted_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            candidate = _sanitize_company(match.group(1))
            if _valid_company(candidate, config):
                return candidate

    verb_pattern = re.compile(
        r"(?P<company>[A-Za-z][A-Za-z0-9 .·&_-]{1,28}|[\u4e00-\u9fffA-Za-z0-9·（）()]{2,30}?)"
        r"(?:宣布)?(?:完成|获得|获|斩获|通过|签署|中标|实现|启动|发布|交付|投产|量产|递表|挂牌)",
        re.IGNORECASE,
    )
    for match in verb_pattern.finditer(title):
        raw = re.split(r"[：:｜|！!?，,；;]", match.group("company"))[-1]
        candidate = _sanitize_company(raw)
        if _valid_company(candidate, config):
            return candidate

    colon_prefix = re.match(r"^([^：:｜|]{2,30})[：:｜|]", title)
    if colon_prefix:
        candidate = _sanitize_company(colon_prefix.group(1))
        if _valid_company(candidate, config):
            return candidate
    return None


def _match_core_variable(text: str, config: dict[str, Any]) -> tuple[str, str, int] | None:
    normalized = text.lower()
    for index, item in enumerate(config.get("core_variables", [])):
        for keyword in item.get("keywords", []):
            if str(keyword).lower() in normalized:
                return str(item.get("name") or "核心变量"), str(keyword), index
    return None


def _is_actionable_event(text: str, core_variable: str, config: dict[str, Any]) -> bool:
    """Require a completed milestone, not a plan, forecast, question, or ongoing process."""

    for pattern in config.get("non_event_patterns", []):
        try:
            if re.search(str(pattern), text, re.IGNORECASE):
                return False
        except re.error:
            continue
    patterns = config.get("confirmed_event_patterns", {}).get(core_variable, [])
    if not patterns:
        return True
    for pattern in patterns:
        try:
            if re.search(str(pattern), text, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _fit_tags(text: str, record: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Infer fit from the observed text, never from the query bucket alone."""
    tags: list[str] = []
    normalized = text.lower()
    for sector, keywords in config.get("sector_keywords", {}).items():
        if any(str(keyword).lower() in normalized for keyword in keywords):
            tags.append(str(sector))
    if not bool(record.get("discovery_only")):
        for tag in record.get("fit_tags", []) or []:
            clean = _normalize_space(tag)
            if clean and clean not in tags:
                tags.append(clean)
    principles = config.get("investment_profile", {}).get("principles", [])
    for principle in principles:
        if principle in text and principle not in tags:
            tags.append(str(principle))
    return tags[:4]


def _verification_action(core_variable: str) -> tuple[str, str]:
    mapping = {
        "客户验证": (
            "客户名称、订单金额、是否付费及复购尚未由一手材料交叉核验",
            "联系公司并取得合同/中标通知/客户访谈，核验金额、交付与回款",
        ),
        "规模化与交付能力": (
            "实际产能、良率、在手订单和交付节奏尚未核验",
            "核验产线、良率、在手订单、交付验收与回款凭证",
        ),
        "监管或临床里程碑": (
            "批件范围、临床阶段和商业化时间表尚未核验",
            "取得监管/临床登记原件，并核验适应症、阶段与后续资金需求",
        ),
        "技术去风险": (
            "技术指标、第三方验证和相对竞品优势尚未核验",
            "取得测试报告、客户验证记录，并访谈技术负责人和外部专家",
        ),
        "资本与产业资源到位": (
            "融资是否完成交割、估值、投资方及资金用途尚未核验",
            "核验交割凭证、最新股权结构、估值及本轮资金用途",
        ),
        "退出可见性": (
            "申报阶段、合规障碍与可实现退出路径尚未核验",
            "核验监管披露、辅导/申报文件和股东可退出条款",
        ),
    }
    return mapping.get(
        core_variable,
        ("事件真实性和对基本面的影响尚未核验", "取得一手材料，并完成公司、客户和行业三方交叉验证"),
    )


def _candidate_from_record(
    record: dict[str, Any],
    start_date: date,
    end_date: date,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    event_date, date_basis = _extract_event_date_with_basis(record)
    if event_date is None or event_date < start_date or event_date > end_date:
        return None

    title = _normalize_space(record.get("title"))
    url = _normalize_space(record.get("url"))
    if not title or not url:
        return None
    publisher_host = (urlsplit(_normalize_space(record.get("publisher_url"))).hostname or "").lower()
    excluded_publisher_hosts = [str(item).lower() for item in config.get("excluded_publisher_hosts", [])]
    if record.get("source_type") == "google_news_rss" and any(
        publisher_host == blocked or publisher_host.endswith("." + blocked)
        for blocked in excluded_publisher_hosts
    ):
        return None
    # A six-digit stock code is a strong signal that this is a listed-company news item,
    # not the primary PE project pool requested by the user.
    if re.search(r"[（(]\s*\d{6}\s*[）)]", title):
        return None
    text = " ".join((title, _normalize_space(record.get("summary"))))
    core_match = _match_core_variable(text, config)
    if core_match is None:
        return None
    core_variable, trigger, variable_index = core_match
    if not _is_actionable_event(text, core_variable, config):
        return None
    if core_variable == "资本与产业资源到位":
        for pattern in config.get("excluded_late_stage_patterns", []):
            try:
                if re.search(str(pattern), text, re.IGNORECASE):
                    return None
            except re.error:
                continue

    explicit_company = _sanitize_company(record.get("company") or record.get("entity"))
    if _valid_company(explicit_company, config):
        company = explicit_company
        entity_confidence = 3
    else:
        company = _extract_company(title, config) or ""
        quoted = bool(company and re.search(rf"[「『【“‘《][^」』】”’》]*{re.escape(company)}", title))
        colon_led = bool(company and re.match(rf"^{re.escape(company)}(?:[（(][^）)]*[）)])?[：:｜|]", title))
        entity_confidence = 2 if quoted or colon_led else 1
    if not company:
        return None
    private_shape = bool(
        re.search(r"(?:科技|智能|半导体|机器人|生物|医药|材料|光电|微电子|能源|动力|装备|仪器)$", company)
    )
    if entity_confidence == 1 and core_variable != "资本与产业资源到位" and not private_shape:
        return None

    tags = _fit_tags(text, record, config)
    if not tags:
        return None

    discovery_only = bool(record.get("discovery_only")) or record.get("source_type") == "google_news_rss"
    verification_status = "待核实" if discovery_only else "已读取原文，关键事实待核实"
    short_title = title if len(title) <= 90 else title[:89] + "…"
    tag_text = "、".join(tags[:2])
    if discovery_only:
        reason = (
            f"因为本周出现“{short_title}”这一待核实信号，可能推动“{core_variable}”发生变化，"
            f"且与当前可配置研究标签“{tag_text}”相关，所以进入待核验清单。"
        )
    else:
        reason = (
            f"因为本周原文披露“{short_title}”，显示“{core_variable}”出现新进展，"
            f"且与当前可配置研究标签“{tag_text}”相关，所以进入待核验清单。"
        )
    key_unknown, next_verification = _verification_action(core_variable)
    exact_source = not discovery_only

    candidate = {
        "company": company,
        "event_date": event_date.isoformat(),
        "date_basis": date_basis,
        "event": short_title,
        "core_variable": core_variable,
        "trigger": trigger,
        "entry_reason": reason,
        "fit_tags": tags,
        "verification_status": verification_status,
        "key_unknown": key_unknown,
        "next_verification": next_verification,
        "source": {
            "title": title,
            "url": url,
            "publisher": _normalize_space(record.get("publisher")),
            "publisher_url": _normalize_space(record.get("publisher_url")),
            "published_at": event_date.isoformat(),
            "date_basis": date_basis,
            "source_type": _normalize_space(record.get("source_type")) or "unknown",
            "evidence_level": "exact_source_page" if exact_source else "discovery_only",
            "verification_status": verification_status,
            "read_count": record.get("read_count"),
            "like_count": record.get("like_count"),
            "share_count": record.get("share_count"),
            "favorite_count": record.get("favorite_count"),
            "comment_count": record.get("comment_count"),
            "metric_credential_status": _normalize_space(record.get("credential_status")),
        },
        "_rank": (
            1 if exact_source else 0,
            entity_confidence,
            max(0, len(config.get("core_variables", [])) - variable_index),
            event_date.toordinal(),
        ),
    }
    return candidate


def _build_report(
    records: Iterable[dict[str, Any]],
    as_of: Any,
    config: dict[str, Any],
    source_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    as_of_date = _coerce_as_of(as_of)
    days = max(1, int(config.get("window_days", _DEFAULT_WINDOW_DAYS)))
    max_candidates = max(1, min(5, int(config.get("max_candidates", _DEFAULT_MAX))))
    window = _window(as_of_date, days)
    start_date = date.fromisoformat(window["start_date"])

    candidates: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        candidate = _candidate_from_record(record, start_date, as_of_date, config)
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            item["_rank"][0],
            item["_rank"][1],
            item["_rank"][2],
            item["_rank"][3],
            _canonical_company(item["company"]),
        ),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for candidate in candidates:
        company_key = _canonical_company(candidate["company"])
        url_key = candidate["source"]["url"].strip().lower()
        title_key = _normalize_title(candidate["source"]["title"])
        if not company_key or url_key in seen_urls or title_key in seen_titles:
            continue
        duplicate_index = next(
            (
                index
                for index, selected_item in enumerate(selected)
                if _same_company_alias(candidate["company"], selected_item["company"])
            ),
            None,
        )
        if duplicate_index is not None:
            existing = selected[duplicate_index]
            aliases = set(existing.get("company_aliases") or [existing["company"]])
            aliases.add(candidate["company"])
            existing["company_aliases"] = sorted(aliases, key=lambda value: (len(value), value), reverse=True)
            if len(candidate["company"]) > len(existing["company"]):
                existing["company"] = candidate["company"]
            continue
        candidate.pop("_rank", None)
        candidate["company_aliases"] = [candidate["company"]]
        selected.append(candidate)
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        if len(selected) >= max_candidates:
            break

    statuses = source_status or {}
    source_errors = any(
        isinstance(item, dict) and item.get("status") in {"partial", "error"}
        for item in statuses.values()
    )
    status = "partial" if source_errors and selected else ("ok" if selected else "empty")
    return {
        "window": window,
        "status": status,
        "source_status": statuses,
        "candidates": selected,
        "empty_slots": max_candidates - len(selected),
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "method": "event_first_no_total_score",
    }


def _safe_error(exc: BaseException) -> str:
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return (text or exc.__class__.__name__)[:240]


def _google_rss_url(query: str) -> str:
    params = urlencode(
        {
            "q": query,
            "hl": "zh-CN",
            "gl": "CN",
            "ceid": "CN:zh-Hans",
        }
    )
    return f"https://news.google.com/rss/search?{params}"


def _fetch_google_query(query_item: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    response = None
    last_error: BaseException | None = None
    for _attempt in range(2):
        try:
            response = requests.get(
                _google_rss_url(str(query_item.get("query") or "")),
                headers={"User-Agent": "Mozilla/5.0 (compatible; DealScope-Evidence-Radar/1.0)"},
                timeout=(5, timeout),
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            response = None
    if response is None:
        assert last_error is not None
        raise last_error
    root = ET.fromstring(response.content)
    records: list[dict[str, Any]] = []
    for item in root.findall("./channel/item")[:50]:
        title = _normalize_space(item.findtext("title"))
        url = _normalize_space(item.findtext("link"))
        published = _normalize_space(item.findtext("pubDate"))
        source_node = item.find("source")
        publisher = _normalize_space(source_node.text if source_node is not None else "")
        publisher_url = _normalize_space(source_node.attrib.get("url") if source_node is not None else "")
        if publisher and title.endswith(" - " + publisher):
            title = title[: -(len(publisher) + 3)].strip()
        if not title or not url or not published:
            continue
        records.append(
            {
                "title": title,
                "url": url,
                "published_at": published,
                "publisher": publisher,
                "publisher_url": publisher_url,
                "summary": _normalize_space(re.sub(r"<[^>]+>", " ", item.findtext("description") or "")),
                "source_type": "google_news_rss",
                "discovery_only": True,
                "verification_status": "待核实",
                "query_name": _normalize_space(query_item.get("name")),
                "discovery_context_tags": list(query_item.get("fit_tags") or []),
            }
        )
    return records


def _collect_google_news(config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    queries = [item for item in config.get("google_news_queries", []) if item.get("query")]
    timeout = float(config.get("request_timeout_seconds", 12))
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    succeeded = 0
    if not queries:
        return [], {
            "status": "skipped",
            "queries_total": 0,
            "queries_succeeded": 0,
            "items_seen": 0,
            "errors": [],
            "note": "未配置 Google News RSS 查询",
        }

    # Google News through a corporate/local proxy is noticeably less reliable under
    # high fan-out. Two workers plus one per-query retry is faster in practice and
    # avoids turning a transient proxy reset into a false "no projects this week".
    workers = min(2, len(queries))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_fetch_google_query, item, timeout): item for item in queries}
        for future in as_completed(future_map):
            query_item = future_map[future]
            try:
                records.extend(future.result())
                succeeded += 1
            except Exception as exc:
                errors.append({"query": _normalize_space(query_item.get("name")), "error": _safe_error(exc)})

    status = "ok" if succeeded == len(queries) else ("partial" if succeeded else "error")
    return records, {
        "status": status,
        "queries_total": len(queries),
        "queries_succeeded": succeeded,
        "items_seen": len(records),
        "errors": errors[:10],
        "evidence_policy": "全部为 discovery_only / 待核实，不等于已验证事实",
    }


def _wechat_discovery_day(value: Any) -> date | None:
    text = _normalize_space(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(SHANGHAI_TZ).date()


def _followup_company(row: dict[str, Any], config: dict[str, Any]) -> str:
    title = _normalize_space(row.get("title"))
    extracted = _extract_company(title, config) or ""
    account = _normalize_space(row.get("account_name"))
    rejected_tokens = ("产品", "系统", "设备", "仪", "证书", "获批", "上市", "IND", "FDA", "NMPA")

    def usable(value: str) -> bool:
        return (
            2 <= len(value) <= 24
            and len(re.findall(r"[\u4e00-\u9fff]", value)) >= 2
            and not any(token.casefold() in value.casefold() for token in rejected_tokens)
            and not value.startswith(("待归属", "待识别", "公众号待"))
        )

    if usable(extracted):
        return extracted
    return account if usable(account) else ""


def _collect_wechat_followup_news(
    config: dict[str, Any],
    discovered_rows: list[dict[str, Any]],
    *,
    as_of: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use new WeChat leads to find dated open-web corroboration.

    The WeChat search title only seeds the query. Returned news rows remain
    discovery-only and keep their own publication date/source URL.
    """

    limit = max(0, min(int(config.get("wechat_followup_query_limit", 10)), 20))
    if not limit:
        return [], {"status": "skipped", "queries_total": 0, "items_seen": 0, "errors": []}
    seed_material = sorted(
        f"{_normalize_space(row.get('url'))}|{_normalize_space(row.get('discovered_at'))}"
        for row in discovered_rows
        if _normalize_space(row.get("url")) and _normalize_space(row.get("discovered_at"))
    )
    seed_signature = hashlib.sha256("\n".join(seed_material).encode("utf-8")).hexdigest() if seed_material else ""
    if seed_signature and as_of == datetime.now(SHANGHAI_TZ).date():
        try:
            cached = json.loads(WECHAT_FOLLOWUP_CACHE_PATH.read_text(encoding="utf-8"))
            generated = datetime.fromisoformat(str(cached.get("generated_at") or "").replace("Z", "+00:00"))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            fresh = datetime.now(timezone.utc) - generated.astimezone(timezone.utc) < timedelta(hours=12)
            cached_records = cached.get("records")
            cached_status = cached.get("source_status")
            if (
                fresh
                and cached.get("seed_signature") == seed_signature
                and isinstance(cached_records, list)
                and isinstance(cached_status, dict)
            ):
                status = dict(cached_status)
                status["status"] = "cached"
                status["cached"] = True
                return [dict(item) for item in cached_records if isinstance(item, dict)], status
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            pass
    candidates: list[tuple[int, dict[str, Any], str, str]] = []
    for row in discovered_rows:
        discovered_day = _wechat_discovery_day(row.get("discovered_at"))
        if discovered_day is None or not (as_of - timedelta(days=2) <= discovered_day <= as_of):
            continue
        title = _normalize_space(row.get("title"))
        core = _match_core_variable(title, config)
        if not title or core is None:
            continue
        if re.search(r"(?:C\d*轮|D\d*轮|E\d*轮|F\d*轮|Pre[- ]?IPO)", title, re.IGNORECASE):
            continue
        company = _followup_company(row, config)
        if not company:
            continue
        core_name = core[0]
        priority = 3 if core_name == "资本与产业资源到位" else (2 if core_name in {"客户验证", "规模化与交付能力"} else 1)
        if re.search(r"(?:天使轮|种子轮|Pre[- ]?A|A\+?轮)", title, re.IGNORECASE):
            priority += 2
        candidates.append((priority, row, company, core_name))
    candidates.sort(
        key=lambda item: (
            item[0],
            _normalize_space(item[1].get("discovered_at")),
            -(int(item[1].get("discovery_rank") or 10_000)),
        ),
        reverse=True,
    )

    query_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_companies: set[str] = set()
    for _priority, row, company, core_name in candidates:
        key = _canonical_company(company)
        if key in seen_companies:
            continue
        seen_companies.add(key)
        event_terms: list[str] = []
        for variable in config.get("core_variables", []):
            if variable.get("name") == core_name:
                event_terms = [_normalize_space(item) for item in variable.get("keywords", []) if _normalize_space(item)][:5]
                break
        expression = " OR ".join(event_terms or [core_name])
        query_items.append(
            (
                {
                    "name": f"公众号拓源复核 · {company}",
                    "query": f'"{company}" ({expression}) when:30d',
                    "fit_tags": [],
                },
                row,
            )
        )
        if len(query_items) >= limit:
            break

    if not query_items:
        return [], {"status": "skipped", "queries_total": 0, "items_seen": 0, "errors": []}

    timeout = float(config.get("request_timeout_seconds", 12))
    records_by_url: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    succeeded = 0
    with ThreadPoolExecutor(max_workers=min(4, len(query_items))) as executor:
        future_map = {
            executor.submit(_fetch_google_query, query_item, timeout): (query_item, seed)
            for query_item, seed in query_items
        }
        for future in as_completed(future_map):
            query_item, seed = future_map[future]
            try:
                rows = future.result()
                succeeded += 1
            except Exception as exc:
                errors.append({"query": query_item["name"], "error": _safe_error(exc)})
                continue
            for record in rows:
                url = _normalize_space(record.get("url"))
                if not url:
                    continue
                current = dict(record)
                current["discovery_only"] = True
                current["discovery_origin"] = "wechat_pool_followup"
                current["wechat_seed_url"] = _normalize_space(seed.get("url"))
                current["wechat_seed_title"] = _normalize_space(seed.get("title"))
                records_by_url.setdefault(url, current)
    status = "ok" if succeeded == len(query_items) else ("partial" if succeeded else "error")
    records = list(records_by_url.values())
    source_status = {
        "status": status,
        "queries_total": len(query_items),
        "queries_succeeded": succeeded,
        "items_seen": len(records),
        "errors": errors[:10],
        "evidence_policy": "公众号标题只用于定向拓源；复核结果仍是 discovery_only，按其自身发布日期进入窗口。",
    }
    if seed_signature and status in {"ok", "partial"}:
        _atomic_write_text(
            WECHAT_FOLLOWUP_CACHE_PATH,
            json.dumps(
                {
                    "seed_signature": seed_signature,
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "records": records,
                    "source_status": source_status,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return records, source_status


def _read_wechat_urls() -> list[str]:
    if not WECHAT_URLS_PATH.exists():
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for raw_line in WECHAT_URLS_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        normalized = _normalize_wechat_url(line)
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def _extract_html_title(page: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<h1[^>]+id=["\']activity-name["\'][^>]*>(.*?)</h1>',
        r"\bmsg_title\s*=\s*['\"]([^'\"]+)",
        r"<title[^>]*>(.*?)</title>",
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE | re.DOTALL)
        if match:
            value = re.sub(r"<[^>]+>", " ", match.group(1))
            value = _normalize_space(value)
            if value:
                return value
    return ""


def _extract_wechat_published_at(page: str) -> str | int | None:
    text_patterns = (
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']article:published_time["\']',
        r'["\']publish_time["\']\s*:\s*["\']([^"\']+)',
    )
    for pattern in text_patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match and _parse_date_value(match.group(1)) is not None:
            return match.group(1)
    epoch_patterns = (
        r"\bvar\s+ct\s*=\s*['\"]?(\d{10,13})",
        r'["\'](?:oriCreateTime|create_time|createTime)["\']\s*:\s*["\']?(\d{10,13})',
    )
    for pattern in epoch_patterns:
        match = re.search(pattern, page)
        if match and _parse_date_value(match.group(1)) is not None:
            return int(match.group(1))
    return None


def _extract_wechat_publisher(page: str) -> str:
    patterns = (
        r"\bnickname\s*=\s*['\"]([^'\"]+)",
        r'<strong[^>]+class=["\'][^"\']*profile_nickname[^"\']*["\'][^>]*>(.*?)</strong>',
        r'<span[^>]+id=["\']js_author_name_text["\'][^>]*>(.*?)</span>',
        r'<span[^>]+id=["\']js_author_name["\'][^>]*>(.*?)</span>',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE | re.DOTALL)
        if match:
            return _normalize_space(re.sub(r"<[^>]+>", " ", match.group(1)))
    return "微信公众号"


def _wechat_record_from_html(
    page: str,
    *,
    canonical_url: str,
    source_type: str,
) -> dict[str, Any]:
    title = _extract_html_title(page)
    published_at = _extract_wechat_published_at(page)
    if not title:
        raise ValueError("页面未解析出文章标题（可能触发公众号访问校验）")
    soup = BeautifulSoup(page, "lxml")
    content = soup.select_one("#js_content") or soup.select_one(".rich_media_content") or soup.body or soup
    plain_text = _normalize_space(content.get_text(" ", strip=True))
    if len(plain_text) < 80:
        raise ValueError("公众号正文过短或触发访问校验")
    publisher = _extract_wechat_publisher(page)
    has_real_date = published_at is not None
    return {
        "title": title,
        "url": canonical_url,
        "published_at": published_at,
        "publisher": publisher,
        "publisher_url": canonical_url,
        "summary": plain_text[:6000],
        "body_text": plain_text[:200_000],
        "source_type": source_type,
        "discovery_only": not has_real_date,
        "verification_status": (
            "已读取原文，关键事实待核实"
            if has_real_date
            else "已读取正文，真实发布日期待补；暂不进入七日评分"
        ),
        "fit_tags": [],
    }


def _fetch_wechat_public_exporter(url: str, timeout: float) -> dict[str, Any]:
    canonical_url = canonicalize_wechat_url(url)
    response = requests.get(
        f"{WECHAT_PUBLIC_EXPORTER_BASE}/api/public/v1/download",
        params={"url": canonical_url, "format": "html"},
        headers={"User-Agent": "DealScope-Evidence-Radar/1.0"},
        timeout=(5, max(5.0, min(float(timeout), 20.0))),
    )
    response.raise_for_status()
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if content_type and not any(kind in content_type for kind in ("text/html", "text/plain", "application/xhtml+xml")):
        raise ValueError("公众号公开正文回退返回了非文本内容")
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError("公众号公开正文回退超过 2MB 安全上限")
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return _wechat_record_from_html(
        response.text,
        canonical_url=canonical_url,
        source_type="wechat_public_exporter_body",
    )


def _public_wechat_fallback_enabled() -> bool:
    return os.getenv("DEALSCOPE_ALLOW_PUBLIC_WECHAT_FALLBACK", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _fetch_wechat_url(url: str, timeout: float) -> dict[str, Any]:
    canonical_requested = canonicalize_wechat_url(url)
    try:
        response = requests.get(
            canonical_requested,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 Mobile/15E148 MicroMessenger/8.0"
                )
            },
            timeout=(5, max(5.0, min(float(timeout), 12.0))),
        )
        response.raise_for_status()
        canonical_url = canonicalize_wechat_url(response.url)
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and not any(kind in content_type for kind in ("text/html", "text/plain", "application/xhtml+xml")):
            raise ValueError("公众号响应不是可读取的正文页面")
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("公众号正文页面超过 2MB 安全上限")
        response.encoding = response.apparent_encoding or response.encoding
        return _wechat_record_from_html(
            response.text,
            canonical_url=canonical_url,
            source_type="wechat_exact_url",
        )
    except Exception as direct_error:
        if not _public_wechat_fallback_enabled():
            raise ValueError(
                "公众号原文未能直接读取；第三方公开正文回退默认关闭。"
                "如确认可向外部服务发送该公开文章 URL，可设置 "
                "DEALSCOPE_ALLOW_PUBLIC_WECHAT_FALLBACK=1。"
                f" 直接读取错误：{_safe_error(direct_error)}"
            ) from direct_error
        try:
            return _fetch_wechat_public_exporter(canonical_requested, timeout)
        except Exception as fallback_error:
            raise ValueError(
                "公众号原文和公开正文回退均未读取成功："
                f"{_safe_error(direct_error)}；{_safe_error(fallback_error)}"
            ) from fallback_error


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_wechat_discovery_state() -> dict[str, Any]:
    try:
        payload = json.loads(WECHAT_DISCOVERY_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _discovery_finished_at(state: dict[str, Any]) -> datetime | None:
    raw = _normalize_space(state.get("finished_at"))
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _discovery_due(settings: dict[str, Any], state: dict[str, Any], *, force: bool) -> bool:
    if force:
        return True
    finished = _discovery_finished_at(state)
    if finished is None:
        return True
    interval_hours = float(settings.get("interval_hours") or 24)
    if str(state.get("status") or "") == "error":
        interval_hours = min(interval_hours, 1.0)
    return datetime.now(timezone.utc) - finished >= timedelta(hours=max(0.25, interval_hours))


def _discovery_rows_for_pool(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, lead in enumerate(result.get("results") or [], start=1):
        if not isinstance(lead, dict):
            continue
        try:
            url = canonicalize_wechat_url(lead.get("url"))
        except ValueError:
            continue
        queries = [
            _normalize_space(item)
            for item in (lead.get("query_hits") or [lead.get("query")])
            if _normalize_space(item)
        ]
        providers = [
            _normalize_space(item)
            for item in (lead.get("providers") or [lead.get("provider")])
            if _normalize_space(item)
        ]
        rows.append(
            {
                "url": url,
                "title": _normalize_space(lead.get("title")),
                "account_name": _normalize_space(lead.get("author")) or "待归属:全网发现",
                "fetch_mode": "web_discovery",
                "credential_status": "not_required",
                "discovery_query": "\n".join(dict.fromkeys(queries))[:2000],
                "discovery_provider": ", ".join(dict.fromkeys(providers))[:120],
                "discovered_at": _normalize_space(lead.get("discovered_at") or result.get("discovered_at")),
                "discovery_rank": rank,
                # Search snippets and search-result dates are intentionally not
                # persisted as digest/body/publish_time evidence.
                "publish_time": None,
                "body_markdown_path": "",
                "error": "",
            }
        )
    return rows


def discover_wechat_sources(
    config: dict[str, Any] | None = None,
    *,
    force: bool = False,
    as_of: Any = None,
    pool: WeChatSourcePool | None = None,
    search_backend: Any = None,
) -> dict[str, Any]:
    """Search the public web for new WeChat articles and persist only leads.

    This function never promotes a search title/snippet into report evidence.
    A later exact-body fetch plus a real publication date is required before a
    discovered row can enter the seven-day radar.
    """

    config = config or _load_config()
    settings = load_discovery_config(CONFIG_PATH)
    target_date = _coerce_as_of(as_of)
    today = datetime.now(SHANGHAI_TZ).date()
    if target_date != today and not force:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "historical_replay",
            "message": "历史回放不调用当前互联网拓源，避免引入未来信息。",
            "stats": {},
            "pool_stats": (pool or WeChatSourcePool()).get_stats(),
        }
    if not bool(settings.get("enabled", True)):
        return {
            "ok": True,
            "status": "skipped",
            "reason": "disabled",
            "message": "公众号主动拓源当前已关闭。",
            "stats": {},
            "pool_stats": (pool or WeChatSourcePool()).get_stats(),
        }

    state = _read_wechat_discovery_state()
    if not _discovery_due(settings, state, force=force):
        return {
            "ok": True,
            "status": "skipped",
            "reason": "interval_not_elapsed",
            "message": "最近已完成公众号全网拓源，本次沿用文章池。",
            "stats": dict(state.get("stats") or {}),
            "pool_write": dict(state.get("pool_write") or {}),
            "pool_stats": (pool or WeChatSourcePool()).get_stats(),
            "finished_at": state.get("finished_at", ""),
        }
    if not _WECHAT_DISCOVERY_LOCK.acquire(blocking=False):
        return {
            "ok": True,
            "status": "busy",
            "queued": True,
            "message": "公众号全网拓源正在进行，请稍后查看文章库。",
            "stats": {},
            "pool_stats": (pool or WeChatSourcePool()).get_stats(),
        }

    source_pool = pool or WeChatSourcePool()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        result = discover_wechat_articles(
            search_backend=search_backend,
            config_path=CONFIG_PATH,
            as_of=target_date,
        )
        rows = _discovery_rows_for_pool(result)
        pool_write = source_pool.add_urls(
            rows,
            account_name="",
            dedupe_globally=True,
        ) if rows else {"added": 0, "exists": 0, "errors": 0, "results": []}
        finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state_payload = {
            "started_at": started_at,
            "finished_at": finished_at,
            "status": result.get("status", "error"),
            "stats": result.get("stats") or {},
            "pool_write": {
                "added": int(pool_write.get("added") or 0),
                "exists": int(pool_write.get("exists") or 0),
                "errors": int(pool_write.get("errors") or 0),
            },
            "errors": list(result.get("errors") or [])[:10],
        }
        _atomic_write_text(
            WECHAT_DISCOVERY_STATE_PATH,
            json.dumps(state_payload, ensure_ascii=False, indent=2),
        )
        status = str(result.get("status") or "error")
        added = int(pool_write.get("added") or 0)
        existing = int(pool_write.get("exists") or 0)
        ok = status in {"ok", "partial", "empty", "skipped"}
        if status == "error":
            message = "公众号全网拓源失败，原文章库保持不变。"
        elif added:
            message = f"全网发现完成：新增 {added} 篇公众号线索，另有 {existing} 篇已在库中。"
        else:
            message = f"全网发现完成：本轮没有新增链接，已有 {existing} 篇命中现有文章库。"
        return {
            "ok": ok,
            "status": status,
            "message": message,
            "stats": result.get("stats") or {},
            "pool_write": state_payload["pool_write"],
            "pool_stats": source_pool.get_stats(),
            "finished_at": finished_at,
            "errors": list(result.get("errors") or [])[:10],
            "evidence_policy": result.get("evidence_policy", ""),
        }
    except Exception as exc:
        finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        error = _safe_error(exc)
        state_payload = {
            "started_at": started_at,
            "finished_at": finished_at,
            "status": "error",
            "stats": {},
            "pool_write": {"added": 0, "exists": 0, "errors": 0},
            "errors": [{"error": error}],
        }
        _atomic_write_text(
            WECHAT_DISCOVERY_STATE_PATH,
            json.dumps(state_payload, ensure_ascii=False, indent=2),
        )
        return {
            "ok": False,
            "status": "error",
            "message": "公众号全网拓源失败，原文章库保持不变。",
            "stats": {},
            "pool_write": state_payload["pool_write"],
            "pool_stats": source_pool.get_stats(),
            "errors": [{"error": error}],
            "finished_at": finished_at,
        }
    finally:
        _WECHAT_DISCOVERY_LOCK.release()


def _cache_fetched_wechat_body(record: dict[str, Any]) -> str:
    url = _normalize_space(record.get("url"))
    body = _normalize_space(record.get("body_text"))
    if not url or not body:
        return ""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    target = WECHAT_POOL_ROOT / "bodies" / "exact" / f"{digest}.md"
    title = _normalize_space(record.get("title")) or "公众号文章"
    publisher = _normalize_space(record.get("publisher")) or "微信公众号"
    published = _parse_date_value(record.get("published_at"))
    date_text = published.isoformat() if published else "待确认"
    markdown = f"# {title}\n\n公众号：{publisher}\n\n发布日期：{date_text}\n\n{body}\n"
    _atomic_write_text(target, markdown)
    return str(target.resolve())


def _read_pool_body(path_value: Any) -> str:
    text = _normalize_space(path_value)
    if not text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = WECHAT_POOL_ROOT / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(WECHAT_POOL_ROOT.resolve())
    except (OSError, ValueError):
        return ""
    if resolved.suffix.lower() not in {".md", ".txt"} or not resolved.is_file():
        return ""
    try:
        if resolved.stat().st_size > 2 * 1024 * 1024:
            return ""
        return _normalize_space(resolved.read_text(encoding="utf-8", errors="replace"))[:200_000]
    except OSError:
        return ""


def _pool_row_to_radar_record(row: dict[str, Any]) -> dict[str, Any] | None:
    title = _normalize_space(row.get("title"))
    url = _normalize_space(row.get("url"))
    published_at = row.get("publish_time")
    if not title or not url or _parse_date_value(published_at) is None:
        return None
    body = _read_pool_body(row.get("body_markdown_path"))
    digest = _normalize_space(row.get("digest"))
    exact_body = bool(body)
    account = _normalize_space(row.get("account_name"))
    author = _normalize_space(row.get("author"))
    if account.startswith(("待识别:", "待归属")):
        account = author
    publisher = account or author or "微信公众号"
    return {
        "title": title,
        "url": url,
        "published_at": published_at,
        "publisher": publisher,
        "publisher_url": url,
        "summary": (body or digest)[:6000],
        "source_type": "wechat_pool_body" if exact_body else "wechat_pool_history",
        "discovery_only": not exact_body,
        "verification_status": "已读取原文，关键事实待核实" if exact_body else "历史清单线索，正文待读取",
        "fit_tags": [],
        "read_count": row.get("read_count"),
        "like_count": row.get("like_count"),
        "share_count": row.get("share_count"),
        "favorite_count": row.get("favorite_count"),
        "comment_count": row.get("comment_count"),
        "credential_status": _normalize_space(row.get("credential_status")),
    }


def _collect_wechat(
    config: dict[str, Any],
    *,
    as_of: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pool = WeChatSourcePool()
    urls = _read_wechat_urls()
    timeout = float(config.get("request_timeout_seconds", 12))
    days = max(1, int(config.get("window_days", _DEFAULT_WINDOW_DAYS)))
    end_date = _coerce_as_of(as_of)
    start_date = end_date - timedelta(days=days - 1)
    discovery = discover_wechat_sources(config, force=False, as_of=end_date, pool=pool)
    pool_rows = pool.records_for_window(start_date, end_date, scope="ready")
    pool_stats_before = pool.get_stats()

    records_by_url: dict[str, dict[str, Any]] = {}

    def keep_record(record: dict[str, Any]) -> None:
        try:
            key = canonicalize_wechat_url(record.get("url"))
        except ValueError:
            return
        existing = records_by_url.get(key)
        if existing is None or (existing.get("discovery_only") and not record.get("discovery_only")):
            records_by_url[key] = record

    for row in pool_rows:
        record = _pool_row_to_radar_record(row)
        if record is not None:
            keep_record(record)

    all_pool_rows = pool.list_articles(limit=10_000)
    pool_by_url: dict[str, dict[str, Any]] = {}
    for row in all_pool_rows:
        try:
            key = canonicalize_wechat_url(row.get("url"))
        except ValueError:
            continue
        pool_by_url.setdefault(key, row)

    discovered_rows = [row for row in all_pool_rows if _normalize_space(row.get("discovered_at"))]
    followup_records, followup_status = _collect_wechat_followup_news(
        config,
        discovered_rows,
        as_of=end_date,
    )

    fetch_urls: list[str] = []
    discovered_pending = [
        row
        for row in discovered_rows
        if not _normalize_space(row.get("error"))
        and not _read_pool_body(row.get("body_markdown_path"))
    ]
    discovered_pending.sort(
        key=lambda row: (
            _normalize_space(row.get("discovered_at")),
            -(int(row.get("discovery_rank") or 10_000)),
        ),
        reverse=True,
    )
    candidate_urls = [str(row.get("url") or "") for row in discovered_pending] + urls
    seen_fetch_urls: set[str] = set()
    for url in candidate_urls:
        try:
            key = canonicalize_wechat_url(url)
        except ValueError:
            continue
        if key in seen_fetch_urls:
            continue
        seen_fetch_urls.add(key)
        known = pool_by_url.get(key)
        known_date = _parse_date_value((known or {}).get("publish_time"))
        if known_date is not None and not (start_date <= known_date <= end_date):
            continue
        if known and _read_pool_body(known.get("body_markdown_path")):
            continue
        fetch_urls.append(url)

    fetch_limit = max(1, min(int(config.get("wechat_exact_fetch_limit_per_refresh", 20)), 50))
    deferred = max(0, len(fetch_urls) - fetch_limit)
    fetch_urls = fetch_urls[:fetch_limit]

    if not candidate_urls and not pool_stats_before.get("stored_article_rows"):
        return [], {
            "status": "skipped",
            "urls_total": 0,
            "urls_succeeded": 0,
            "items_seen": 0,
            "pool_total": 0,
            "in_window": 0,
            "ready": 0,
            "pending": 0,
            "failed": 0,
            "errors": [],
            "note": "公众号文章库尚未加入或导入文章",
            "discovery": discovery,
        }

    errors: list[dict[str, str]] = []
    succeeded = 0
    if fetch_urls:
        workers = min(4, len(fetch_urls))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(_fetch_wechat_url, url, timeout): url for url in fetch_urls}
            for future in as_completed(future_map):
                requested_url = future_map[future]
                try:
                    record = future.result()
                    body_path = _cache_fetched_wechat_body(record)
                    key = canonicalize_wechat_url(record.get("url"))
                    existing = pool_by_url.get(key) or {}
                    account_name = _normalize_space(existing.get("account_name")) or _normalize_space(record.get("publisher")) or "待归属公众号"
                    pool.add_urls(
                        [
                            {
                                "url": key,
                                "title": record.get("title"),
                                "publish_time": record.get("published_at"),
                                "author": record.get("publisher"),
                                "digest": record.get("summary"),
                                "body_markdown_path": body_path,
                                "fetch_mode": (
                                    "known_url_public_exporter"
                                    if record.get("source_type") == "wechat_public_exporter_body"
                                    else "known_url"
                                ),
                                "credential_status": "not_required",
                                "error": "",
                            }
                        ],
                        account_name=account_name,
                    )
                    keep_record(record)
                    succeeded += 1
                except Exception as exc:
                    message = _safe_error(exc)
                    errors.append({"url": requested_url, "error": message})
                    try:
                        key = canonicalize_wechat_url(requested_url)
                        existing = pool_by_url.get(key) or {}
                        pool.add_urls(
                            [{"url": key, "error": message, "fetch_mode": "known_url"}],
                            account_name=_normalize_space(existing.get("account_name")) or "待归属公众号",
                        )
                    except Exception:
                        pass

    pool_stats = pool.get_stats()
    window_stats = pool.get_stats(start=start_date, end=end_date)
    records = list(records_by_url.values()) + followup_records
    followup_degraded = followup_status.get("status") in {"partial", "error"}
    if (errors or followup_degraded) and records:
        status = "partial"
    elif (errors or followup_degraded) and not records:
        status = "error"
    else:
        status = "ok"
    return records, {
        "status": status,
        "urls_total": len(fetch_urls),
        "urls_succeeded": succeeded,
        "items_seen": len(records),
        "pool_total": pool_stats.get("stored_article_rows", 0),
        "account_count": pool_stats.get("accounts", 0),
        "in_window": window_stats.get("stored_article_rows", 0),
        "ready": pool_stats.get("ready", 0),
        "pending": pool_stats.get("pending", 0),
        "failed": pool_stats.get("failed", 0),
        "discovered_total": pool_stats.get("discovered_total", 0),
        "discovered_accounts": pool_stats.get("discovered_accounts", 0),
        "last_discovery_at": pool_stats.get("last_discovery_at", ""),
        "ready_in_window": window_stats.get("ready", 0),
        "pending_in_window": window_stats.get("pending", 0),
        "failed_in_window": window_stats.get("failed", 0),
        "deferred": deferred,
        "errors": errors[:10],
        "discovery": discovery,
        "followup_news": followup_status,
        "evidence_policy": "已缓存正文可作为原文页；只有历史标题的记录仍是 discovery_only。抓取时间永不替代发布日期",
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _no_cache_report(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    as_of = _coerce_as_of(None)
    max_candidates = max(1, min(5, int(config.get("max_candidates", _DEFAULT_MAX))))
    return {
        "window": _window(as_of, int(config.get("window_days", _DEFAULT_WINDOW_DAYS))),
        "status": "no_cache",
        "source_status": {},
        "candidates": [],
        "empty_slots": max_candidates,
        "generated_at": None,
        "method": "event_first_no_total_score",
    }


def load_cached_report() -> dict[str, Any]:
    """Load the last successful report; return a structural no-cache report if unavailable."""

    try:
        config = _load_config()
    except Exception:
        config = {}
    if not OUTPUT_PATH.exists():
        return _no_cache_report(config)
    try:
        payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8-sig"))
        required = {"window", "status", "source_status", "candidates", "empty_slots"}
        if not isinstance(payload, dict) or not required.issubset(payload):
            raise ValueError("缓存报告结构不完整")
        return payload
    except Exception as exc:
        report = _no_cache_report(config)
        report["status"] = "cache_invalid"
        report["cache_error"] = _safe_error(exc)
        return report


def refresh_report(as_of: Any = None) -> dict[str, Any]:
    """Refresh the seven-day report and atomically replace cache only on source success."""

    config = _load_config()
    as_of_date = _coerce_as_of(as_of)
    records: list[dict[str, Any]] = []
    source_status: dict[str, Any] = {}

    for name, collector in (("google_news", _collect_google_news), ("wechat", _collect_wechat)):
        try:
            if name == "wechat":
                collected, status = collector(config, as_of=as_of_date)
            else:
                collected, status = collector(config)
            records.extend(collected)
            source_status[name] = status
        except Exception as exc:
            source_status[name] = {"status": "error", "errors": [{"error": _safe_error(exc)}]}

    usable_source = any(
        isinstance(status, dict) and status.get("status") in {"ok", "partial"}
        for status in source_status.values()
    )
    if not usable_source:
        cached = load_cached_report()
        attempted_window = _window(as_of_date, int(config.get("window_days", _DEFAULT_WINDOW_DAYS)))
        if cached.get("status") not in {"no_cache", "cache_invalid"}:
            stale = copy.deepcopy(cached)
            stale["status"] = "stale_cache"
            stale["refresh_attempt"] = {
                "window": attempted_window,
                "source_status": source_status,
                "attempted_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
                "note": "本次所有信源均失败，磁盘中的上一成功报告未被覆盖",
            }
            return stale
        failed = _no_cache_report(config)
        failed["window"] = attempted_window
        failed["status"] = "refresh_failed"
        failed["source_status"] = source_status
        failed["note"] = "本次所有信源均失败，未写入报告"
        return failed

    report = _build_report(records, as_of_date, config, source_status)
    degraded = any(
        isinstance(status, dict) and status.get("status") in {"partial", "error"}
        for status in source_status.values()
    )
    if degraded and not report.get("candidates"):
        cached = load_cached_report()
        if cached.get("candidates"):
            stale = copy.deepcopy(cached)
            stale["status"] = "stale_cache"
            stale["refresh_attempt"] = {
                "window": report.get("window"),
                "source_status": source_status,
                "attempted_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
                "note": "本轮信源明显降级且未得到候选，保留上一次非空报告，未覆盖磁盘缓存",
            }
            return stale
    _atomic_write_json(OUTPUT_PATH, report)
    return report


def _normalize_wechat_url(url: str) -> str | None:
    try:
        parsed = urlsplit(_normalize_space(url))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"} or host != "mp.weixin.qq.com":
        return None
    if not parsed.path.startswith(("/s", "/mp/")):
        return None
    netloc = host
    if parsed.port and parsed.port not in {80, 443}:
        netloc = f"{host}:{parsed.port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def add_wechat_url(url: str) -> dict[str, Any]:
    """Validate and append one known WeChat article URL to the formal input list."""

    normalized = _normalize_wechat_url(url)
    if not normalized:
        return {
            "ok": False,
            "status": "invalid",
            "url": _normalize_space(url),
            "message": "仅接受 mp.weixin.qq.com 的公众号文章 URL",
        }
    existing = _read_wechat_urls()
    if normalized in existing:
        return {
            "ok": True,
            "status": "exists",
            "url": normalized,
            "count": len(existing),
            "message": "该公众号文章已在正式输入列表中",
        }

    WECHAT_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    updated = existing + [normalized]
    fd, temp_name = tempfile.mkstemp(
        prefix=WECHAT_URLS_PATH.name + ".",
        suffix=".tmp",
        dir=str(WECHAT_URLS_PATH.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(updated) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, WECHAT_URLS_PATH)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "status": "added",
        "url": normalized,
        "count": len(updated),
        "message": "已加入公众号正式输入列表；刷新后按真实发布日期判断是否进入七日窗口",
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="DealScope 一级市场证据雷达")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--refresh", action="store_true", help="抓取并刷新周度报告")
    action.add_argument("--show", action="store_true", help="显示最近一次成功报告")
    action.add_argument("--add-wechat", metavar="URL", help="添加一个公众号文章 URL")
    parser.add_argument("--as-of", help="报告基准日，格式 YYYY-MM-DD")
    args = parser.parse_args()

    if args.add_wechat:
        result = add_wechat_url(args.add_wechat)
    elif args.refresh:
        result = refresh_report(args.as_of)
    else:
        result = load_cached_report()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"invalid", "refresh_failed", "cache_invalid"} else 1


if __name__ == "__main__":
    raise SystemExit(_main())
