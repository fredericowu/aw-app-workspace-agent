"""workspace_agent_app's mode-agnostic FastAPI sub-app.

``build_routes(resolve_config, store)`` returns the same sub-app in both
modes — integrated (``plugin.py`` hands it to ``ctx.routes.register(...)``,
mounted at ``/api/apps/workspace-agent`` behind the runtime's
``IdentityGuard``) and standalone (``__main__.py`` mounts it at the same
prefix, no guard).

Every path here is RELATIVE, so both modes serve the identical shape:

    /api/apps/workspace-agent/api/bootstrap
    /api/apps/workspace-agent/api/sessions
    /api/apps/workspace-agent/api/runs/{run_id}/stream
    /api/apps/workspace-agent/              (the command-center frontend)

The frontend (frontend/app.js) does relative ``fetch('api/...')`` calls from
whatever page it was served at — this sub-app therefore serves the static
frontend at its OWN root ("/"), not under a nested path, so "api/bootstrap"
resolves to a sibling of that root rather than under some /ui/ subdirectory.
The ``/api/...`` routes are registered before the static mount so they take
priority over it for those exact paths.

``resolve_config`` is a zero-argument callable returning the current
``(agents_platform_base, agents_platform_token)`` pair — passed in rather
than read once, so a settings change takes effect on the very next request
with no restart (same pattern as aw-app-apple-automation's plugin.py).

The platform bearer never leaves this process. Every upstream call is made
here, including the SSE passthrough: that token authorises everything on the
platform API, so handing it to an iframe would be handing it to the browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ap_client
from . import sessions as sessions_mod
from .store import Store

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

ConfigResolver = Callable[[], tuple[str, str]]


class NewSession(BaseModel):
    agent: str
    first_message: str


class Message(BaseModel):
    text: str


class Rename(BaseModel):
    name: str


class Toggle(BaseModel):
    value: bool


def build_routes(
    resolve_config: ConfigResolver | None = None, store: Store | None = None
) -> FastAPI:
    """Mode-agnostic factory — call this fresh for each mode."""
    app = FastAPI(title="workspace-agent")
    resolve = resolve_config or (lambda: ("", ""))
    st = store if store is not None else Store(None)

    def creds() -> tuple[str, str]:
        base, token = resolve()
        if not base or not token:
            raise HTTPException(status_code=503, detail="platform token not configured")
        return base, token

    async def guarded(coro):
        """One place to turn an upstream failure into an HTTP status, so
        every route reports a bad token and an unreachable platform the same
        way and the frontend only has one shape to render."""
        try:
            return await coro
        except ap_client.APError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ---- fleet (card 1/3) ---------------------------------------------

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict:
        base, token = creds()
        data = await guarded(ap_client.fetch_bootstrap(base, token))
        # The rail ships with the first paint rather than as a second
        # round-trip: the Sessions view is one keystroke from the landing
        # view, and an empty rail that fills in a moment later reads as
        # "you have no sessions".
        rail = await guarded(sessions_mod.build_rail(base, token, st))
        data["sessions"] = rail["sessions"]
        data["stats"]["sessions_open"] = sum(1 for s in rail["sessions"] if s["state"] == "open")
        return data

    @app.get("/api/pulse")
    async def pulse() -> dict:
        base, token = creds()
        return await guarded(ap_client.fetch_pulse(base, token))

    # ---- sessions rail -------------------------------------------------

    @app.get("/api/sessions")
    async def list_sessions(full: bool = False) -> dict:
        """The rail.

        ``full=true`` is the authoritative build (``/api/sessions`` + one
        runs sweep) — used on first paint and periodically. The default is
        the cheap one-call refresh the ~10s poll uses; build_rail's docstring
        carries the measurement that makes the split worth having.
        """
        base, token = creds()
        return await guarded(sessions_mod.build_rail(base, token, st, full=full))

    @app.get("/api/sessions/{session_id}/thread")
    async def thread(session_id: str) -> dict:
        base, token = creds()
        return await guarded(sessions_mod.build_thread(base, token, st, session_id))

    @app.post("/api/sessions")
    async def start_session(body: NewSession) -> dict:
        """Open a NEW CLI session by dispatching the first message with no
        ``session_id``. Returns ``run_id`` immediately; ``session_id`` is
        null until the CLI mints it (poll ``/api/runs/{run_id}``)."""
        base, token = creds()
        text = body.first_message.strip()
        if not text:
            raise HTTPException(status_code=400, detail="first_message is required")
        return await guarded(sessions_mod.dispatch(base, token, st, body.agent, text))

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: Message) -> dict:
        """Continue an EXISTING session — the same dispatch endpoint, with
        ``session_id`` set, which is exactly how the Telegram agent resumes
        a session."""
        base, token = creds()
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        agent = await guarded(_agent_for(base, token, st, session_id))
        return await guarded(
            sessions_mod.dispatch(base, token, st, agent, text, session_id=session_id)
        )

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str, body: Rename) -> dict:
        base, token = creds()
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        return await guarded(sessions_mod.rename(base, token, session_id, name))

    @app.post("/api/sessions/{session_id}/pin")
    async def pin(session_id: str, body: Toggle) -> dict:
        return {"pinned": st.set_flag(session_id, "pinned", body.value)}

    @app.post("/api/sessions/{session_id}/archive")
    async def archive(session_id: str, body: Toggle) -> dict:
        return {"archived": st.set_flag(session_id, "archived", body.value)}

    # ---- runs ----------------------------------------------------------

    @app.get("/api/runs/{run_id}")
    async def run_status(run_id: str) -> dict:
        base, token = creds()
        return await guarded(sessions_mod.run_status(base, token, st, run_id))

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> list:
        """The tool trace, on demand — not part of the thread payload,
        because a long run's event list dwarfs its transcript."""
        base, token = creds()
        import httpx

        async def go():
            async with httpx.AsyncClient() as client:
                return await ap_client.get_json(client, base, token, f"/api/runs/{run_id}/events")

        data = await guarded(go())
        return data if isinstance(data, list) else []

    @app.get("/api/runs/{run_id}/stream")
    async def run_stream(run_id: str) -> StreamingResponse:
        base, token = creds()
        return StreamingResponse(
            ap_client.stream_sse(base, token, f"/api/runs/{run_id}/stream"),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                # Belt and braces for any proxy that ends up in front of
                # this route — Tier-1 mounts straight into the workspace
                # process, so today there is none, but a buffered SSE stream
                # fails as "the UI thinks forever" rather than as an error.
                "X-Accel-Buffering": "no",
            },
        )

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


async def _agent_for(base: str, token: str, store: Store, session_id: str) -> str:
    """Which agent to resume ``session_id`` with.

    ``POST /api/agents/{slug}/run`` is addressed by agent, but a session row
    carries no agent slug — so it has to come from the session's own runs.
    The cached ``agent_slug`` is the normal path; the fallback asks upstream
    for the session's most recent run, because a session the rail has never
    derived (deep in the list, or created since the last refresh) must still
    be answerable.
    """
    meta = store.meta(session_id) or {}
    if meta.get("agent_slug"):
        return str(meta["agent_slug"])

    import httpx

    async with httpx.AsyncClient() as client:
        runs = await ap_client.get_json(
            client,
            base,
            token,
            "/api/runs",
            session_id=session_id,
            summary="true",
            limit=1,
        )
    runs = runs if isinstance(runs, list) else []
    slug = runs[0].get("source_slug") if runs else None
    if not slug:
        raise ap_client.APError(
            "cannot tell which agent owns this session — it has no runs to read a slug from"
        )
    return str(slug)
