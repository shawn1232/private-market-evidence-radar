"""Safe adapter for downloading bodies of known WeChat article URLs.

The adapter delegates network work to the installed
``yichen-wechat-mp-batch-exporter/scripts/download_urls.py`` helper.  It does
not log in to WeChat and never consumes or returns credentials.
"""

from __future__ import annotations

import csv
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = ROOT / "data" / "wechat_pool" / "bodies"
SKILL_NAME = "yichen-wechat-mp-batch-exporter"
SCRIPT_RELATIVE_PATH = Path("scripts") / "download_urls.py"
SCRIPT_ENV = "YICHEN_WECHAT_DOWNLOAD_SCRIPT"
SKILL_DIR_ENV = "YICHEN_WECHAT_MP_BATCH_EXPORTER_DIR"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SENSITIVE_QUERY_KEYS = {
    "auth_key",
    "auth-key",
    "cookie",
    "cookies",
    "key",
    "pass_ticket",
    "token",
    "uin",
}
SENSITIVE_ERROR_RE = re.compile(
    r"(?i)([\"']?)(cookie|cookies|auth[-_]?key|token|pass_ticket|key|uin)\1"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(4)}"


def _validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(value) or value in {".", ".."} or ".." in value:
        raise ValueError("run_id must contain only letters, digits, '.', '_' or '-' and cannot contain '..'")
    return value


def _sanitize_error(value: Any) -> str:
    text = str(value or "").strip()
    return SENSITIVE_ERROR_RE.sub(lambda match: f"{match.group(2)}=[REDACTED]", text)[:500]


def _has_sensitive_query(url: str) -> bool:
    try:
        keys = {key.lower() for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
    except ValueError:
        return True
    return bool(keys & SENSITIVE_QUERY_KEYS)


def _reportable_url(raw_url: Any) -> str:
    text = str(raw_url or "").strip()
    sanitized = _sanitize_error(text)
    return "[REDACTED_INVALID_URL]" if _has_sensitive_query(text) or sanitized != text else text[:1000]


def normalize_wechat_url(raw_url: Any) -> tuple[str | None, str | None]:
    """Validate and canonicalize a public WeChat article URL."""
    text = str(raw_url or "").strip()
    if not text or any(ord(char) < 32 for char in text):
        return None, "empty or control-character URL"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None, "malformed URL"
    if parsed.scheme.lower() != "https":
        return None, "only https URLs are allowed"
    if parsed.username or parsed.password or parsed.netloc.lower() != "mp.weixin.qq.com":
        return None, "only https://mp.weixin.qq.com URLs are allowed"
    if not parsed.path or parsed.path == "/":
        return None, "article path is required"
    if _has_sensitive_query(text):
        return None, "credential-like query parameters are not allowed"
    canonical = urlunsplit(("https", "mp.weixin.qq.com", parsed.path, parsed.query, ""))
    return canonical, None


def _candidate_scripts(environment: Mapping[str, str]) -> list[Path]:
    candidates: list[Path] = []
    if environment.get(SCRIPT_ENV):
        candidates.append(Path(environment[SCRIPT_ENV]).expanduser())
    if environment.get(SKILL_DIR_ENV):
        candidates.append(Path(environment[SKILL_DIR_ENV]).expanduser() / SCRIPT_RELATIVE_PATH)
    if environment.get("CODEX_HOME"):
        candidates.append(Path(environment["CODEX_HOME"]).expanduser() / "skills" / SKILL_NAME / SCRIPT_RELATIVE_PATH)
    candidates.extend(
        [
            Path.home() / ".codex" / "skills" / SKILL_NAME / SCRIPT_RELATIVE_PATH,
            Path.home() / ".agents" / "skills" / SKILL_NAME / SCRIPT_RELATIVE_PATH,
        ]
    )
    return candidates


def resolve_downloader_script(
    script_path: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the downloader from an explicit path, env, or standard install."""
    env = os.environ if environment is None else environment
    candidates = [Path(script_path).expanduser()] if script_path else _candidate_scripts(env)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.name == "download_urls.py":
            return resolved
    raise FileNotFoundError(
        "download_urls.py was not found; pass script_path or set "
        f"{SCRIPT_ENV}/{SKILL_DIR_ENV}"
    )


def _is_sensitive_env_name(name: str) -> bool:
    upper = name.upper()
    return (
        "COOKIE" in upper
        or "TOKEN" in upper
        or "AUTH_KEY" in upper
        or "PASS_TICKET" in upper
        or "WXDOWN" in upper
        or upper == "UIN"
        or upper.endswith("_UIN")
        or upper.endswith("_API_KEY")
        or upper.startswith("WECHAT_CREDENTIAL")
    )


def _sanitized_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key): str(value) for key, value in source.items() if not _is_sensitive_env_name(str(key))}


def _failure(source_url: str, code: str, error: str, *, seq: str = "") -> dict[str, str]:
    return {
        "seq": seq,
        "source_url": source_url,
        "url": source_url,
        "status": "failed",
        "error_code": code,
        "error": _sanitize_error(error),
        "fetch_mode": "known_url_public_exporter",
        "credential_status": "not_required",
    }


def _safe_body_path(output_dir: Path, relative_value: str) -> Path | None:
    relative = Path(str(relative_value or ""))
    if not relative_value or relative.is_absolute() or relative.suffix.lower() != ".md":
        return None
    try:
        target = (output_dir / relative).resolve()
        target.relative_to(output_dir.resolve())
    except (OSError, ValueError):
        return None
    return target if target.is_file() else None


def _load_errors(errors_path: Path, requested: set[str]) -> dict[str, dict[str, str]]:
    if not errors_path.is_file():
        return {}
    try:
        payload = json.loads(errors_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        url, _ = normalize_wechat_url(item.get("source_url"))
        if url and url in requested:
            result[url] = {
                "seq": str(item.get("seq") or ""),
                "error": _sanitize_error(item.get("error") or "download failed"),
            }
    return result


def _parse_outputs(output_dir: Path, requested_urls: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    requested = set(requested_urls)
    index_path = output_dir / "index.csv"
    errors_path = output_dir / "errors.json"
    error_details = _load_errors(errors_path, requested)
    successes: dict[str, dict[str, Any]] = {}
    failures: dict[str, dict[str, str]] = {}

    if index_path.is_file():
        try:
            with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            rows = []
        for row in rows:
            url, _ = normalize_wechat_url(row.get("source_url"))
            if not url or url not in requested or url in successes or url in failures:
                continue
            seq = str(row.get("seq") or "")
            if str(row.get("status") or "").lower() == "success":
                body_path = _safe_body_path(output_dir, str(row.get("path") or ""))
                if body_path is None:
                    failures[url] = _failure(url, "invalid_body_path", "downloaded body path is missing or outside the run directory", seq=seq)
                    continue
                successes[url] = {
                    "seq": seq,
                    "account_name": "",
                    "fakeid": "",
                    "title": str(row.get("title") or ""),
                    "url": url,
                    "source_url": url,
                    "publish_time": "",
                    "author": "",
                    "digest": "",
                    "cover_url": "",
                    "body_markdown_path": str(body_path),
                    "html_path": "",
                    "image_dir": "",
                    "read_count": None,
                    "like_count": None,
                    "share_count": None,
                    "favorite_count": None,
                    "comment_count": None,
                    "comments_path": "",
                    "comment_replies_path": "",
                    "format": "markdown",
                    "status": "success",
                    "fetch_mode": "known_url_public_exporter",
                    "credential_status": "not_required",
                    "exported_at": str(row.get("downloaded_at") or _utc_now_iso()),
                    "error": "",
                }
            else:
                fallback = error_details.get(url, {})
                failures[url] = _failure(
                    url,
                    "download_failed",
                    str(row.get("error") or fallback.get("error") or "download failed"),
                    seq=seq or fallback.get("seq", ""),
                )

    for url in requested_urls:
        if url in successes or url in failures:
            continue
        detail = error_details.get(url)
        if detail:
            failures[url] = _failure(
                url,
                "download_failed",
                detail.get("error") or "download failed",
                seq=detail.get("seq", ""),
            )
        else:
            failures[url] = _failure(url, "missing_result", "downloader produced no result for this URL")
    return list(successes.values()), list(failures.values())


def _build_result(
    *,
    run_id: str,
    output_dir: Path,
    successes: list[dict[str, Any]],
    failures: list[dict[str, str]],
    accepted_count: int,
    requested_count: int,
    returncode: int | None,
) -> dict[str, Any]:
    if successes and failures:
        status = "partial"
    elif successes:
        status = "success"
    else:
        status = "failed"
    index_path = output_dir / "index.csv"
    errors_path = output_dir / "errors.json"
    return {
        "ok": status == "success",
        "status": status,
        "run_id": run_id,
        "output_dir": str(output_dir),
        "index_csv": str(index_path) if index_path.is_file() else "",
        "errors_json": str(errors_path) if errors_path.is_file() else "",
        "requested_count": requested_count,
        "accepted_count": accepted_count,
        "success_count": len(successes),
        "failure_count": len(failures),
        "successes": successes,
        "failures": failures,
        "failed_urls": [item["source_url"] for item in failures],
        "fetch_mode": "known_url_public_exporter",
        "credential_status": "not_required",
        "process_returncode": returncode,
    }


def fetch_known_wechat_articles(
    urls: Iterable[str] | str,
    *,
    output_root: str | Path | None = None,
    run_id: str | None = None,
    script_path: str | Path | None = None,
    python_executable: str | Path | None = None,
    request_timeout_seconds: int = 45,
    process_timeout_seconds: int = 300,
    sleep_seconds: float = 0.8,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Download known public article URLs and return pool-friendly records.

    All external work is performed by ``download_urls.py`` without login.
    Invalid URLs are returned as structured failures and are never passed to
    the child process.
    """
    if request_timeout_seconds <= 0 or process_timeout_seconds <= 0:
        raise ValueError("timeouts must be positive")
    if sleep_seconds < 0:
        raise ValueError("sleep_seconds cannot be negative")

    raw_urls = [urls] if isinstance(urls, str) else list(urls)
    requested_count = len(raw_urls)
    valid_urls: list[str] = []
    validation_failures: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        normalized, error = normalize_wechat_url(raw_url)
        if error:
            validation_failures.append(
                _failure(_reportable_url(raw_url), "invalid_url", error)
            )
            continue
        assert normalized is not None
        if normalized not in seen:
            seen.add(normalized)
            valid_urls.append(normalized)

    selected_run_id = _validate_run_id(run_id) if run_id else make_run_id()
    root = Path(output_root).expanduser().resolve() if output_root else DEFAULT_OUTPUT_ROOT.resolve()
    output_dir = (root / selected_run_id).resolve()
    output_dir.relative_to(root)

    if not valid_urls:
        return _build_result(
            run_id=selected_run_id,
            output_dir=output_dir,
            successes=[],
            failures=validation_failures,
            accepted_count=0,
            requested_count=requested_count,
            returncode=None,
        )

    try:
        downloader = resolve_downloader_script(script_path, environment=environment)
    except FileNotFoundError as exc:
        failures = validation_failures + [
            _failure(url, "downloader_not_found", str(exc)) for url in valid_urls
        ]
        return _build_result(
            run_id=selected_run_id,
            output_dir=output_dir,
            successes=[],
            failures=failures,
            accepted_count=len(valid_urls),
            requested_count=requested_count,
            returncode=None,
        )

    output_dir.mkdir(parents=True, exist_ok=False)
    input_path = output_dir / "requested_urls.txt"
    input_path.write_text("\n".join(valid_urls) + "\n", encoding="utf-8")
    command = [
        str(python_executable or sys.executable),
        str(downloader),
        "--file",
        str(input_path),
        "--format",
        "markdown",
        "--output-dir",
        str(output_dir),
        "--timeout",
        str(int(request_timeout_seconds)),
        "--sleep",
        str(float(sleep_seconds)),
    ]
    returncode: int | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=str(downloader.parent),
            capture_output=True,
            text=True,
            check=False,
            timeout=process_timeout_seconds,
            env=_sanitized_environment(environment),
        )
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        failures = validation_failures + [
            _failure(url, "timeout", f"downloader exceeded {process_timeout_seconds} seconds")
            for url in valid_urls
        ]
        return _build_result(
            run_id=selected_run_id,
            output_dir=output_dir,
            successes=[],
            failures=failures,
            accepted_count=len(valid_urls),
            requested_count=requested_count,
            returncode=None,
        )
    except OSError as exc:
        failures = validation_failures + [
            _failure(url, "process_error", str(exc)) for url in valid_urls
        ]
        return _build_result(
            run_id=selected_run_id,
            output_dir=output_dir,
            successes=[],
            failures=failures,
            accepted_count=len(valid_urls),
            requested_count=requested_count,
            returncode=None,
        )
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except OSError:
            pass

    successes, output_failures = _parse_outputs(output_dir, valid_urls)
    if not successes and all(item["error_code"] == "missing_result" for item in output_failures):
        code = "process_failed" if returncode else "missing_result"
        output_failures = [
            _failure(item["source_url"], code, "downloader did not produce index.csv results")
            for item in output_failures
        ]
    return _build_result(
        run_id=selected_run_id,
        output_dir=output_dir,
        successes=successes,
        failures=validation_failures + output_failures,
        accepted_count=len(valid_urls),
        requested_count=requested_count,
        returncode=returncode,
    )


# Short alias for future source-pool integration.
fetch_wechat_articles = fetch_known_wechat_articles
