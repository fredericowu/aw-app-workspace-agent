"""TestClient coverage for workspace_agent_app/routes.py's build_routes().

ap_client.fetch_bootstrap/fetch_pulse are monkeypatched here — their own
real-field-mapping logic is covered by test_ap_client.py; this file only
proves the HTTP surface (503 when unconfigured, 200/502 passthrough, and
that the frontend is served at the sub-app's own root).

Run: python -m pytest tests/test_routes.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_agent_app import ap_client  # noqa: E402
from workspace_agent_app.routes import build_routes  # noqa: E402


def test_bootstrap_503s_when_no_credentials_are_configured():
    client = TestClient(build_routes(lambda: ("", "")))
    resp = client.get("/api/bootstrap")
    assert resp.status_code == 503


def test_bootstrap_returns_the_shaped_payload_when_configured(monkeypatch):
    async def fake_fetch_bootstrap(base, token):
        assert (base, token) == ("http://ap", "tok")
        return {"agents": [], "squads": [], "models": {}, "stats": {"agents": 0}}

    monkeypatch.setattr(ap_client, "fetch_bootstrap", fake_fetch_bootstrap)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    resp = client.get("/api/bootstrap")
    assert resp.status_code == 200
    assert resp.json()["stats"] == {"agents": 0}


def test_bootstrap_surfaces_a_platform_error_as_502(monkeypatch):
    async def raising(base, token):
        raise ap_client.APError("platform token invalid or expired")

    monkeypatch.setattr(ap_client, "fetch_bootstrap", raising)
    client = TestClient(build_routes(lambda: ("http://ap", "bad-tok")))
    resp = client.get("/api/bootstrap")
    assert resp.status_code == 502
    assert "invalid" in resp.json()["detail"]


def test_pulse_503s_when_no_credentials_are_configured():
    client = TestClient(build_routes(lambda: ("", "")))
    resp = client.get("/api/pulse")
    assert resp.status_code == 503


def test_pulse_surfaces_a_platform_error_as_502(monkeypatch):
    async def raising(base, token):
        raise ap_client.APError("could not reach the Agents Platform: boom")

    monkeypatch.setattr(ap_client, "fetch_pulse", raising)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    resp = client.get("/api/pulse")
    assert resp.status_code == 502


def test_pulse_returns_the_shaped_payload_when_configured(monkeypatch):
    async def fake_fetch_pulse(base, token):
        return {"event": {"agent": None, "text": "1 agent run active now"}, "stats": {"running": 1}}

    monkeypatch.setattr(ap_client, "fetch_pulse", fake_fetch_pulse)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    resp = client.get("/api/pulse")
    assert resp.status_code == 200
    assert resp.json()["stats"]["running"] == 1


def test_default_resolver_is_empty_when_none_given():
    client = TestClient(build_routes())
    assert client.get("/api/bootstrap").status_code == 503


def test_frontend_is_served_at_the_subapp_root():
    client = TestClient(build_routes())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Command Center" in resp.text
