"""Single-domain WSGI entrypoint for the public DealScope portfolio app.

The public deployment runs the same Flask applications and templates as the
local product.  It supports a synthetic read-only profile for tests and a
public-live profile that permits only bounded, credential-free RSS refreshes.
Anonymous visitors can never modify the article pool, upload files, run the
deep pipeline, or access local login sessions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from werkzeug.middleware.dispatcher import DispatcherMiddleware

from scripts.load_demo import TEMPLATES, _validate_synthetic, load_demo


ROOT = Path(__file__).resolve().parent


def _cloud_mode() -> str:
    return os.getenv("DEALSCOPE_MODE", "").strip().lower()


def _reject_private_runtime_files(mode: str) -> None:
    forbidden_paths = [
        ROOT / "data" / "input" / "urls.txt",
        ROOT / "data" / "input" / "wechat_urls.txt",
    ]
    forbidden_paths.extend((ROOT / "sessions").glob("*.json"))
    forbidden_paths.extend(path for path in (ROOT / "data" / "raw").glob("**/*") if path.is_file())
    if mode == "public_readonly":
        forbidden_paths.extend(
            [
                ROOT / "data" / "wechat_pool" / "discovery_state.json",
                ROOT / "data" / "wechat_pool" / "followup_news_cache.json",
            ]
        )
        forbidden_paths.extend((ROOT / "data" / "wechat_pool").glob("*.sqlite*"))
    else:
        forbidden_paths.extend(
            [
                ROOT / "data" / "output" / "latest_report.json",
                ROOT / "data" / "output" / "latest_pipeline_attempt.json",
                ROOT / "data" / "output" / "discovery_links.json",
            ]
        )
    existing = [path for path in forbidden_paths if path.exists()]
    if existing:
        names = ", ".join(str(path.relative_to(ROOT)) for path in existing[:5])
        raise RuntimeError(f"public deployment refused private runtime files: {names}")


def _prepare_cloud_runtime() -> str:
    mode = _cloud_mode()
    if mode not in {"public_readonly", "public_live"}:
        raise RuntimeError("cloud_app requires DEALSCOPE_MODE=public_readonly or public_live")
    _reject_private_runtime_files(mode)

    if mode == "public_readonly":
        if os.getenv("DEALSCOPE_DISABLE_NETWORK", "").strip() != "1":
            raise RuntimeError("public_readonly requires DEALSCOPE_DISABLE_NETWORK=1")
        written = load_demo(force=False)
        expected_targets = {target.resolve() for target in TEMPLATES.values()}
        if {path.resolve() for path in written} != expected_targets:
            raise RuntimeError("synthetic demo initialization produced an unexpected file set")
        for path in written:
            payload = json.loads(path.read_text(encoding="utf-8"))
            _validate_synthetic(payload, path)
    else:
        if os.getenv("DEALSCOPE_PUBLIC_RSS_ONLY", "").strip() != "1":
            raise RuntimeError("public_live requires DEALSCOPE_PUBLIC_RSS_ONLY=1")
        if os.getenv("DEALSCOPE_PUBLIC_EXA_MCP", "").strip() != "1":
            raise RuntimeError("public_live requires DEALSCOPE_PUBLIC_EXA_MCP=1")
        os.environ["DEALSCOPE_ALLOW_PUBLIC_WECHAT_FALLBACK"] = "0"
        (ROOT / "data" / "output").mkdir(parents=True, exist_ok=True)
    return mode


MODE = _prepare_cloud_runtime()
os.environ.setdefault("DEALSCOPE_DEEP_BASE_URL", "/workbench/")
os.environ.setdefault("DEALSCOPE_RADAR_BASE_URL", "/")

# Import only after the synthetic-only startup gate has passed.
from app.app import app as workbench_app  # noqa: E402
from app.radar_app import app as radar_app, _warm_cache_in_background  # noqa: E402


application = DispatcherMiddleware(radar_app, {"/workbench": workbench_app})

if MODE == "public_live":
    _warm_cache_in_background()
