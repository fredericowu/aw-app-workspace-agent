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
from workspace_agent_app import notifications as notifications_mod  # noqa: E402
from workspace_agent_app import sessions as sessions_mod  # noqa: E402
from workspace_agent_app.routes import build_routes  # noqa: E402


def _stub_rail(monkeypatch, sessions=()):
    """``/api/bootstrap`` ships the rail with the first paint, so every
    bootstrap test has to stub the rail too or it reaches the network."""

    async def fake_build_rail(base, token, store):
        return {"sessions": list(sessions), "backfilled": 0}

    monkeypatch.setattr(sessions_mod, "build_rail", fake_build_rail)


def _stub_notifications(monkeypatch, notes=()):
    """Same reasoning as ``_stub_rail`` — the System feeds also ship with
    the first paint."""

    async def fake_build_questions(base, token):
        return {"notifications": list(notes)}

    monkeypatch.setattr(notifications_mod, "build_questions", fake_build_questions)


def test_bootstrap_503s_when_no_credentials_are_configured():
    client = TestClient(build_routes(lambda: ("", "")))
    resp = client.get("/api/bootstrap")
    assert resp.status_code == 503


def test_bootstrap_returns_the_shaped_payload_when_configured(monkeypatch):
    async def fake_fetch_bootstrap(base, token):
        assert (base, token) == ("http://ap", "tok")
        return {"agents": [], "squads": [], "models": {}, "stats": {"agents": 0}}

    monkeypatch.setattr(ap_client, "fetch_bootstrap", fake_fetch_bootstrap)
    _stub_rail(monkeypatch)
    _stub_notifications(monkeypatch)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    resp = client.get("/api/bootstrap")
    assert resp.status_code == 200
    assert resp.json()["stats"]["agents"] == 0


def test_bootstrap_ships_the_rail_with_the_first_paint(monkeypatch):
    """The Sessions view is one keystroke from the landing view, so the rail
    must not arrive as a second round-trip — an empty rail that fills in a
    moment later reads as "you have no sessions"."""

    async def fake_fetch_bootstrap(base, token):
        return {"agents": [], "squads": [], "models": {}, "stats": {"agents": 0}}

    monkeypatch.setattr(ap_client, "fetch_bootstrap", fake_fetch_bootstrap)
    _stub_rail(
        monkeypatch,
        [
            {"id": "s1", "state": "open"},
            {"id": "s2", "state": "archived"},
        ],
    )
    _stub_notifications(monkeypatch)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    body = client.get("/api/bootstrap").json()
    assert [s["id"] for s in body["sessions"]] == ["s1", "s2"]
    assert body["stats"]["sessions_open"] == 1


def test_bootstrap_ships_the_system_badge_count_with_the_first_paint(monkeypatch):
    """Same reasoning as the rail: the needs-you badge must be real on first
    paint, not fill in a few seconds later."""

    async def fake_fetch_bootstrap(base, token):
        return {"agents": [], "squads": [], "models": {}, "stats": {"agents": 0}}

    monkeypatch.setattr(ap_client, "fetch_bootstrap", fake_fetch_bootstrap)
    _stub_rail(monkeypatch)
    _stub_notifications(
        monkeypatch,
        [
            {"id": "hq:t1", "at": 3, "state": "open", "severity": "action"},
            {"id": "hq:t3", "at": 2, "state": "open", "severity": "critical"},
            {"id": "hq:t2", "at": 1, "state": "resolved", "severity": "action"},
        ],
    )
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    body = client.get("/api/bootstrap").json()
    assert len(body["notifications"]) == 3
    assert body["stats"]["needs_you"] == 2


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


# ---- sessions surface ------------------------------------------------------


def test_every_session_route_503s_when_unconfigured():
    """A missing platform token has to read the same on every route, or the
    UI has to guess whether an empty rail means "none" or "no credentials"."""
    client = TestClient(build_routes(lambda: ("", "")))
    assert client.get("/api/sessions").status_code == 503
    assert client.get("/api/sessions/abc/thread").status_code == 503
    assert (
        client.post("/api/sessions", json={"agent": "a", "first_message": "x"}).status_code == 503
    )
    assert client.post("/api/sessions/abc/messages", json={"text": "x"}).status_code == 503
    assert client.patch("/api/sessions/abc", json={"name": "n"}).status_code == 503
    assert client.get("/api/runs/r1").status_code == 503
    assert client.get("/api/runs/r1/events").status_code == 503
    assert client.get("/api/runs/r1/stream").status_code == 503


def test_dispatch_rejects_an_empty_message():
    """Dispatching empty text would spend a real run on nothing."""
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    assert (
        client.post("/api/sessions", json={"agent": "a", "first_message": "   "}).status_code == 400
    )
    assert client.post("/api/sessions/s1/messages", json={"text": " "}).status_code == 400
    assert client.patch("/api/sessions/s1", json={"name": " "}).status_code == 400


def test_start_session_returns_the_run_id_with_a_null_session_id(monkeypatch):
    """The 2s in-flight criterion depends on this: the dispatch returns a
    run_id immediately and does NOT wait for the CLI to mint a session id."""

    async def fake_dispatch(base, token, store, agent, text, session_id=None):
        assert (agent, text, session_id) == ("architect", "go", None)
        return {"run_id": "r9", "session_id": None, "status": "pending"}

    monkeypatch.setattr(sessions_mod, "dispatch", fake_dispatch)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    body = client.post("/api/sessions", json={"agent": "architect", "first_message": "go"}).json()
    assert body == {"run_id": "r9", "session_id": None, "status": "pending"}


def test_continuing_a_session_resolves_its_agent_and_passes_session_id(monkeypatch):
    """``POST /api/agents/{slug}/run`` is addressed by agent but a session row
    carries no slug, so a continuation has to resolve one before it can
    dispatch — and must pass session_id, or it forks a new session."""
    seen = {}

    async def fake_agent_for(base, token, store, session_id):
        return "coder-opus"

    async def fake_dispatch(base, token, store, agent, text, session_id=None):
        seen.update(agent=agent, text=text, session_id=session_id)
        return {"run_id": "r10", "session_id": session_id, "status": "pending"}

    monkeypatch.setattr("workspace_agent_app.routes._agent_for", fake_agent_for)
    monkeypatch.setattr(sessions_mod, "dispatch", fake_dispatch)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    resp = client.post("/api/sessions/sess-1/messages", json={"text": "next"})
    assert resp.status_code == 200
    assert seen == {"agent": "coder-opus", "text": "next", "session_id": "sess-1"}


def test_pin_and_archive_are_app_local_and_need_no_platform_call():
    """There is no upstream archive route, so these must work (and persist)
    without touching the platform at all — including when it is unreachable."""
    client = TestClient(build_routes(lambda: ("", "")))
    assert client.post("/api/sessions/s1/pin", json={"value": True}).json() == {"pinned": True}
    assert client.post("/api/sessions/s1/archive", json={"value": False}).json() == {
        "archived": False
    }


def test_thread_and_run_status_pass_through(monkeypatch):
    async def fake_thread(base, token, store, session_id):
        assert session_id == "sess-1"
        return {"session": {"id": "sess-1", "messages": []}}

    async def fake_run_status(base, token, store, run_id):
        assert run_id == "r1"
        return {"run_id": "r1", "done": True, "cost_usd": 0.5}

    monkeypatch.setattr(sessions_mod, "build_thread", fake_thread)
    monkeypatch.setattr(sessions_mod, "run_status", fake_run_status)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    assert client.get("/api/sessions/sess-1/thread").json()["session"]["id"] == "sess-1"
    assert client.get("/api/runs/r1").json()["cost_usd"] == 0.5


def test_rename_passes_through_and_502s_on_a_platform_error(monkeypatch):
    async def fake_rename(base, token, session_id, name):
        if name == "boom":
            raise ap_client.APError("Session not found")
        return {"title": name}

    monkeypatch.setattr(sessions_mod, "rename", fake_rename)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    assert client.patch("/api/sessions/s1", json={"name": "Rollout"}).json() == {"title": "Rollout"}
    bad = client.patch("/api/sessions/s1", json={"name": "boom"})
    assert bad.status_code == 502 and "not found" in bad.json()["detail"]


def test_run_events_returns_a_list_even_when_upstream_does_not(monkeypatch):
    """The frontend filters this array directly, so a non-list would throw in
    the browser rather than failing here."""

    async def fake_get(client, base, token, path, **params):
        return (
            {"detail": "weird"}
            if path.endswith("/r-odd/events")
            else [
                {"kind": "tool_call", "payload": {"name": "Bash"}},
            ]
        )

    monkeypatch.setattr(ap_client, "get_json", fake_get)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    assert client.get("/api/runs/r1/events").json()[0]["kind"] == "tool_call"
    assert client.get("/api/runs/r-odd/events").json() == []


def test_sessions_rail_surfaces_a_platform_error_as_502(monkeypatch):
    async def raising(base, token, store, full=True):
        raise ap_client.APError("platform token invalid or expired")

    monkeypatch.setattr(sessions_mod, "build_rail", raising)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    resp = client.get("/api/sessions")
    assert resp.status_code == 502 and "invalid" in resp.json()["detail"]


def test_the_rail_route_defaults_to_cheap_and_opts_in_to_full(monkeypatch):
    """The ~10s poll hits the default, so it must NOT be the expensive build;
    first paint and the ~60s refresh pass full=1 explicitly."""
    seen = []

    async def fake_build_rail(base, token, store, full=True):
        seen.append(full)
        return {"sessions": [], "backfilled": 0, "full": full}

    monkeypatch.setattr(sessions_mod, "build_rail", fake_build_rail)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    client.get("/api/sessions")
    client.get("/api/sessions?full=1")
    assert seen == [False, True]


def test_stream_is_served_as_sse_not_json(monkeypatch):
    """A buffered or JSON-typed response would leave EventSource waiting
    forever, which the UI renders as the agent still thinking."""

    async def fake_stream(base, token, path):
        assert path == "/api/runs/r1/stream"
        yield b'event: llm_token\ndata: {"payload": {"delta": "hi"}}\n\n'
        yield b'event: done\ndata: {"done": true}\n\n'

    monkeypatch.setattr(ap_client, "stream_sse", fake_stream)
    client = TestClient(build_routes(lambda: ("http://ap", "tok")))
    with client.stream("GET", "/api/runs/r1/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-accel-buffering"] == "no"
        body = b"".join(resp.iter_bytes())
    assert b'"delta": "hi"' in body
    assert b"event: done" in body


def test_frontend_is_served_at_the_subapp_root():
    client = TestClient(build_routes())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Command Center" in resp.text
