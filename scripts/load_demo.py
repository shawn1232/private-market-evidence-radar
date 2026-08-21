#!/usr/bin/env python3
"""Generate local DealScope caches from synthetic fixtures only.

This script performs no network I/O and never reads raw captures, sessions, or
the WeChat source pool.  Its only write targets are two JSON files under
``data/output``.  Existing non-synthetic reports are protected by default.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = ROOT / "examples"
OUTPUT_DIR = ROOT / "data" / "output"
NOTICE = "SYNTHETIC DEMO DATA - NOT A REAL COMPANY, SOURCE, OR INVESTMENT RECOMMENDATION"
TEMPLATES = {
    EXAMPLES_DIR / "synthetic_weekly_report.json": OUTPUT_DIR / "weekly_radar.json",
    EXAMPLES_DIR / "synthetic_deep_report.json": OUTPUT_DIR / "latest_report.json",
}
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _tokens() -> dict[str, str]:
    now = datetime.now(SHANGHAI_TZ).replace(microsecond=0)
    as_of = now.date()
    return {
        "__GENERATED_AT__": now.isoformat(),
        "__AS_OF__": as_of.isoformat(),
        "__WINDOW_START__": (as_of - timedelta(days=6)).isoformat(),
        "__WINDOW_END__": as_of.isoformat(),
        "__EVENT_DATE_1__": (as_of - timedelta(days=2)).isoformat(),
        "__EVENT_DATE_2__": (as_of - timedelta(days=3)).isoformat(),
    }


def _replace_tokens(value: Any, tokens: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_tokens(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_tokens(item, tokens) for item in value]
    if isinstance(value, str):
        result = value
        for marker, replacement in tokens.items():
            result = result.replace(marker, replacement)
        return result
    return value


def _walk(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _validate_synthetic(payload: Any, source: Path) -> None:
    if not isinstance(payload, dict) or payload.get("synthetic") is not True:
        raise ValueError(f"{source.name} must have top-level synthetic=true")
    if payload.get("demo_notice") != NOTICE:
        raise ValueError(f"{source.name} has an unexpected demo_notice")

    serialized = json.dumps(payload, ensure_ascii=False)
    if "__" in serialized:
        raise ValueError(f"{source.name} still contains an unresolved placeholder")

    for key, value in _walk(payload):
        if key not in {"url", "source_url", "publisher_url"} or not isinstance(value, str) or not value:
            continue
        hostname = (urlsplit(value).hostname or "").lower().rstrip(".")
        if not hostname.endswith(".invalid"):
            raise ValueError(f"Synthetic URL must use a .invalid host: {value}")

    candidates = payload.get("candidates")
    if candidates is None and isinstance(payload.get("report"), dict):
        candidates = payload["report"].get("candidates")
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            raise ValueError(f"{source.name} contains a non-object candidate")
        name = str(candidate.get("company") or candidate.get("entity") or "")
        if "虚构" not in name:
            raise ValueError(f"Synthetic candidate must be visibly marked 虚构: {name!r}")


def _load_fixture(path: Path, tokens: dict[str, str]) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload = _replace_tokens(payload, tokens)
    _validate_synthetic(payload, path)
    return payload


def _existing_is_synthetic(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("synthetic") is True


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_demo(*, force: bool = False) -> list[Path]:
    tokens = _tokens()
    prepared = [(target, _load_fixture(source, tokens)) for source, target in TEMPLATES.items()]

    protected = [target for target, _payload in prepared if not force and not _existing_is_synthetic(target)]
    if protected:
        relative = ", ".join(_display_path(path) for path in protected)
        raise RuntimeError(
            f"Refusing to overwrite non-synthetic output: {relative}. "
            "Move the real report or rerun with --force only in a disposable demo environment."
        )

    written: list[Path] = []
    for target, payload in prepared:
        _atomic_write_json(target, payload)
        written.append(target)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Load DealScope SYNTHETIC DEMO caches into data/output only.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing non-synthetic cache files (still writes synthetic demo data only)",
    )
    args = parser.parse_args()

    try:
        written = load_demo(force=args.force)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Demo load failed: {exc}\n")

    print(NOTICE)
    print("No network calls were made. Generated:")
    for path in written:
        print(f"- {_display_path(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
