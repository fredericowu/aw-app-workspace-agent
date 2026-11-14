"""Unit tests for workspace_agent_app/ap_client.py — credential resolution,
the hex-color-to-hue conversion, and the real-data-to-frontend-shape mapping.
No network: fetch_bootstrap/fetch_pulse are covered via test_routes.py's
TestClient tests with httpx mocked at the transport level.

Run: python -m pytest tests/test_ap_client.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_agent_app import ap_client  # noqa: E402

AGENT = {
    "slug": "architect",
    "name": "Architect",
    "description": "Decides how a change should be shaped.",
    "icon": "layers",
    "color": "#2dd4bf",
    "model_slug": "claude-runner-fable",
    "group_slug": "devteam",
    "skill_slugs": ["aw-agent-architect"],
    "use_cases": ["how should we build this"],
}

GROUP = {"slug": "devteam", "name": "Dev Team", "description": "Builds things."}


def test_hex_to_hue_converts_a_real_hex_color():
    # #2dd4bf is a teal — hue should land in the cyan/teal band (~175-180).
    hue = ap_client.hex_to_hue("#2dd4bf")
    assert 170 <= hue <= 185


def test_hex_to_hue_falls_back_on_missing_or_malformed_color():
    assert ap_client.hex_to_hue(None) == 40
    assert ap_client.hex_to_hue("") == 40
    assert ap_client.hex_to_hue("not-a-color") == 40
    assert ap_client.hex_to_hue("#ffffff") == 40  # grey/white has no hue — default, not a crash
    assert ap_client.hex_to_hue("#zzzzzz") == 40  # right length, not actually hex digits


def test_hex_to_hue_covers_the_red_and_blue_dominant_branches():
    # Red-dominant and blue-dominant colors exercise the two branches a
    # single teal/green sample (the other test) never reaches.
    assert ap_client.hex_to_hue("#ff0040") == 345
    assert ap_client.hex_to_hue("#0040ff") == 225


def test_resolve_credentials_prefers_own_config_when_both_fields_set():
    base, token, source = ap_client.resolve_credentials(
        {"agents_platform_base": "http://own"}, "own-token"
    )
    assert (base, token, source) == ("http://own", "own-token", "app-config")


def test_resolve_credentials_falls_back_to_runners_app_file(monkeypatch):
    monkeypatch.setattr(
        ap_client,
        "_runners_config",
        lambda: {
            "agents_platform_base": "http://inherited",
            "agents_platform_token": "inherited-token",
        },
    )
    base, token, source = ap_client.resolve_credentials({}, None)
    assert base == "http://inherited"
    assert token == "inherited-token"
    assert source == "inherited:agents-platform-runners"


def test_resolve_credentials_reports_unconfigured_when_nothing_is_set(monkeypatch):
    monkeypatch.setattr(ap_client, "_runners_config", lambda: {})
    base, token, source = ap_client.resolve_credentials({}, None)
    assert (base, token, source) == ("", "", "unconfigured")


def test_runners_config_reads_either_the_bare_or_wrapped_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    cfg_dir = tmp_path / "app-config"
    cfg_dir.mkdir()
    (cfg_dir / "agents-platform-runners.json").write_text(
        json.dumps({"config": {"agents_platform_base": "http://wrapped"}})
    )
    assert ap_client._runners_config()["agents_platform_base"] == "http://wrapped"


def test_runners_config_missing_file_is_a_silent_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    assert ap_client._runners_config() == {}


def test_runners_config_malformed_json_is_a_silent_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    cfg_dir = tmp_path / "app-config"
    cfg_dir.mkdir()
    (cfg_dir / "agents-platform-runners.json").write_text("not json")
    assert ap_client._runners_config() == {}


def test_agent_tagline_falls_back_to_description_when_no_use_cases():
    agent = {**AGENT, "use_cases": []}
    assert ap_client._agent_tagline(agent) == AGENT["description"]


@pytest.mark.asyncio
async def test_get_raises_ap_error_on_401():
    def handler(request):
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ap_client.APError, match="invalid or expired"):
            await ap_client._get(client, "http://ap", "bad-tok", "/api/agents")


@pytest.mark.asyncio
async def test_get_raises_ap_error_on_other_http_status():
    def handler(request):
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await ap_client._get(client, "http://ap", "tok", "/api/agents")


@pytest.mark.asyncio
async def test_get_raises_ap_error_when_unreachable():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ap_client.APError, match="could not reach"):
            await ap_client._get(client, "http://ap", "tok", "/api/agents")


@pytest.mark.asyncio
async def test_fetch_bootstrap_and_fetch_pulse_hit_the_right_paths_and_shape(monkeypatch):
    seen_paths = []

    def handler(request):
        seen_paths.append(request.url.path)
        if request.url.path == "/api/agents":
            return httpx.Response(200, json=[AGENT])
        if request.url.path == "/api/agent-groups":
            return httpx.Response(200, json=[GROUP])
        if request.url.path == "/api/models":
            return httpx.Response(200, json=[{"slug": "m", "display_name": "M"}])
        if request.url.path == "/api/runs":
            return httpx.Response(200, json=[{"source_slug": "architect", "status": "running"}])
        raise AssertionError(f"unexpected path {request.url.path}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda: real_async_client(transport=httpx.MockTransport(handler))
    )

    data = await ap_client.fetch_bootstrap("http://ap", "tok")
    assert data["stats"]["running"] == 1
    assert data["sessions"] == []
    assert data["notifications"] == []
    assert data["ticker"][0]["agent"] is None

    pulse = await ap_client.fetch_pulse("http://ap", "tok")
    assert pulse["stats"]["running"] == 1
    assert "1 agent run" in pulse["event"]["text"]

    assert set(seen_paths) == {"/api/agents", "/api/agent-groups", "/api/models", "/api/runs"}


def test_shape_bootstrap_maps_real_fields_and_buckets_ungrouped():
    ungrouped_agent = {**AGENT, "slug": "aw-autoskill", "group_slug": None}
    data = ap_client.shape_bootstrap(
        [AGENT, ungrouped_agent],
        [GROUP],
        [{"source_slug": "architect", "status": "running"}],
    )

    assert data["stats"] == {"agents": 2, "online": 2, "running": 1}
    squad_keys = {s["key"] for s in data["squads"]}
    assert squad_keys == {"devteam", "ungrouped"}

    by_slug = {a["slug"]: a for a in data["agents"]}
    assert by_slug["architect"]["squad"] == "devteam"
    assert by_slug["architect"]["status"] == "running"
    assert by_slug["aw-autoskill"]["squad"] == "ungrouped"
    assert by_slug["aw-autoskill"]["status"] == "idle"
    assert by_slug["architect"]["hue"] == ap_client.hex_to_hue("#2dd4bf")


def test_shape_bootstrap_drops_soft_deleted_agents():
    deleted = {**AGENT, "slug": "gone", "deleted_at": "2026-01-01T00:00:00Z"}
    data = ap_client.shape_bootstrap([AGENT, deleted], [GROUP], [])
    assert [a["slug"] for a in data["agents"]] == ["architect"]
    assert data["stats"]["agents"] == 1


def test_shape_bootstrap_running_stat_excludes_non_agent_source_slugs():
    # A workflow run (e.g. a kanban-sweep) carries a source_slug that is not
    # in /api/agents at all — it must not inflate "Running now" past the
    # number of agent cards actually marked running (caught live: hero said
    # 6, only 5 agent cards were is-running, because a kanban-sweep workflow
    # run's source_slug doesn't match any agent).
    data = ap_client.shape_bootstrap(
        [AGENT],
        [GROUP],
        [
            {"source_slug": "architect", "status": "running"},
            {"source_slug": "kanban-sweep", "status": "running"},
        ],
    )
    assert data["stats"]["running"] == 1
    assert sum(1 for a in data["agents"] if a["status"] == "running") == 1


def test_shape_bootstrap_no_ungrouped_bucket_when_every_agent_has_a_real_group():
    data = ap_client.shape_bootstrap([AGENT], [GROUP], [])
    assert [s["key"] for s in data["squads"]] == ["devteam"]


def test_shape_models_keys_by_slug():
    models = ap_client.shape_models(
        [{"slug": "anthropic-sonnet-4-5", "display_name": "Claude Sonnet 4.5"}]
    )
    assert models == {"anthropic-sonnet-4-5": {"label": "Claude Sonnet 4.5"}}
