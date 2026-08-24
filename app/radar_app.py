"""DealScope 一级市场证据雷达的独立本地界面。

这套界面故意与旧的九维评分工作台隔离：周度雷达只回答一个问题——
本周发生了什么，使某个项目需要进入待核验清单。
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weekly_radar import (  # noqa: E402
    add_wechat_url,
    discover_wechat_sources,
    load_cached_report,
    refresh_report,
)
from wechat_source_pool import WeChatSourcePool  # noqa: E402


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
LOCAL_DEEP_WORKBENCH_HOME_URL = "http://127.0.0.1:8787/"
# Backward-compatible public constant used by the local test and launcher contract.
DEEP_WORKBENCH_HOME_URL = LOCAL_DEEP_WORKBENCH_HOME_URL
_refresh_lock = threading.Lock()
_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_SUCCESSFUL_REFRESH_STATES = {"ok", "partial", "empty"}
_runtime_state: dict[str, Any] = {
    "refreshing": False,
    "last_error": "",
    "last_started_at": "",
    "last_finished_at": "",
    "last_attempt": {},
}
_wechat_pool: WeChatSourcePool | None = None
_wechat_pool_lock = threading.Lock()


def _public_readonly_mode() -> bool:
    return os.getenv("DEALSCOPE_MODE", "").strip().lower() == "public_readonly"


def _public_live_mode() -> bool:
    return os.getenv("DEALSCOPE_MODE", "").strip().lower() == "public_live"


def _public_cloud_mode() -> bool:
    return _public_readonly_mode() or _public_live_mode()


def _same_origin_request() -> bool:
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == request.host.casefold()


def _workbench_home_url() -> str:
    configured = os.getenv("DEALSCOPE_DEEP_BASE_URL", "").strip()
    return configured or LOCAL_DEEP_WORKBENCH_HOME_URL


def _get_wechat_pool() -> WeChatSourcePool:
    global _wechat_pool
    if _wechat_pool is None:
        with _wechat_pool_lock:
            if _wechat_pool is None:
                _wechat_pool = WeChatSourcePool()
    return _wechat_pool


def _wechat_window() -> tuple[date, date]:
    end = _today_local()
    return end - timedelta(days=6), end


def _wechat_stats() -> dict[str, Any]:
    pool = _get_wechat_pool()
    start, end = _wechat_window()
    total = pool.get_stats()
    window = pool.get_stats(start=start, end=end)
    discovered_in_window = 0
    for row in pool.list_articles(scope="discovered", limit=10_000):
        raw = str(row.get("discovered_at") or "").strip()
        if not raw:
            continue
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            discovered_day = moment.astimezone(_SHANGHAI_TZ).date()
        except ValueError:
            continue
        if start <= discovered_day <= end:
            discovered_in_window += 1
    return {
        "total": total.get("stored_article_rows", 0),
        "pool_total": total.get("stored_article_rows", 0),
        "account_count": total.get("accounts", 0),
        "in_window": window.get("stored_article_rows", 0),
        "ready": total.get("ready", 0),
        "pending": total.get("pending", 0),
        "failed": total.get("failed", 0),
        "ready_in_window": window.get("ready", 0),
        "pending_in_window": window.get("pending", 0),
        "failed_in_window": window.get("failed", 0),
        "last_import_at": total.get("last_import_at", ""),
        "discovered_total": total.get("discovered_total", 0),
        "discovered_accounts": total.get("discovered_accounts", 0),
        "discovered_in_window": discovered_in_window,
        "last_discovery_at": total.get("last_discovery_at", ""),
    }


def _wechat_row_status(row: dict[str, Any]) -> str:
    if str(row.get("error") or "").strip():
        return "failed"
    has_body = bool(str(row.get("body_markdown_path") or row.get("html_path") or "").strip())
    if str(row.get("discovered_at") or "").strip() and not has_body:
        return "discovered"
    return "ready" if has_body and row.get("publish_time") else "pending"


def _wechat_public_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    if str(item.get("account_name") or "").strip().casefold() in {"n/a", "na", "none", "unknown"}:
        item["account_name"] = "公众号待识别"
    status = _wechat_row_status(item)
    published = item.get("publish_time")
    if isinstance(published, (int, float)) and published > 0:
        item["published_at"] = datetime.fromtimestamp(float(published), _SHANGHAI_TZ).date().isoformat()
    else:
        item["published_at"] = ""
    fetch_mode = str(item.get("fetch_mode") or "")
    if str(item.get("discovered_at") or "").strip():
        item["source_kind"] = "discovery"
    elif fetch_mode in {"known_url", "manual_url", "known_url_public_exporter"}:
        item["source_kind"] = "manual"
    elif fetch_mode in {"enhanced_export", "body_export"}:
        item["source_kind"] = "exporter"
    else:
        item["source_kind"] = "import"
    item["status"] = status
    for private_field in (
        "body_markdown_path",
        "html_path",
        "image_dir",
        "comments_path",
        "comment_replies_path",
        "error",
    ):
        item.pop(private_field, None)
    return item


@app.before_request
def enforce_local_request():
    if _public_readonly_mode():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        return jsonify(
            {
                "ok": False,
                "message": "公开在线版为只读演示；联网刷新、导入和写入功能请在本地完整版使用。",
            }
        ), 403

    if _public_live_mode():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        if request.method == "POST" and request.endpoint == "api_refresh" and _same_origin_request():
            return None
        return jsonify(
            {
                "ok": False,
                "message": "公开在线版仅允许同源触发受冷却保护的 RSS 更新；导入、删除和登录仍只在本地完整版开放。",
            }
        ), 403

    remote = (request.remote_addr or "").split("%", 1)[0]
    if remote not in {"127.0.0.1", "::1"}:
        abort(403)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin", "").strip()
        if origin and (urlparse(origin).hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            abort(403)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_text(*values: Any, default: str = "待核实") -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _today_local() -> date:
    return datetime.now(_SHANGHAI_TZ).date()


def _parse_local_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=_SHANGHAI_TZ)
        return current.astimezone(_SHANGHAI_TZ).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        current = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=_SHANGHAI_TZ)
        return current.astimezone(_SHANGHAI_TZ).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _freshness_metadata(window_end: Any, generated_at: Any, today: date | None = None) -> dict[str, Any]:
    today = today or _today_local()
    window_end_date = _parse_local_date(window_end)
    generated_date = _parse_local_date(generated_at)
    known_dates = [item for item in (window_end_date, generated_date) if item is not None]
    age_days = max((today - item).days for item in known_dates) if known_dates else 0
    age_days = max(0, age_days)
    has_future_date = any(item > today for item in known_dates)
    is_stale = (
        window_end_date is None
        or generated_date is None
        or window_end_date < today
        or generated_date < today
        or has_future_date
    )

    data_as_of = window_end_date.isoformat() if window_end_date else "待确认"
    if not known_dates:
        freshness_state = "missing"
        freshness_label = "尚无可用缓存，正在等待首次更新。"
    elif has_future_date:
        freshness_state = "stale"
        freshness_label = f"数据截至 {data_as_of}，缓存日期异常，请重新更新。"
    elif is_stale and age_days:
        freshness_state = "stale"
        freshness_label = f"数据截至 {data_as_of}，缓存已过期 {age_days} 天。"
    elif is_stale:
        freshness_state = "stale"
        freshness_label = f"数据截至 {data_as_of}，缓存生成时间不可确认。"
    else:
        freshness_state = "fresh"
        freshness_label = f"数据截至 {data_as_of}，缓存为今日最新。"

    return {
        "data_as_of": data_as_of,
        "cache_age_days": age_days,
        "is_stale": is_stale,
        "freshness_state": freshness_state,
        "freshness_label": freshness_label,
    }


def _source_error_summary(source_status: Any) -> str:
    messages: list[str] = []
    for status in _as_dict(source_status).values():
        for error in _as_list(_as_dict(status).get("errors")):
            message = _first_text(_as_dict(error).get("error"), default="")
            if message and message not in messages:
                messages.append(message)
            if len(messages) >= 2:
                return "；".join(messages)
    return "；".join(messages)


def _normalize_candidate(raw: Any, rank: int) -> dict[str, Any]:
    item = _as_dict(raw)
    raw_event = item.get("event") or item.get("trigger")
    event = _as_dict(raw_event)
    evidence = _as_dict(item.get("primary_evidence"))
    if not evidence:
        evidence = _as_dict(item.get("source"))
    if not evidence:
        sources = _as_list(item.get("sources"))
        evidence = _as_dict(sources[0]) if sources else {}

    fit_tags = item.get("fit_tags") or _as_dict(item.get("fit")).get("tags") or []
    if isinstance(fit_tags, str):
        fit_tags = [part.strip() for part in fit_tags.replace("，", ",").split(",") if part.strip()]

    verification = _first_text(
        evidence.get("verification_status"),
        item.get("verification_status"),
        default="待原文核验",
    )
    verified = verification in {"verified", "已核验", "原文核验通过", "cross_checked"}
    decision = _first_text(item.get("decision"), default="进入待核验清单")
    company_name = _first_text(
        item.get("company_name"), item.get("company"), item.get("entity"), default="主体待确认"
    )
    workbench_url = f"{_workbench_home_url()}?{urlencode({'q': company_name, 'company': company_name})}"

    return {
        "rank": rank,
        "company_name": company_name,
        "workbench_url": workbench_url,
        "legal_entity": _first_text(item.get("legal_entity"), default="工商主体待核实"),
        "decision": decision,
        "event_fact": _first_text(
            event.get("fact"),
            event.get("what_happened"),
            item.get("what_happened"),
            raw_event if isinstance(raw_event, str) else None,
            default="事件事实待核实",
        ),
        "event_date": _first_text(event.get("event_date"), item.get("event_date"), default="日期待核实"),
        "date_basis": _first_text(event.get("date_basis"), item.get("date_basis"), default="事件发生日"),
        "core_variable": _first_text(
            event.get("core_variable"), item.get("core_variable"), default="核心变量待判断"
        ),
        "before_state": _first_text(event.get("before_state"), default="此前状态未知"),
        "after_state": _first_text(event.get("after_state"), default="变化后状态待核实"),
        "admission_sentence": _first_text(
            item.get("admission_sentence"),
            item.get("why_enter_pool"),
            item.get("entry_reason"),
            _as_dict(item.get("admission")).get("sentence"),
            default="发现了需要核实的新变化，因此进入待核验清单。",
        ),
        "fit_reason": _first_text(
            item.get("fit_reason"), _as_dict(item.get("fit")).get("reason"), default="匹配理由待核实"
        ),
        "fit_tags": fit_tags[:3],
        "publisher": _first_text(evidence.get("publisher"), evidence.get("source"), default="发布主体待确认"),
        "publisher_type": _first_text(
            evidence.get("publisher_type"), evidence.get("source_type"), default="信源类型待确认"
        ),
        "source_title": _first_text(evidence.get("title"), default="原文标题待核实"),
        "source_url": _first_text(evidence.get("url"), evidence.get("source_url"), default=""),
        "source_quote": _first_text(evidence.get("exact_quote"), evidence.get("quote"), default="尚无正文逐字引文"),
        "published_at": _first_text(
            evidence.get("published_at"),
            evidence.get("article_published_at"),
            item.get("event_date") if "文章发布日期" in str(item.get("date_basis") or "") else None,
            default="发布时间待核实",
        ),
        "verification": verification,
        "verified": verified,
        "corroborated": bool(item.get("corroborating_evidence")) or bool(item.get("cross_checked")),
        "key_unknown": _first_text(item.get("key_unknown"), item.get("critical_unknown"), default="需确认事件与主体的真实性及实质影响"),
        "next_action": _first_text(
            item.get("next_action"),
            item.get("next_check"),
            item.get("next_verification"),
            default="打开原文并向公司或相关方交叉核实",
        ),
    }


def _normalize_report(raw: Any, today: date | None = None) -> dict[str, Any]:
    report = _as_dict(raw)
    window = _as_dict(report.get("window"))
    raw_status = report.get("status")
    status = _as_dict(raw_status)
    source_status = _as_dict(report.get("source_status"))
    refresh_attempt = _as_dict(report.get("refresh_attempt"))
    candidates = [
        _normalize_candidate(item, idx)
        for idx, item in enumerate(_as_list(report.get("candidates"))[:5], start=1)
    ]
    empty_count = max(0, 5 - len(candidates))
    run_state = _first_text(
        status.get("state"),
        raw_status if isinstance(raw_status, str) else None,
        report.get("run_status"),
        default="not_run",
    )
    default_messages = {
        "ok": f"更新完成，本期找到 {len(candidates)} 个项目",
        "partial": f"部分信源受限，本期找到 {len(candidates)} 个项目",
        "empty": "检索完成，本期没有达到门槛的变化",
        "no_cache": "首次启动，正在生成近 7 天待核验清单",
        "refresh_failed": "本轮信源均不可用，未生成新报告",
        "stale_cache": "本轮更新失败，当前保留上一次成功结果",
        "cache_invalid": "本地报告不可用，请重新更新",
    }
    generated_at = _first_text(report.get("generated_at"), default="尚未生成")
    window_end = _first_text(
        window.get("end"), window.get("end_date"), report.get("window_end"), default="待生成"
    )
    freshness = _freshness_metadata(window_end, generated_at, today=today)
    latest_source_status = _as_dict(refresh_attempt.get("source_status")) or source_status
    refresh_error = _source_error_summary(latest_source_status)
    if not refresh_error:
        refresh_error = _first_text(report.get("cache_error"), default="")
    return {
        "mode": _first_text(report.get("mode"), default="weekly_event_radar"),
        "generated_at": generated_at,
        "window_start": _first_text(
            window.get("start"), window.get("start_date"), report.get("window_start"), default="待生成"
        ),
        "window_end": window_end,
        "timezone": _first_text(window.get("timezone"), report.get("timezone"), default="Asia/Shanghai"),
        "run_state": run_state,
        "run_message": _first_text(
            status.get("message"),
            report.get("status_message"),
            default=default_messages.get(run_state, "点击更新开始检索"),
        ),
        "source_status": source_status,
        "candidates": candidates,
        "empty_slots": list(range(len(candidates) + 1, 6)),
        "candidate_count": len(candidates),
        "empty_count": empty_count,
        "refresh_attempt": refresh_attempt,
        "refresh_error": refresh_error,
        "needs_refresh": freshness["is_stale"] or run_state in {
            "no_cache",
            "refresh_failed",
            "stale_cache",
            "cache_invalid",
            "failed",
            "error",
        },
        **freshness,
    }


def _refresh_now() -> dict[str, Any]:
    if _public_live_mode():
        cached = load_cached_report()
        raw_generated = str(cached.get("generated_at") or "").strip()
        if raw_generated and cached.get("synthetic") is not True:
            try:
                generated = datetime.fromisoformat(raw_generated.replace("Z", "+00:00"))
                if generated.tzinfo is None:
                    generated = generated.replace(tzinfo=_SHANGHAI_TZ)
                cooldown = max(60, min(int(os.getenv("DEALSCOPE_REFRESH_COOLDOWN_SECONDS", "900")), 3600))
                age_seconds = int((datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds())
                if 0 <= age_seconds < cooldown:
                    normalized = _normalize_report(cached)
                    retry_after = cooldown - age_seconds
                    finished_at = datetime.now(_SHANGHAI_TZ).isoformat(timespec="seconds")
                    message = f"数据已是最新；约 {max(1, (retry_after + 59) // 60)} 分钟后可再次联网更新。"
                    attempt = {
                        "ok": True,
                        "run_state": normalized["run_state"],
                        "message": message,
                        "started_at": raw_generated,
                        "finished_at": finished_at,
                        "candidate_count": normalized["candidate_count"],
                        "cached": True,
                    }
                    _runtime_state.update(
                        last_error="",
                        last_finished_at=finished_at,
                        last_attempt=attempt,
                    )
                    return {
                        "ok": True,
                        "busy": False,
                        "cached": True,
                        "run_state": normalized["run_state"],
                        "candidate_count": normalized["candidate_count"],
                        "message": message,
                        "data_as_of": normalized["data_as_of"],
                        "cache_age_days": normalized["cache_age_days"],
                        "is_stale": normalized["is_stale"],
                        "retry_after_seconds": retry_after,
                        "last_attempt": attempt,
                    }
            except (TypeError, ValueError, OverflowError):
                pass

    if not _refresh_lock.acquire(blocking=False):
        return {
            "ok": False,
            "busy": True,
            "run_state": "busy",
            "message": "已有更新任务正在运行，请稍候。",
            "last_attempt": _runtime_state.get("last_attempt") or {},
        }
    started_at = datetime.now(_SHANGHAI_TZ).isoformat(timespec="seconds")
    _runtime_state.update(
        refreshing=True,
        last_error="",
        last_started_at=started_at,
        last_attempt={
            "ok": None,
            "run_state": "running",
            "message": "正在检索近 7 天项目变化。",
            "started_at": started_at,
            "finished_at": "",
            "error_detail": "",
        },
    )
    try:
        report = refresh_report()
        normalized = _normalize_report(report)
        ok = normalized["run_state"] in _SUCCESSFUL_REFRESH_STATES and not normalized["is_stale"]
        finished_at = datetime.now(_SHANGHAI_TZ).isoformat(timespec="seconds")
        error_detail = normalized["refresh_error"] if not ok else ""
        attempt = {
            "ok": ok,
            "run_state": normalized["run_state"],
            "message": normalized["run_message"],
            "started_at": started_at,
            "finished_at": finished_at,
            "candidate_count": normalized["candidate_count"],
            "data_as_of": normalized["data_as_of"],
            "cache_age_days": normalized["cache_age_days"],
            "is_stale": normalized["is_stale"],
            "error_detail": error_detail,
            "source_status": _as_dict(normalized["refresh_attempt"].get("source_status"))
            or normalized["source_status"],
        }
        _runtime_state.update(
            last_error=(f"{normalized['run_message']}：{error_detail}" if error_detail else normalized["run_message"])
            if not ok
            else "",
            last_finished_at=finished_at,
            last_attempt=attempt,
        )
        return {
            "ok": ok,
            "busy": False,
            "run_state": normalized["run_state"],
            "candidate_count": normalized["candidate_count"],
            "message": normalized["run_message"],
            "data_as_of": normalized["data_as_of"],
            "cache_age_days": normalized["cache_age_days"],
            "is_stale": normalized["is_stale"],
            "error_detail": error_detail,
            "last_attempt": attempt,
        }
    except Exception as exc:
        finished_at = datetime.now(_SHANGHAI_TZ).isoformat(timespec="seconds")
        message = f"{type(exc).__name__}: {exc}"
        attempt = {
            "ok": False,
            "run_state": "error",
            "message": "更新未完成，请查看错误信息后重试。",
            "started_at": started_at,
            "finished_at": finished_at,
            "error_detail": message,
        }
        _runtime_state.update(
            last_error=message,
            last_finished_at=finished_at,
            last_attempt=attempt,
        )
        traceback.print_exc()
        return {
            "ok": False,
            "busy": False,
            "run_state": "error",
            "message": attempt["message"],
            "error_detail": message,
            "last_attempt": attempt,
        }
    finally:
        _runtime_state["refreshing"] = False
        _refresh_lock.release()


@app.get("/")
def index():
    report = _normalize_report(load_cached_report())
    return render_template(
        "radar.html",
        report=report,
        runtime=_runtime_state,
        workbench_home_url=_workbench_home_url(),
        public_readonly=_public_readonly_mode(),
        public_live=_public_live_mode(),
        public_cloud=_public_cloud_mode(),
    )


@app.get("/health")
def health():
    if _public_cloud_mode():
        return jsonify(
            {
                "ok": True,
                "service": "WeeklyProjectRadar",
                "mode": "public_live" if _public_live_mode() else "public_readonly",
                "refreshing": bool(_runtime_state["refreshing"]),
                "last_finished_at": str(_runtime_state["last_finished_at"] or ""),
            }
        )
    return jsonify(
        {
            "ok": True,
            "service": "WeeklyProjectRadar",
            "refreshing": _runtime_state["refreshing"],
            "last_error": _runtime_state["last_error"],
            "last_started_at": _runtime_state["last_started_at"],
            "last_finished_at": _runtime_state["last_finished_at"],
            "last_attempt": _runtime_state["last_attempt"],
        }
    )


@app.get("/api/report")
def api_report():
    return jsonify(_normalize_report(load_cached_report()))


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error: RequestEntityTooLarge):
    return jsonify({"ok": False, "message": "历史文件超过 10MB，请拆分后再导入。"}), 413


@app.post("/api/refresh")
def api_refresh():
    result = _refresh_now()
    status_code = 409 if result.get("busy") else (200 if result.get("ok") else 502)
    return jsonify(result), status_code


def _read_wechat_upload() -> tuple[bytes, str]:
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ValueError("请选择 JSON 或 CSV 历史文件。")
    filename = Path(upload.filename).name
    if Path(filename).suffix.lower() not in {".json", ".jsonl", ".csv"}:
        raise ValueError("仅支持 JSON 或 CSV 历史文件。")
    data = upload.stream.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("历史文件超过 10MB，请拆分后再导入。")
    if not data:
        raise ValueError("历史文件为空。")
    return data, filename


@app.get("/api/wechat/pool")
def api_wechat_pool():
    if _public_cloud_mode():
        empty_stats = {
            "total": 0,
            "pool_total": 0,
            "account_count": 0,
            "in_window": 0,
            "ready": 0,
            "pending": 0,
            "failed": 0,
            "ready_in_window": 0,
            "pending_in_window": 0,
            "failed_in_window": 0,
            "last_import_at": "",
            "discovered_total": 0,
            "discovered_accounts": 0,
            "discovered_in_window": 0,
            "last_discovery_at": "",
        }
        return jsonify(
            {
                "ok": True,
                "scope": "all",
                "stats": empty_stats,
                "total": 0,
                "offset": 0,
                "limit": 50,
                "rows": [],
                "public_readonly": _public_readonly_mode(),
                "public_live": _public_live_mode(),
            }
        )
    scope = str(request.args.get("scope") or "all").strip().lower()
    if scope not in {"all", "window", "pending", "failed", "discovered"}:
        return jsonify({"ok": False, "message": "不支持的文章库筛选条件。"}), 400
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 100))
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        return jsonify({"ok": False, "message": "分页参数不正确。"}), 400

    pool = _get_wechat_pool()
    start, end = _wechat_window()
    if scope == "window":
        rows = pool.list_articles(start=start, end=end, limit=limit, offset=offset)
        filtered_total = pool.get_stats(start=start, end=end).get("stored_article_rows", 0)
    else:
        query_scope = None if scope == "all" else scope
        rows = pool.list_articles(scope=query_scope, limit=limit, offset=offset)
        filtered_total = pool.get_stats(scope=query_scope).get("stored_article_rows", 0)
    return jsonify(
        {
            "ok": True,
            "scope": scope,
            "stats": _wechat_stats(),
            "total": filtered_total,
            "offset": offset,
            "limit": limit,
            "rows": [_wechat_public_row(row) for row in rows],
        }
    )


@app.post("/api/wechat/urls")
@app.post("/api/wechat/add")
def api_wechat_urls():
    payload = request.get_json(silent=True) or request.form
    raw_urls = payload.get("urls") or payload.get("url") or []
    if isinstance(raw_urls, list):
        urls = [str(item).strip() for item in raw_urls if str(item).strip()]
    else:
        urls = [part.strip() for part in str(raw_urls).replace("，", "\n").splitlines() if part.strip()]
    if not urls:
        return jsonify({"ok": False, "message": "请粘贴至少一个公众号文章链接。"}), 400
    if len(urls) > 500:
        return jsonify({"ok": False, "message": "一次最多加入 500 个公众号文章链接。"}), 400
    if any(len(url) > 2048 for url in urls):
        return jsonify({"ok": False, "message": "存在异常过长的文章链接，请检查后重试。"}), 400

    result = _get_wechat_pool().add_urls(urls)
    public_results = []
    for item in result.get("results", []):
        current = dict(item)
        if current.get("status") == "error":
            current["status"] = "invalid"
            current["ok"] = False
            current["message"] = current.get("error") or "无法识别该公众号文章链接。"
        else:
            current["ok"] = True
            current["message"] = "已加入文章库" if current.get("status") == "added" else "文章已在库中"
            # Keep the legacy input list in sync so the existing refresh path
            # can immediately attempt the newly pasted exact article URL.
            add_wechat_url(str(current.get("url") or ""))
        public_results.append(current)
    added = int(result.get("added") or 0)
    existing = int(result.get("exists") or 0)
    invalid = int(result.get("errors") or 0)
    ok = added + existing > 0
    return jsonify(
        {
            "ok": ok,
            "added_count": added,
            "existing_count": existing,
            "invalid_count": invalid,
            "results": public_results,
            "stats": _wechat_stats(),
            "message": f"文章库新增 {added} 篇，已存在 {existing} 篇，无法识别 {invalid} 篇。",
        }
    ), (200 if ok else 400)


@app.post("/api/wechat/discover")
def api_wechat_discover():
    """Run credential-free public-web discovery and persist leads locally."""

    result = discover_wechat_sources(force=True, pool=_get_wechat_pool())
    status = str(result.get("status") or "error")
    payload = {
        "ok": bool(result.get("ok")),
        "queued": status == "busy",
        "status": status,
        "message": str(result.get("message") or "公众号全网拓源已完成。"),
        "stats": _wechat_stats(),
        "discovery_stats": result.get("stats") or {},
        "pool_write": result.get("pool_write") or {},
        "finished_at": result.get("finished_at") or "",
        "evidence_policy": (
            "全网搜索只负责发现链接；未取得正文和真实发布日期的文章不会进入七日评分。"
        ),
    }
    if status == "busy":
        return jsonify(payload), 202
    if not payload["ok"]:
        return jsonify(payload), 502
    return jsonify(payload)


@app.post("/api/wechat/import/preview")
def api_wechat_import_preview():
    try:
        data, filename = _read_wechat_upload()
        preview = _get_wechat_pool().preview_import(data, filename)
        if int(preview.get("accepted_records") or 0) > 10_000:
            raise ValueError("单次最多导入 10,000 篇，请拆分历史文件。")
        preview["articles"] = [
            {
                "account_name": item.get("account_name", ""),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "publish_time": item.get("publish_time"),
            }
            for item in _as_list(preview.get("articles"))
        ]
        return jsonify({"ok": True, **preview})
    except (ValueError, UnicodeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception:
        traceback.print_exc()
        return jsonify({"ok": False, "message": "历史文件无法解析，请确认它来自公众号导出器。"}), 400


@app.post("/api/wechat/import")
def api_wechat_import():
    try:
        data, filename = _read_wechat_upload()
        fingerprint = str(request.form.get("fingerprint") or "").strip()
        pool = _get_wechat_pool()
        result = pool.import_file(data, filename, fingerprint)
        start, end = _wechat_window()
        # Only queue articles that can actually fall in the current radar
        # window; old history is retained in the library without being
        # re-fetched on every refresh.
        for row in pool.records_for_window(start, end)[:100]:
            add_wechat_url(str(row.get("url") or ""))
        invalid = int(result.get("rejected_records") or 0)
        stats = _wechat_stats()
        return jsonify(
            {
                "ok": True,
                "added_count": int(result.get("added") or 0),
                "existing_count": int(result.get("exists") or 0),
                "updated_count": int(result.get("updated") or 0),
                "invalid_count": invalid,
                "publish_groups": int(result.get("publish_groups") or 0),
                "expanded_url_items": int(result.get("expanded_url_items") or 0),
                "original_articles": int(result.get("original_articles") or 0),
                "credential_fields_ignored": int(result.get("credential_fields_ignored") or 0),
                "stats": stats,
                "message": "历史文件已写入本地公众号文章库。",
            }
        )
    except (ValueError, UnicodeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception:
        traceback.print_exc()
        return jsonify({"ok": False, "message": "历史文件导入失败，原文章库未被改动。"}), 400


@app.delete("/api/wechat/pool/<int:article_id>")
def api_wechat_remove(article_id: int):
    removed = _get_wechat_pool().remove(article_id)
    if not removed:
        return jsonify({"ok": False, "message": "文章不存在或已经移出。"}), 404
    return jsonify({"ok": True, "stats": _wechat_stats(), "message": "已从本地文章库移出。"})


def _should_warm_cache() -> bool:
    return bool(_normalize_report(load_cached_report()).get("needs_refresh"))


def _warm_cache_in_background() -> None:
    if not _should_warm_cache():
        return
    thread = threading.Thread(target=_refresh_now, name="weekly-radar-refresh", daemon=True)
    thread.start()


if __name__ == "__main__":
    _warm_cache_in_background()
    port = int(os.getenv("WEEKLY_RADAR_PORT", "8791"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
