from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

from collectors.url_capture import capture_urls
from llm_extract import extract_batch
from score_engine import build_report

try:
    from auto_search import run_auto_search
    AUTO_SEARCH_IMPORT_ERROR: str | None = None
except ImportError as exc:
    run_auto_search = None
    AUTO_SEARCH_IMPORT_ERROR = str(exc)

ROOT = Path(__file__).resolve().parent
URLS_PATH = ROOT / "data" / "input" / "urls.txt"
OUT_DIR = ROOT / "data" / "output"
RAW_DIR = ROOT / "data" / "raw"
TEMPLATE_PATH = ROOT / "config" / "source_templates.json"
LATEST_REPORT_PATH = OUT_DIR / "latest_report.json"
LATEST_ATTEMPT_PATH = OUT_DIR / "latest_pipeline_attempt.json"
DISCOVERY_LINKS_PATH = OUT_DIR / "discovery_links.json"

INTENSITY_CHOICES = {"standard", "deep", "epic"}
API_KEY_NAMES = ("BRAVE_API_KEY", "SERPER_API_KEY", "TAVILY_API_KEY")


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON without ever leaving a half-written report behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_attempt(summary: dict[str, Any]) -> None:
    payload = dict(summary)
    payload.setdefault("attempted_at", datetime.now(timezone.utc).isoformat())
    atomic_write_json(LATEST_ATTEMPT_PATH, payload)


def read_urls() -> list[str]:
    if not URLS_PATH.exists():
        return []
    return [
        line.strip()
        for line in URLS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def build_discovery_links(query: str) -> list[dict[str, Any]]:
    templates = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    encoded = quote_plus(query.strip())
    links: list[dict[str, Any]] = []
    for item in templates:
        url = item["template"].replace("{query}", encoded)
        links.append(
            {
                "name": item["name"],
                "group": item["group"],
                "url": url,
            }
        )
    return links


def get_api_key_status() -> dict[str, bool]:
    return {name: bool(os.getenv(name)) for name in API_KEY_NAMES}


def should_run_auto_search() -> bool:
    return any(get_api_key_status().values()) and run_auto_search is not None


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _source_domain(item: dict[str, Any]) -> str:
    domain = (item.get("domain") or "").strip().lower()
    if domain:
        return domain
    source_url = item.get("source_url") or item.get("url") or ""
    return urlparse(source_url).netloc.lower()


def normalize_auto_search_result(result: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "providers_used": [],
        "query_plan": [],
        "source_domains": [],
        "sources": [],
        "search_strategy": {},
        "source_coverage": {},
    }

    if not result:
        return evidence, meta

    if isinstance(result, list):
        evidence = _as_dict_list(result)
    elif isinstance(result, dict):
        evidence = _as_dict_list(result.get("evidence"))
        if not evidence and "results" in result:
            maybe_results = _as_dict_list(result.get("results"))
            if maybe_results and all("claim_type" in item for item in maybe_results):
                evidence = maybe_results

        meta["sources"] = _as_dict_list(result.get("sources")) or _as_dict_list(result.get("search_results"))
        meta["query_plan"] = _as_dict_list(result.get("query_plan"))

        search_strategy = result.get("search_strategy")
        if isinstance(search_strategy, dict):
            meta["search_strategy"] = dict(search_strategy)
            if not meta["query_plan"]:
                meta["query_plan"] = _as_dict_list(search_strategy.get("query_plan"))

        source_coverage = result.get("source_coverage")
        if isinstance(source_coverage, dict):
            meta["source_coverage"] = dict(source_coverage)

        providers = result.get("providers_used") or result.get("search_engines_used")
        if not providers and meta["search_strategy"]:
            providers = meta["search_strategy"].get("providers_used")
        if isinstance(providers, list):
            meta["providers_used"] = [str(item) for item in providers if str(item).strip()]

    domains = {
        _source_domain(item)
        for item in meta["sources"] + evidence
        if _source_domain(item)
    }
    meta["source_domains"] = sorted(domains)
    meta["evidence_count"] = len(evidence)
    meta["source_count"] = len(meta["sources"])
    return evidence, meta


def dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("entity", "")).strip(),
            str(item.get("claim_type", "")).strip(),
            str(item.get("stance", "")).strip(),
            str(item.get("source_url", "")).strip(),
            str(item.get("source_title", "")).strip(),
            str(item.get("published_at", "")).strip(),
            str(item.get("quote", "")).strip()[:240],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def compute_report_stats(report: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    confidence_scores = [
        float(candidate.get("confidence_score", 0.0))
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    domains = {
        urlparse(str(item.get("source_url", "")).strip()).netloc.lower()
        for item in evidence
        if item.get("source_url")
    }
    providers_used = []
    search_strategy = report.get("search_strategy")
    if isinstance(search_strategy, dict):
        providers = search_strategy.get("providers_used")
        if isinstance(providers, list):
            providers_used = [str(item) for item in providers if str(item).strip()]
    return {
        "candidate_count": len(candidates),
        "total_evidence": len(evidence),
        "source_domain_count": len(domains),
        "search_engines_used": providers_used,
        "average_confidence_score": round(sum(confidence_scores) / len(confidence_scores), 1) if confidence_scores else 0.0,
    }


def enrich_report(
    report: dict[str, Any],
    args: argparse.Namespace,
    discovery_links: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    manual_urls: list[str],
    auto_meta: dict[str, Any],
    manual_meta: dict[str, Any],
    api_key_status: dict[str, bool],
) -> dict[str, Any]:
    generated_at = report.get("generated_at") or datetime.now(timezone.utc).isoformat()

    report["generated_at"] = generated_at
    report["thesis"] = args.thesis
    report["company"] = args.company
    report["intensity"] = args.intensity
    report["discovery_links"] = discovery_links
    report["total_evidence"] = len(evidence)

    search_strategy = dict(report.get("search_strategy") or {})
    search_strategy.setdefault("profile", args.intensity)
    if auto_meta.get("providers_used"):
        search_strategy["providers_used"] = auto_meta["providers_used"]
    if auto_meta.get("query_plan"):
        search_strategy["query_plan"] = auto_meta["query_plan"]
    search_strategy["api_key_status"] = api_key_status
    search_strategy["auto_search_available"] = run_auto_search is not None
    search_strategy["auto_search_ran"] = bool(auto_meta.get("ran"))
    report["search_strategy"] = search_strategy

    source_coverage = dict(report.get("source_coverage") or {})
    source_coverage["result_count"] = len(evidence)
    source_coverage["top_domains"] = auto_meta.get("source_domains") or sorted(
        {
            urlparse(str(item.get("source_url", "")).strip()).netloc.lower()
            for item in evidence
            if item.get("source_url")
        }
    )
    source_coverage["traceable_result_count"] = sum(
        1 for item in evidence if item.get("source_url") and item.get("quote")
    )
    if auto_meta.get("source_count"):
        source_coverage["search_result_count"] = auto_meta["source_count"]
    report["source_coverage"] = source_coverage

    report["pipeline"] = {
        "auto_search_module_available": run_auto_search is not None,
        "auto_search_import_error": AUTO_SEARCH_IMPORT_ERROR,
        "auto_search_ran": bool(auto_meta.get("ran")),
        "auto_search_evidence_count": auto_meta.get("evidence_count", 0),
        "search_engines_used": auto_meta.get("providers_used", []),
        "manual_url_count": len(manual_urls),
        "manual_captured_pages": manual_meta.get("captured_pages", 0),
        "manual_capture_error_count": manual_meta.get("capture_error_count", 0),
        "manual_extraction_status": manual_meta.get("extraction_status", "empty"),
        "manual_evidence_count": manual_meta.get("evidence_count", 0),
        "merged_evidence_count": len(evidence),
    }

    meta = dict(report.get("meta") or {})
    stats = compute_report_stats(report, evidence)
    meta.update(
        {
            "generated_at": generated_at,
            "headline": meta.get("headline") or args.thesis,
            "key_source_count": source_coverage.get("search_result_count", len(evidence)),
            "total_evidence": len(evidence),
            "company_count": stats["candidate_count"],
            "avg_confidence_score": stats["average_confidence_score"],
            "run_source": "pipeline",
        }
    )
    report["meta"] = meta
    report["summary_stats"] = stats
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thesis", required=True, help="投资主线或搜索主题")
    parser.add_argument("--company", default="", help="可选的目标公司名")
    parser.add_argument(
        "--intensity",
        default="standard",
        choices=sorted(INTENSITY_CHOICES),
        help="自动搜索强度: standard / deep / epic",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    URLS_PATH.parent.mkdir(parents=True, exist_ok=True)

    discovery_links = build_discovery_links(args.thesis)
    atomic_write_json(DISCOVERY_LINKS_PATH, discovery_links)

    api_key_status = get_api_key_status()
    print(f"[管线] thesis={args.thesis} intensity={args.intensity}")
    if args.company:
        print(f"[管线] company={args.company}")

    auto_evidence: list[dict[str, Any]] = []
    auto_meta: dict[str, Any] = {
        "ran": False,
        "providers_used": [],
        "source_domains": [],
        "query_plan": [],
        "search_strategy": {},
        "source_coverage": {},
        "sources": [],
        "evidence_count": 0,
        "source_count": 0,
    }

    if should_run_auto_search():
        print("[搜索] 检测到可用 API Key，开始自动搜索。")
        try:
            result = run_auto_search(args.thesis, company_name=args.company or None, intensity=args.intensity)
            auto_evidence, normalized_meta = normalize_auto_search_result(result)
            auto_meta.update(normalized_meta)
            auto_meta["ran"] = True
            print(
                f"[搜索] 自动搜索完成，证据 {len(auto_evidence)} 条，"
                f"来源域名 {len(auto_meta.get('source_domains', []))} 个。"
            )
        except Exception as exc:
            auto_meta["error"] = str(exc)
            print(f"[搜索] 自动搜索失败，已降级继续: {exc}")
    else:
        reason = "未配置 API Key" if not any(api_key_status.values()) else "auto_search 模块不可用"
        if run_auto_search is None and AUTO_SEARCH_IMPORT_ERROR:
            reason = f"auto_search 不可导入: {AUTO_SEARCH_IMPORT_ERROR}"
        print(f"[搜索] 跳过自动搜索: {reason}")

    urls = read_urls()
    raw_paths: list[Path] = []
    manual_evidence: list[dict[str, Any]] = []
    manual_meta = {"captured_pages": 0, "evidence_count": 0}

    if urls:
        print(f"[采集] 读取到 {len(urls)} 条手工 URL，开始页面抓取。")
        try:
            capture_result = capture_urls(urls, RAW_DIR)
            raw_paths = list(capture_result)
            manual_meta["capture_diagnostics"] = list(getattr(capture_result, "diagnostics", []))
            manual_meta["capture_error_count"] = sum(
                1 for item in manual_meta["capture_diagnostics"] if item.get("status") == "error"
            )
            manual_meta["captured_pages"] = len(raw_paths)
            extraction_result = extract_batch(raw_paths, args.thesis) if raw_paths else []
            manual_evidence = list(extraction_result)
            manual_meta["extraction_status"] = getattr(extraction_result, "status", "empty")
            manual_meta["extraction_diagnostics"] = list(getattr(extraction_result, "diagnostics", []))
            manual_meta["evidence_count"] = len(manual_evidence)
            print(f"[采集] Playwright 抓取 {len(raw_paths)} 页，抽取证据 {len(manual_evidence)} 条。")
        except Exception as exc:
            manual_meta["error"] = str(exc)
            print(f"[采集] 手工 URL 抓取失败，已继续评分流程: {exc}")
    else:
        print("[采集] urls.txt 未提供 URL，跳过手工抓取。")

    merged_evidence = dedupe_evidence(auto_evidence + manual_evidence)
    if not merged_evidence:
        summary = {
            "ok": False,
            "status": "no_verified_evidence",
            "message": "本轮没有取得可核验原文证据，已保留上一次成功报告。",
            "thesis": args.thesis,
            "company": args.company,
            "intensity": args.intensity,
            "auto_search_ran": auto_meta.get("ran", False),
            "auto_search_error": auto_meta.get("error", ""),
            "manual_urls": len(urls),
            "manual_capture_error": manual_meta.get("error", ""),
            "manual_capture_diagnostics": manual_meta.get("capture_diagnostics", []),
            "manual_extraction_status": manual_meta.get("extraction_status", "empty"),
            "manual_extraction_diagnostics": manual_meta.get("extraction_diagnostics", []),
            "preserved_report": str(LATEST_REPORT_PATH) if LATEST_REPORT_PATH.exists() else "",
        }
        write_attempt(summary)
        print(json.dumps(summary, ensure_ascii=False))
        raise SystemExit(2)

    report = build_report(args.thesis, discovery_links, merged_evidence)
    report = enrich_report(
        report=report,
        args=args,
        discovery_links=discovery_links,
        evidence=merged_evidence,
        manual_urls=urls,
        auto_meta=auto_meta,
        manual_meta=manual_meta,
        api_key_status=api_key_status,
    )

    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    if not candidates:
        summary = {
            "ok": False,
            "status": "no_candidate_entities",
            "message": "已取得原文，但没有形成可识别的候选主体；已保留上一次成功报告。",
            "thesis": args.thesis,
            "company": args.company,
            "intensity": args.intensity,
            "merged_evidence": len(merged_evidence),
            "preserved_report": str(LATEST_REPORT_PATH) if LATEST_REPORT_PATH.exists() else "",
        }
        write_attempt(summary)
        print(json.dumps(summary, ensure_ascii=False))
        raise SystemExit(3)

    atomic_write_json(LATEST_REPORT_PATH, report)

    summary = {
        "ok": True,
        "thesis": args.thesis,
        "company": args.company,
        "intensity": args.intensity,
        "discovery_links": len(discovery_links),
        "auto_search_ran": auto_meta.get("ran", False),
        "auto_search_evidence": len(auto_evidence),
        "manual_urls": len(urls),
        "captured_pages": len(raw_paths),
        "manual_evidence": len(manual_evidence),
        "merged_evidence": len(merged_evidence),
        "candidates": len(report.get("candidates", [])),
        "search_engines_used": report.get("search_strategy", {}).get("providers_used", []),
        "source_domains": report.get("source_coverage", {}).get("top_domains", []),
        "report": str(LATEST_REPORT_PATH),
    }
    write_attempt(summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
