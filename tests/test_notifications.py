"""Coverage for workspace_agent_app/notifications.py and the System routes.

What matters most: the two feeds map onto one shape; an answer goes to the
platform with ``answer`` (not ``text``); an escalation has no reply action;
and the escalation feed reads the same runs sweep the rail uses.

Run: python -m pytest tests/test_notifications.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_agent_app import ap_client  # noqa: E402
from workspace_agent_app import notifications as notes  # noqa: E402
from workspace_agent_app import routes as routes_mod  # noqa: E402


def question(**extra):
    return {
        "id": "q1",
        "token": "tok1",
        "run_id": "r1",
        "session_id": "s1",
        "agent_slug": "architect",
        "question": "Ship it?",
        "answer": None,
        "status": "pending",
        "created_at": "2026-11-14T16:00:00+00:00",
        "notion_task_id": "card1",
        **extra,
    }


def run_row(run_id="r9", flagged=True, **extra):
    return {
        "id": run_id,
        "session_id": "s9",
        "source_slug": "coder",
        "flow_needs_human": flagged,
        "started_at": "2026-11-14T17:00:00+00:00",
        "input": {"input": "  review\nthis   change "},
        "notion_task_id": None,
        **extra,
    }


class _PatchedClient(httpx.AsyncClient):
    handler = None

    def __init__(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(type(self).handler)
        super().__init__(*a, **kw)


@pytest.fixture
def intercept(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    def install(routes: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path, request.url.query.decode()))
            body = routes.get(request.url.path)
            if body is None:
                return httpx.Response(404, json={"detail": "no fake"})
            return httpx.Response(200, json=body(request) if callable(body) else body)

        _PatchedClient.handler = staticmethod(handler)
        monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)
        return calls

    return install


def test_preview_collapses_and_truncates():
    assert notes._preview("a\n  b") == "a b"
    assert notes._preview("x" * 200, 10) == "x" * 9 + "…"


def test_shape_question_open_has_suggestions_and_session():
    n = notes.shape_question(question())
    assert n["id"] == "hq:tok1" and n["kind"] == "question" and n["state"] == "open"
    assert n["context"] == {"run": "r1", "session": "s1", "card": "card1"}
    assert n["from_agent"] == "architect" and n["body"] == "Ship it?"
    assert n["suggestions"] and n["replies"] == []


def test_shape_question_answered_is_resolved_with_the_answer_as_a_user_reply():
    n = notes.shape_question(question(status="answered", answer="yes"))
    assert n["state"] == "resolved" and n["suggestions"] == []
    assert n["replies"][0]["role"] == "user" and n["replies"][0]["text"] == "yes"


def test_shape_question_tolerates_missing_fields():
    n = notes.shape_question({"token": "t"})
    assert n["state"] == "open" and n["from_agent"] == "unknown" and n["body"] == ""


def test_shape_escalation_is_a_critical_alert_without_reply_affordances():
    n = notes.shape_escalation(run_row())
    assert n["id"] == "esc:r9" and n["kind"] == "alert" and n["severity"] == "critical"
    assert n["body"] == "review this change"
    assert n["context"]["session"] == "s9"
    assert n["suggestions"] == [] and n["replies"] == []


def test_shape_escalation_falls_back_when_there_is_no_prompt():
    n = notes.shape_escalation({"id": "r", "input": {}})
    assert n["body"] == "A flow run needs a human decision." and n["from_agent"] == "unknown"


def test_build_questions_is_one_call_and_never_touches_the_runs_sweep(intercept):
    calls = intercept({"/api/human-questions": [question(), question(token="t2")]})
    out = asyncio.run(notes.build_questions("http://ap", "tok"))
    assert sorted(n["id"] for n in out["notifications"]) == ["hq:t2", "hq:tok1"]
    assert [c[1] for c in calls] == ["/api/human-questions"]
    assert "status=all" in calls[0][2]


def test_build_questions_tolerates_a_non_list_upstream(intercept):
    intercept({"/api/human-questions": {"detail": "x"}})
    assert asyncio.run(notes.build_questions("http://ap", "tok")) == {"notifications": []}


def test_escalations_are_shaped_newest_first():
    out = notes.escalations(
        [run_row("old", started_at="2026-11-14T10:00:00+00:00"), run_row("new")]
    )
    assert [n["id"] for n in out] == ["esc:new", "esc:old"]


def test_flagged_runs_filters_the_sweep():
    from workspace_agent_app import sessions

    assert [r["id"] for r in sessions.flagged_runs([run_row("a"), run_row("b", flagged=False)])] == ["a"]


def _runs_sweep_calls(calls):
    return [c for c in calls if c[1] == "/api/runs" and "limit=1000" in c[2]]


def _bootstrap_fakes(intercept):
    return intercept(
        {
            "/api/agents": [],
            "/api/agent-groups": [],
            "/api/models": [],
            "/api/sessions": [],
            "/api/human-questions": [question()],
            "/api/runs": [run_row("r9"), run_row("r8", flagged=False)],
        }
    )


def test_bootstrap_makes_exactly_one_runs_sweep_and_still_lists_both_feeds(intercept):
    """The escalations feed must ride the rail's sweep, not issue its own."""
    calls = _bootstrap_fakes(intercept)
    body = client().get("/api/bootstrap").json()
    assert len(_runs_sweep_calls(calls)) == 1
    assert sorted(n["id"] for n in body["notifications"]) == ["esc:r9", "hq:tok1"]
    assert body["stats"]["needs_you"] == 2


def test_rail_poll_carries_escalations_without_a_second_sweep(intercept):
    calls = _bootstrap_fakes(intercept)
    for url in ("/api/sessions", "/api/sessions?full=1"):
        calls.clear()
        body = client().get(url).json()
        assert [n["id"] for n in body["escalations"]] == ["esc:r9"]
        assert "flagged_runs" not in body
        assert len(_runs_sweep_calls(calls)) == 1


def test_notifications_poll_makes_no_runs_call_at_all(intercept):
    calls = _bootstrap_fakes(intercept)
    client().get("/api/notifications")
    assert [c[1] for c in calls] == ["/api/human-questions"]


def test_answer_question_posts_answer_field_not_text(intercept):
    seen = {}

    def ok(request):
        seen["body"] = request.read()
        return {"ok": True}

    intercept({"/api/human-questions/tok1/answer": ok})
    out = asyncio.run(notes.answer_question("http://ap", "tok", "tok1", "yes"))
    assert out == {"ok": True}
    assert b'"answer"' in seen["body"] and b'"yes"' in seen["body"]


# ---- routes ---------------------------------------------------------------


def client(monkeypatch=None):
    return TestClient(routes_mod.build_routes(lambda: ("http://ap", "tok")))


def test_system_routes_503_when_unconfigured():
    c = TestClient(routes_mod.build_routes(lambda: ("", "")))
    assert c.get("/api/notifications").status_code == 503
    assert c.post("/api/notifications/hq:t/reply", json={"text": "x"}).status_code == 503


def test_notifications_route_passes_through_and_502s(monkeypatch):
    async def ok(base, token):
        return {"notifications": [{"id": "hq:t"}]}

    monkeypatch.setattr(notes, "build_questions", ok)
    assert client().get("/api/notifications").json() == {"notifications": [{"id": "hq:t"}]}

    async def boom(base, token):
        raise ap_client.APError("down")

    monkeypatch.setattr(notes, "build_questions", boom)
    assert client().get("/api/notifications").status_code == 502


def test_reply_proxies_to_the_question_token(monkeypatch):
    seen = {}

    async def fake(base, token, qtoken, text):
        seen.update(qtoken=qtoken, text=text)
        return {"ok": True}

    monkeypatch.setattr(notes, "answer_question", fake)
    r = client().post("/api/notifications/hq:tok1/reply", json={"text": "  go  "})
    assert r.status_code == 200 and seen == {"qtoken": "tok1", "text": "go"}


def test_reply_refuses_an_escalation_and_a_blank_answer(monkeypatch):
    c = client()
    assert c.post("/api/notifications/esc:r9/reply", json={"text": "x"}).status_code == 400
    assert c.post("/api/notifications/hq:/reply", json={"text": "x"}).status_code == 400
    assert c.post("/api/notifications/hq:t/reply", json={"text": "  "}).status_code == 400


def test_reply_surfaces_a_platform_refusal_as_502(monkeypatch):
    async def refuse(base, token, qtoken, text):
        raise ap_client.APError("question already answered")

    monkeypatch.setattr(notes, "answer_question", refuse)
    r = client().post("/api/notifications/hq:t/reply", json={"text": "x"})
    assert r.status_code == 502 and "already answered" in r.json()["detail"]
