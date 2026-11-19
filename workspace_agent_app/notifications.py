"""The System surface: two notice feeds, merged into the one shape
frontend/app.js's ``S.notifs`` already renders (``noteHtml``/``sysCounts``).

Feed 1 — agent questions. ``GET /api/human-questions`` is the route
card feature:ap-mt-human-questions-list-route added specifically to unblock
this surface (the pending-questions query already existed twice internally;
only the HTTP route was missing). Answering one calls that same route's
``POST .../answer`` alias, which — server-side, in agents-platform-multitenant
— marks the question answered and resumes the asking agent's session with the
answer as its next prompt. This module only proxies both calls; it cannot
observe the resume happen, only that the platform accepted the answer.

Feed 2 — flow escalations. Runs with ``flow_needs_human`` true, taken from
the rail's OWN sweep (``sessions.flagged_runs``, carried on every rail
response) — no second ``/api/runs`` call, in bootstrap or in the polls.

Both feeds map onto one notification:
``{id, kind, from_agent, title, body, severity, state, at,
   context: {run, session, card}, replies, suggestions}``
so the System tab (carried over from the UX-Proto prototype almost verbatim)
needs no second rendering path. ``id`` is prefixed (``hq:``/``esc:``) so the
reply route can tell a question from an alert without a second lookup.
"""

from __future__ import annotations

import logging

import httpx

from . import ap_client
from . import sessions as sessions_mod

log = logging.getLogger("aw_apps.workspace_agent.notifications")

#: GET /api/human-questions newest-first; "all" (not the default "pending")
#: so an answered question stays visible under the System tab's Resolved
#: filter instead of vanishing the moment it is no longer pending.
QUESTION_LIMIT = 100

#: Static quick-fills for the reply box — PO-approved copy, not derived
#: data, so it is not the mock data acceptance criterion 7 forbids.
QUESTION_SUGGESTIONS = ("Yes, proceed.", "No, stop here.", "Go with the safer option.")


def _preview(text: str, limit: int = 110) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed[:limit] if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


async def fetch_human_questions(
    client: httpx.AsyncClient, base: str, token: str, status: str = "all"
) -> list[dict]:
    rows = await ap_client.get_json(
        client, base, token, "/api/human-questions", status=status, limit=QUESTION_LIMIT
    )
    return rows if isinstance(rows, list) else []


async def answer_question(base: str, token: str, question_token: str, text: str) -> dict:
    """Proxy to ``POST /api/human-questions/{token}/answer``. The platform's
    own body field is ``answer``, not ``text`` — this is the one seam where
    this app's and AP-MT's field names differ, so the rename happens here
    rather than leaking AP-MT's schema into the frontend."""
    async with httpx.AsyncClient() as client:
        return await ap_client.post_json(
            client, base, token, f"/api/human-questions/{question_token}/answer", {"answer": text}
        )


def shape_question(q: dict) -> dict:
    status = q.get("status") or "pending"
    state = "resolved" if status != "pending" else "open"
    agent = q.get("agent_slug") or "unknown"
    answer = q.get("answer")
    replies = (
        [{"role": "user", "text": answer, "at": sessions_mod.epoch(q.get("created_at"))}]
        if answer
        else []
    )
    return {
        "id": f"hq:{q.get('token')}",
        "kind": "question",
        "from_agent": agent,
        "title": f"{agent} needs an answer",
        "body": q.get("question") or "",
        "severity": "action",
        "state": state,
        "at": sessions_mod.epoch(q.get("created_at")),
        "context": {
            "run": q.get("run_id"),
            "session": q.get("session_id"),
            "card": q.get("notion_task_id"),
        },
        "replies": replies,
        "suggestions": list(QUESTION_SUGGESTIONS) if state == "open" else [],
    }


def shape_escalation(run: dict) -> dict:
    agent = run.get("source_slug") or "unknown"
    body = _preview(sessions_mod.run_prompt(run)) or "A flow run needs a human decision."
    return {
        "id": f"esc:{run.get('id')}",
        "kind": "alert",
        "from_agent": agent,
        "title": "Flow needs a human",
        "body": body,
        "severity": "critical",
        "state": "open",
        "at": sessions_mod.epoch(run.get("started_at")),
        "context": {
            "run": run.get("id"),
            "session": run.get("session_id"),
            "card": run.get("notion_task_id"),
        },
        "replies": [],
        "suggestions": [],
    }


async def build_questions(base: str, token: str) -> dict:
    """Feed 1 only: one upstream call. Feed 2 (escalations) is NOT fetched
    here — it rides on the rail's own runs sweep (``sessions.flagged_runs``),
    so the System tab adds no second ``/api/runs`` call anywhere."""
    async with httpx.AsyncClient() as client:
        questions = await fetch_human_questions(client, base, token)
    notes = [shape_question(q) for q in questions]
    notes.sort(key=lambda n: n["at"], reverse=True)
    return {"notifications": notes}


def escalations(flagged: list[dict]) -> list[dict]:
    """Shape the rail's flagged runs, newest first."""
    notes = [shape_escalation(r) for r in flagged]
    notes.sort(key=lambda n: n["at"], reverse=True)
    return notes
