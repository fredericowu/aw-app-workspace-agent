"""Talks to the real Agents Platform REST API and shapes its responses into
what the command-center frontend (frontend/app.js) expects.

Credential resolution mirrors aw-app-call-agent's settings.py steps 1+3 (that
app's own docstring explains the full 4-step chain; steps 2/4 are its
Tier-2-container-only workarounds and do not apply here — this app is
Tier-1 in-process, so a direct file read of the Agents Platform Runners
app's own saved config is reachable with no container boundary to cross):

1. this app's own config (``agents_platform_base`` / ``agents_platform_token``)
   — set these to point at a different platform, or as a break-glass override.
2. the Agents Platform Runners app's saved config snapshot on disk, at
   ``$AW_WORKSPACE_HOME/app-config/agents-platform-runners.json`` — what every
   workspace that runs agents already has, so installing this app needs no
   extra setup.

Both are best-effort, read fresh on every call (never cached across calls) so
a token rotated on the runners app is picked up on the very next request.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("aw_apps.workspace_agent.ap_client")

RUNNERS_APP_ID = "agents-platform-runners"

# Agents whose `use_cases` is empty fall back to this, truncated — about()
# falls back to the same string so the sheet never shows a blank section.
_NO_DESCRIPTION = "No description yet."


def _workspace_home() -> Path:
    return Path(
        os.environ.get("AW_WORKSPACE_HOME")
        or (os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace") + "/.aw-workspace")
    )


def _runners_config() -> dict:
    """Best-effort read of the Agents Platform Runners app's saved config."""
    path = _workspace_home() / "app-config" / f"{RUNNERS_APP_ID}.json"
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — a bad file just leaves config blank
        log.warning("could not read %s: %s", path, exc)
        return {}
    # Core snapshots either the bare config dict or wraps it under "config"
    # depending on version — accept both, same rule aw-app-call-agent uses.
    cfg = raw.get("config") if isinstance(raw.get("config"), dict) else raw
    return cfg if isinstance(cfg, dict) else {}


def resolve_credentials(config: dict | None, secret_token: str | None) -> tuple[str, str, str]:
    """Return ``(base, token, source)`` — ``source`` is for diagnostics only."""
    cfg = config or {}
    base = (cfg.get("agents_platform_base") or "").strip()
    token = (secret_token or "").strip()
    if base and token:
        return base, token, "app-config"

    runners = _runners_config()
    if not base:
        base = (runners.get("agents_platform_base") or "").strip()
    if not token:
        token = (runners.get("agents_platform_token") or "").strip()
    source = "inherited:agents-platform-runners" if (base or token) else "unconfigured"
    return base, token, source


def hex_to_hue(color: str | None, default: int = 40) -> int:
    """Convert a ``#rrggbb`` hex string to a 0-359 HSL hue for the
    frontend's ``--h`` CSS custom property. Each real agent already carries
    its own hex ``color`` — this is a pure presentation-layer conversion of
    that real field, not a fabricated value."""
    if not color or not isinstance(color, str):
        return default
    c = color.strip().lstrip("#")
    if len(c) != 6:
        return default
    try:
        r, g, b = (int(c[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return default
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return default
    d = mx - mn
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return round(h * 60) % 360


class APError(RuntimeError):
    """Raised when the Agents Platform is unreachable or unauthorized."""


class APUnknownOutcome(APError):
    """A write whose result we could not see (timeout, dropped connection).
    The platform may have accepted it, so it must not be blindly re-sent."""


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"


def _check(resp: httpx.Response) -> None:
    """Translate the two failures the UI has to tell apart — a bad token and
    everything else — then let httpx raise for the rest."""
    if resp.status_code == 401:
        raise APError("platform token invalid or expired")
    resp.raise_for_status()


async def _get(client: httpx.AsyncClient, base: str, token: str, path: str, **params) -> Any:
    try:
        resp = await client.get(
            _url(base, path),
            headers=_auth(token),
            params=params,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise APError(f"could not reach the Agents Platform: {exc}") from exc
    _check(resp)
    return resp.json()


#: Public name for the same call — sessions.py is a separate module and
#: should not be reaching into an underscore.
get_json = _get


async def _write(
    client: httpx.AsyncClient, method: str, base: str, token: str, path: str, body: dict
) -> Any:
    try:
        resp = await client.request(
            method,
            _url(base, path),
            headers=_auth(token),
            json=body,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        # A timeout or dropped connection AFTER the request left means the
        # platform may well have created the run: the outcome is unknown.
        raise APUnknownOutcome(f"could not reach the Agents Platform: {exc}") from exc
    if resp.status_code in (400, 404, 409, 429):
        # The platform's own refusals carry a reason the user needs to read
        # verbatim: an unknown agent slug (404), a QA dedup or budget
        # refusal (409/429), a rejected dispatch (400). Collapsing them into
        # a bare 502 "upstream error" throws away the only useful part.
        detail = ""
        try:
            detail = str((resp.json() or {}).get("detail") or "")
        except ValueError:
            detail = resp.text[:400]
        raise APError(detail or f"the platform refused the request ({resp.status_code})")
    _check(resp)
    return resp.json()


async def post_json(client: httpx.AsyncClient, base: str, token: str, path: str, body: dict) -> Any:
    return await _write(client, "POST", base, token, path, body)


async def patch_json(
    client: httpx.AsyncClient, base: str, token: str, path: str, body: dict
) -> Any:
    return await _write(client, "PATCH", base, token, path, body)


async def stream_sse(base: str, token: str, path: str):
    """Byte-for-byte passthrough of an upstream SSE response.

    Three things make this a stream rather than a buffered fetch:

    * ``read=None`` — an agent run can go minutes between events (a cold
      container, a long tool call), and any finite read timeout would kill a
      perfectly healthy turn.
    * ``aiter_bytes`` — yields the frames as the platform wrote them, with
      any transfer encoding already undone. NOT ``aiter_raw``: httpx offers
      ``Accept-Encoding: gzip`` by default, and raw bytes would then be
      compressed bytes forwarded under a ``text/event-stream`` header this
      route sets itself, with no ``Content-Encoding`` to tell the browser.
    * the client is created here and closed in the ``finally``, not with
      ``async with`` around the call — the generator outlives this function's
      first ``yield``, so a context manager scoped to the call would close
      the socket out from under it.

    Nothing clever happens on reconnect, on purpose: the upstream route
    replays every persisted event from the DB on each connect and closes a
    finished run with a ``done`` event (api/runs.py's ``stream_run``), so a
    dropped stream recovers losslessly through ``EventSource``'s native
    auto-reconnect.
    """
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)
    )
    try:
        async with client.stream("GET", _url(base, path), headers=_auth(token)) as resp:
            if resp.status_code != 200:
                await resp.aread()
                reason = (
                    "platform token invalid or expired"
                    if resp.status_code == 401
                    else f"upstream stream returned {resp.status_code}"
                )
                yield _sse_error(reason)
                return
            async for chunk in resp.aiter_bytes():
                yield chunk
    except httpx.HTTPError as exc:
        # The client is already streaming, so an HTTP status is no longer
        # available to it — report the failure as a frame it can render
        # instead of just dropping the connection.
        yield _sse_error(f"stream to the Agents Platform failed: {exc}")
    finally:
        await client.aclose()


def _sse_error(reason: str) -> bytes:
    import json as _json

    return (
        b"event: error\ndata: " + _json.dumps({"kind": "error", "error": reason}).encode() + b"\n\n"
    )


def _agent_squad(agent: dict) -> str:
    return agent.get("group_slug") or "ungrouped"


def _agent_tagline(agent: dict) -> str:
    use_cases = agent.get("use_cases") or []
    if use_cases:
        return str(use_cases[0])[:120]
    return str(agent.get("description") or _NO_DESCRIPTION)[:120]


def _running_slugs(runs: list[dict]) -> set[str]:
    return {r["source_slug"] for r in runs if r.get("status") == "running" and r.get("source_slug")}


def shape_bootstrap(agents: list[dict], groups: list[dict], runs: list[dict]) -> dict:
    """Map real Agents Platform responses onto the frontend's `S.*` shape
    (app.js boot()). `sessions`/`notifications` are filled in by
    routes.build_routes's own `/api/bootstrap` handler, from the real rail
    (sessions.py) and the real System feeds (notifications.py) — shipping a
    fixed roster here instead would be the exact mock data this build is
    required to ship none of."""
    live_agents = [a for a in agents if not a.get("deleted_at")]
    # /api/runs' source_slug can name a workflow (e.g. a kanban-sweep run),
    # not just an agent — restrict to slugs that are actually in the fleet
    # so the hero "Running now" count never disagrees with how many agent
    # cards actually render as running.
    running = _running_slugs(runs) & {a["slug"] for a in live_agents}

    squads = [
        {"key": g["slug"], "label": g["name"], "blurb": g.get("description") or ""} for g in groups
    ]
    known_squads = {g["slug"] for g in groups}
    if any(_agent_squad(a) not in known_squads for a in live_agents):
        squads.append(
            {
                "key": "ungrouped",
                "label": "Ungrouped",
                "blurb": "Agents without a squad assignment yet.",
            }
        )

    shaped_agents = [
        {
            "slug": a["slug"],
            "name": a.get("name") or a["slug"],
            "glyph": _GLYPH.get(a.get("icon") or "", "◆"),
            "hue": hex_to_hue(a.get("color")),
            "model": a.get("model_slug") or "",
            "squad": _agent_squad(a) if _agent_squad(a) in known_squads else "ungrouped",
            "tagline": _agent_tagline(a),
            "about": a.get("description") or _NO_DESCRIPTION,
            "skills": a.get("skill_slugs") or [],
            "status": "running" if a["slug"] in running else "idle",
        }
        for a in live_agents
    ]

    return {
        "agents": shaped_agents,
        "squads": squads,
        "stats": {
            "agents": len(shaped_agents),
            "online": len(shaped_agents),
            "running": len(running),
        },
    }


#: Model providers that run through a coding CLI, and therefore MINT a CLI
#: session the platform records as a ``CliSession``. Everything else answers
#: over a plain API call and leaves no session behind, so a run against it
#: can never be resumed — measured on this workspace: 84 of 92 live agents
#: are ``runner``, the other 8 are openai/echo/fake/anthropic. The Fleet
#: gallery offers all of them, so the Sessions surface has to say which is
#: which rather than letting a tab silently become un-continuable.
RESUMABLE_PROVIDERS = frozenset({"runner", "cli"})


def shape_models(models: list[dict]) -> dict:
    return {
        m["slug"]: {
            "label": m.get("display_name") or m["slug"],
            "provider": m.get("provider") or "",
            "resumable": (m.get("provider") or "") in RESUMABLE_PROVIDERS,
        }
        for m in models
    }


# A tiny, deliberately small icon->glyph map — just enough common icons to
# render something recognizable; anything unmapped falls back to a diamond
# rather than guessing a cute emoji for an icon name we don't know.
_GLYPH = {
    "layers": "◆",
    "sparkles": "✦",
    "calculator": "\U0001f9ee",
    "code": "\U0001f5ae",
    "wrench": "\U0001f527",
    "bug": "\U0001f41b",
    "book": "\U0001f4d6",
    "shield": "\U0001f6e1",
    "chat": "\U0001f4ac",
}


async def fetch_bootstrap(base: str, token: str) -> dict:
    async with httpx.AsyncClient() as client:
        agents, groups, models, runs = (
            await _get(client, base, token, "/api/agents"),
            await _get(client, base, token, "/api/agent-groups"),
            await _get(client, base, token, "/api/models"),
            await _get(
                client, base, token, "/api/runs", status="running", summary="true", limit=500
            ),
        )
    data = shape_bootstrap(agents, groups, runs)
    data["models"] = shape_models(models)
    data["sessions"] = []
    data["notifications"] = []
    data["ticker"] = [{"agent": None, "text": "connected — tracking the live Agents Platform."}]
    data["workspace"] = {
        "name": os.environ.get("AW_WORKSPACE_SLUG", "aw-workspace"),
        "env": "self-hosted",
    }
    return data


async def fetch_pulse(base: str, token: str) -> dict:
    async with httpx.AsyncClient() as client:
        runs = await _get(
            client, base, token, "/api/runs", status="running", summary="true", limit=500
        )
    running = _running_slugs(runs)
    n = len(running)
    text = f"{n} agent run{'s' if n != 1 else ''} active now" if n else "nothing running right now"
    return {
        "event": {"agent": None, "text": text},
        "stats": {"running": n},
    }
