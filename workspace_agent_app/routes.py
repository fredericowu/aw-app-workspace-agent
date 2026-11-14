"""workspace_agent_app's mode-agnostic FastAPI sub-app.

``build_routes(resolve_config)`` returns the same sub-app in both modes —
integrated (``plugin.py`` hands it to ``ctx.routes.register(...)``, mounted
at ``/api/apps/workspace-agent`` behind the runtime's ``IdentityGuard``) and
standalone (``__main__.py`` mounts it at the same prefix, no guard).

Every path here is RELATIVE, so both modes serve the identical shape:

    /api/apps/workspace-agent/api/bootstrap
    /api/apps/workspace-agent/api/pulse
    /api/apps/workspace-agent/              (the command-center frontend)

The frontend (frontend/app.js) does relative ``fetch('api/...')`` calls from
whatever page it was served at — this sub-app therefore serves the static
frontend at its OWN root ("/"), not under a nested path, so "api/bootstrap"
resolves to a sibling of that root rather than under some /ui/ subdirectory.
The two ``/api/...`` routes are registered before the static mount so they
take priority over it for those exact paths.

``resolve_config`` is a zero-argument callable returning the current
``(agents_platform_base, agents_platform_token)`` pair — passed in rather
than read once, so a settings change takes effect on the very next request
with no restart (same pattern as aw-app-apple-automation's plugin.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import ap_client

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

ConfigResolver = Callable[[], tuple[str, str]]


def build_routes(resolve_config: ConfigResolver | None = None) -> FastAPI:
    """Mode-agnostic factory — call this fresh for each mode."""
    app = FastAPI(title="workspace-agent")
    resolve = resolve_config or (lambda: ("", ""))

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict:
        base, token = resolve()
        if not base or not token:
            raise HTTPException(status_code=503, detail="platform token not configured")
        try:
            return await ap_client.fetch_bootstrap(base, token)
        except ap_client.APError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/pulse")
    async def pulse() -> dict:
        base, token = resolve()
        if not base or not token:
            raise HTTPException(status_code=503, detail="platform token not configured")
        try:
            return await ap_client.fetch_pulse(base, token)
        except ap_client.APError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app
