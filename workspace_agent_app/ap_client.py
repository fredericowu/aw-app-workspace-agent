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


async def _get(client: httpx.AsyncClient, base: str, token: str, path: str, **params) -> Any:
    try:
        resp = await client.get(
            f"{base.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise APError(f"could not reach the Agents Platform: {exc}") from exc
    if resp.status_code == 401:
        raise APError("platform token invalid or expired")
    resp.raise_for_status()
    return resp.json()


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
    (app.js boot()). Sessions/notifications stay empty — those surfaces are
    separate cards, not yet wired; shipping a fixed roster there would be the
    exact mock data this build is required to ship none of."""
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


def shape_models(models: list[dict]) -> dict:
    return {m["slug"]: {"label": m.get("display_name") or m["slug"]} for m in models}


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
