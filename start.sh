#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

VENV_ROOT="$PROJECT_ROOT/.venv"
PYTHON_BIN="$VENV_ROOT/bin/python"
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements.txt"
REQUIREMENTS_STAMP="$VENV_ROOT/.requirements.sha256"
LOGS_DIR="$PROJECT_ROOT/logs"
TARGET_URL="http://127.0.0.1:8791/"

mkdir -p "$LOGS_DIR"

fail() {
  printf 'DealScope startup failed: %s\n' "$1" >&2
  exit 1
}

if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  fail "requirements.txt is missing."
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3.12 >/dev/null 2>&1; then
    BASE_PYTHON="$(command -v python3.12)"
  elif command -v python3 >/dev/null 2>&1; then
    BASE_PYTHON="$(command -v python3)"
  else
    fail "Python 3.12 was not found."
  fi
  "$BASE_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
    || fail "Python 3.12 or newer is required."
  "$BASE_PYTHON" -m venv "$VENV_ROOT" \
    || fail "could not create the local Python environment."
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
  || fail "the existing .venv does not use Python 3.12 or newer; recreate it with Python 3.12."

requirements_hash="$($PYTHON_BIN -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$REQUIREMENTS_FILE")"
installed_hash=""
if [[ -f "$REQUIREMENTS_STAMP" ]]; then
  installed_hash="$(tr -d '\r\n' < "$REQUIREMENTS_STAMP")"
fi
if [[ "$requirements_hash" != "$installed_hash" ]]; then
  "$PYTHON_BIN" -m pip install --upgrade pip \
    || fail "could not update pip."
  "$PYTHON_BIN" -m pip install -r "$REQUIREMENTS_FILE" \
    || fail "could not install Python dependencies."
  printf '%s\n' "$requirements_hash" > "$REQUIREMENTS_STAMP"
fi

export PLAYWRIGHT_BROWSERS_PATH="$PROJECT_ROOT/.playwright-browsers"
browser_path="$($PYTHON_BIN -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()' 2>/dev/null || true)"
if [[ -z "$browser_path" || ! -x "$browser_path" ]]; then
  "$PYTHON_BIN" -m playwright install chromium \
    || fail "could not install Playwright Chromium."
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export WEEKLY_RADAR_PORT=8791

service_ready() {
  local port="$1"
  local expected="$2"
  "$PYTHON_BIN" - "$port" "$expected" <<'PY'
import json
import sys
import urllib.request

port, expected = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
        payload = json.load(response)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if payload.get("service") == expected else 1)
PY
}

port_in_use() {
  "$PYTHON_BIN" - "$1" <<'PY'
import socket
import sys

with socket.socket() as client:
    client.settimeout(0.4)
    raise SystemExit(0 if client.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

start_service() {
  local name="$1"
  local port="$2"
  local expected="$3"
  local relative_script="$4"
  local script_path="$PROJECT_ROOT/$relative_script"

  if service_ready "$port" "$expected"; then
    return 0
  fi
  if port_in_use "$port"; then
    fail "port $port is already used by another application."
  fi
  if [[ ! -f "$script_path" ]]; then
    fail "service file is missing: $relative_script"
  fi

  local stamp
  stamp="$(date '+%Y%m%d_%H%M%S')"
  local stdout_log="$LOGS_DIR/${name}_${stamp}.out.log"
  local stderr_log="$LOGS_DIR/${name}_${stamp}.err.log"
  nohup "$PYTHON_BIN" "$script_path" >"$stdout_log" 2>"$stderr_log" </dev/null &
  local pid=$!

  for _ in $(seq 1 75); do
    if service_ready "$port" "$expected"; then
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      fail "$name exited during startup. See $stderr_log"
    fi
    sleep 1
  done
  fail "$name did not become ready. See $stderr_log"
}

start_service "dealscope_workbench" 8787 "DealScopeWorkbench" "app/app.py"
start_service "dealscope_radar" 8791 "WeeklyProjectRadar" "app/radar_app.py"

printf 'DealScope is ready at %s\n' "$TARGET_URL"
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$TARGET_URL" >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
  open "$TARGET_URL" >/dev/null 2>&1 &
else
  printf 'Open %s in your browser.\n' "$TARGET_URL"
fi
