#!/usr/bin/env python3
"""Fail CI if a public DealScope checkout contains private-workspace residue."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".txt", ".sh", ".ps1", ".cmd"}
FORBIDDEN_PATH_PARTS = {
    ".claude",
    ".codex",
    "external_skills_raw",
    "legacy_stock_picker",
    "sessions",
}
FORBIDDEN_NAME_PREFIXES = ("_task", "_result")
FORBIDDEN_TEXT = (
    "中证投",
    "中信证券",
    "CITICS",
    "另类投资子公司",
    "自有资金股权投资平台",
    "C:\\Users\\",
    "/Users/",
    "/home/",
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|auth[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"password|pass_ticket|cookie)\b\s*[:=]\s*['\"]"
        r"(?!example|test|fake|placeholder|redacted|not-required)[^'\"]{16,}['\"]"
    ),
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    errors: list[str] = []
    files = tracked_files()
    for path in files:
        relative = path.relative_to(ROOT)
        lowered_parts = {part.casefold() for part in relative.parts}
        if lowered_parts & {item.casefold() for item in FORBIDDEN_PATH_PARTS}:
            errors.append(f"forbidden path: {relative}")
            continue
        if relative.name.casefold().startswith(FORBIDDEN_NAME_PREFIXES):
            errors.append(f"forbidden internal artifact: {relative}")
        if path.suffix.casefold() not in TEXT_SUFFIXES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        is_scanner = relative.as_posix() == "scripts/public_release_check.py"
        if not is_scanner:
            for marker in FORBIDDEN_TEXT:
                if marker.casefold() in text.casefold():
                    errors.append(f"forbidden private marker {marker!r}: {relative}")
        if "tests" not in lowered_parts and not is_scanner:
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    errors.append(f"possible embedded credential: {relative}")

    if errors:
        print("Public release check failed:")
        for error in sorted(set(errors)):
            print(f"- {error}")
        return 1
    print(f"Public release check passed for {len(files)} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
