"""One-time migration of existing local WeChat research archives into the pool.

Only public article URLs and article content already saved under the explicitly
provided source directory are read.  Bodies are copied into the project pool;
cookies, search tokens and raw login/session data are never imported.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from weekly_radar import _extract_wechat_published_at
from wechat_source_pool import WeChatSourcePool, canonicalize_wechat_url


DATE_SUFFIX_RE = re.compile(r"\s*[-–—]\s*(20\d{2}-\d{2}-\d{2})\s*$")


def _inside(path_value: Any, root: Path, *, suffixes: Iterable[str]) -> Path | None:
    if not path_value:
        return None
    try:
        path = Path(str(path_value)).expanduser().resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    return path if path.is_file() and path.suffix.lower() in set(suffixes) else None


def _first_valid_url(payload: dict[str, Any]) -> str:
    for key in ("canonical_url", "source_url", "resolved_url", "article_url", "url"):
        value = payload.get(key)
        if not value:
            continue
        try:
            return canonicalize_wechat_url(value)
        except ValueError:
            continue
    return ""


def _atomic_text(path: Path, text: str) -> None:
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


def _saved_meta_records(source_root: Path, body_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for meta_path in sorted(source_root.rglob("meta.json")):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            url = _first_valid_url(payload)
            if not url:
                continue
            title = str(payload.get("title") or "").strip()
            account = str(
                payload.get("account_name")
                or payload.get("account")
                or payload.get("publisher")
                or payload.get("author")
                or "本地已保存公众号"
            ).strip()
            publish_time: Any = (
                payload.get("publish_time")
                or payload.get("published_at")
                or payload.get("publish_date")
            )
            raw_path = _inside(
                payload.get("raw_html_path") or (meta_path.parent / "article_raw.html"),
                source_root,
                suffixes={".html", ".htm"},
            )
            if not publish_time and raw_path and raw_path.stat().st_size <= 4 * 1024 * 1024:
                raw_html = raw_path.read_text(encoding="utf-8", errors="replace")
                publish_time = _extract_wechat_published_at(raw_html)
            text_path = _inside(
                payload.get("text_path")
                or payload.get("saved_file")
                or (meta_path.parent / "article_text.txt"),
                source_root,
                suffixes={".txt", ".md"},
            )
            copied_body = ""
            if text_path and text_path.stat().st_size <= 2 * 1024 * 1024:
                body = text_path.read_text(encoding="utf-8", errors="replace").strip()
                if body:
                    target = body_root / f"{sha256(url.encode('utf-8')).hexdigest()}.md"
                    _atomic_text(target, f"# {title or '公众号文章'}\n\n公众号：{account}\n\n{body}\n")
                    copied_body = str(target.resolve())
            records.append(
                {
                    "account_name": account,
                    "title": title,
                    "url": url,
                    "publish_time": publish_time,
                    "author": str(payload.get("author") or "").strip(),
                    "body_markdown_path": copied_body,
                    "fetch_mode": "body_export" if copied_body else "metadata_only",
                    "credential_status": "not_stored",
                }
            )
        except Exception as exc:
            errors.append(f"{meta_path.name}: {type(exc).__name__}")
    return records, errors


def _history_link_records(source_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = source_root / "history_extracted" / "footer_all_links.json"
    if not path.is_file():
        return [], []
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], [f"footer_all_links.json: {type(exc).__name__}"]
    for index, item in enumerate(payload if isinstance(payload, list) else [], start=1):
        if not isinstance(item, dict):
            continue
        try:
            url = canonicalize_wechat_url(item.get("url"))
            raw_title = str(item.get("title") or "").strip()
            match = DATE_SUFFIX_RE.search(raw_title)
            records.append(
                {
                    "account_name": str(item.get("account_name") or "历史链接导入"),
                    "title": DATE_SUFFIX_RE.sub("", raw_title).strip(),
                    "url": url,
                    "publish_time": match.group(1) if match else "",
                    "fetch_mode": "metadata_only",
                    "credential_status": "not_stored",
                }
            )
        except Exception as exc:
            errors.append(f"footer row {index}: {type(exc).__name__}")
    return records, errors


def migrate_archive(source_root: str | Path, pool: WeChatSourcePool | None = None) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("公众号历史资料目录不存在")
    target_pool = pool or WeChatSourcePool()
    body_root = target_pool.db_path.parent / "bodies" / "legacy"
    saved, saved_errors = _saved_meta_records(root, body_root)
    history, history_errors = _history_link_records(root)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in [*saved, *history]:
        groups[str(record.get("account_name") or "待归属公众号")].append(record)

    added = existing = invalid = 0
    for account_name, records in groups.items():
        result = target_pool.add_urls(records, account_name=account_name)
        added += int(result.get("added") or 0)
        existing += int(result.get("exists") or 0)
        invalid += int(result.get("errors") or 0)
    stats = target_pool.get_stats()
    return {
        "ok": True,
        "source_root": str(root),
        "saved_body_records_seen": len(saved),
        "history_link_records_seen": len(history),
        "added": added,
        "existing": existing,
        "invalid": invalid,
        "scan_errors": [*saved_errors, *history_errors][:50],
        "stats": stats,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="迁移本机已有公众号资料到项目雷达文章库")
    parser.add_argument("source_root")
    parser.add_argument("--db", default="")
    args = parser.parse_args()
    result = migrate_archive(args.source_root, WeChatSourcePool(args.db) if args.db else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
