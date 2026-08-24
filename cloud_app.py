"""Single-domain WSGI entrypoint for the public DealScope portfolio app.

The public deployment runs the same Flask applications and templates as the
local product.  It intentionally starts only in a fail-closed, synthetic,
read-only mode so an anonymous visitor cannot trigger network collection,
modify the shared article pool, or access local login sessions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from werkzeug.middleware.dispatcher import DispatcherMiddleware

from scripts.load_demo import TEMPLATES, _validate_synthetic, load_demo


ROOT = Path(__file__).resolve().parent


def _require_public_readonly_environment() -> None:
    if os.getenv("DEALSCOPE_MODE", "").strip().lower() != "public_readonly":
        raise RuntimeError("cloud_app requires DEALSCOPE_MODE=public_readonly")
    if os.getenv("DEALSCOPE_DISABLE_NETWORK", "").strip() != "1":
        raise RuntimeError("cloud_app requires DEALSCOPE_DISABLE_NETWORK=1")


def _reject_private_runtime_files() -> None:
    forbidden_paths = [
        ROOT / "data" / "input" / "urls.txt",
        ROOT / "data" / "input" / "wechat_urls.txt",
        ROOT / "data" / "wechat_pool" / "discovery_state.json",
        ROOT / "data" / "wechat_pool" / "followup_news_cache.json",
    ]
    forbidden_paths.extend((ROOT / "data" / "wechat_pool").glob("*.sqlite*"))
    forbidden_paths.extend((ROOT / "sessions").glob("*.json"))
    forbidden_paths.extend(path for path in (ROOT / "data" / "raw").glob("**/*") if path.is_file())
    existing = [path for path in forbidden_paths if path.exists()]
    if existing:
        names = ", ".join(str(path.relative_to(ROOT)) for path in existing[:5])
        raise RuntimeError(f"public deployment refused private runtime files: {names}")


def _prepare_synthetic_runtime() -> None:
    _require_public_readonly_environment()
    _reject_private_runtime_files()
    written = load_demo(force=False)
    expected_targets = {target.resolve() for target in TEMPLATES.values()}
    if {path.resolve() for path in written} != expected_targets:
        raise RuntimeError("synthetic demo initialization produced an unexpected file set")
    for path in written:
        payload = json.loads(path.read_text(encoding="utf-8"))
        _validate_synthetic(payload, path)


_prepare_synthetic_runtime()
os.environ.setdefault("DEALSCOPE_DEEP_BASE_URL", "/workbench/")
os.environ.setdefault("DEALSCOPE_RADAR_BASE_URL", "/")

# Import only after the synthetic-only startup gate has passed.
from app.app import app as workbench_app  # noqa: E402
from app.radar_app import app as radar_app  # noqa: E402


application = DispatcherMiddleware(radar_app, {"/workbench": workbench_app})

