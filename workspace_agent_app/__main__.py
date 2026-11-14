"""Standalone entrypoint — run this app WITHOUT the aw-workspace runtime:

    python -m workspace_agent_app                # binds 127.0.0.1:9401
    AW_AGENTS_PLATFORM_BASE=... AW_AGENTS_PLATFORM_TOKEN=... python -m workspace_agent_app

Mounts the SAME ``build_routes()`` sub-app at the SAME prefix used in
integrated mode (``/api/apps/workspace-agent``), config/token read from the
environment since there is no workspace config store here.

No ``IdentityGuard`` — that is aw-workspace runtime machinery, not app code.
Default posture: bind ``127.0.0.1`` only.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from .routes import build_routes

SLUG = "workspace-agent"  # must match aw-app.json's "id"
DEFAULT_PORT = 9401  # must match aw-app.json's runtime.standalone.default_port


def _resolve_config() -> tuple[str, str]:
    return (
        os.environ.get("AW_AGENTS_PLATFORM_BASE", "").strip(),
        os.environ.get("AW_AGENTS_PLATFORM_TOKEN", "").strip(),
    )


def build_standalone_app() -> FastAPI:
    app = FastAPI(title="workspace-agent (standalone)")
    app.mount(f"/api/apps/{SLUG}", build_routes(_resolve_config))
    return app


app = build_standalone_app()


def main() -> None:
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    host = os.environ.get("AW_APP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
