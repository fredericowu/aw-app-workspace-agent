"""The durable per-session message queue (card 4225bf3b).

Sending while a session's turn is running used to push onto a JS array in the
browser: nothing reached the agent until the whole turn ended, and a reload
lost the message. These tests pin the server-side replacement.

The store tests run the REAL SQL against sqlite (the facade's ``{table}`` /
``:param`` dialect is portable for these statements), so a typo in a WHERE
clause fails here instead of in production.

Run: python -m pytest tests/test_queue.py -q
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace_agent_app import ap_client, plugin as plugin_mod  # noqa: E402
from workspace_agent_app import sessions as sm  # noqa: E402
from workspace_agent_app import store as store_mod  # noqa: E402
from workspace_agent_app.routes import build_routes  # noqa: E402
from workspace_agent_app.store import APP_QUEUE, MAX_ATTEMPTS, Store  # noqa: E402


class SqliteDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)

    def create(self, name, columns_sql):
        self.conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({columns_sql})')
        return name

    def execute(self, name, sql, params=None):
        cur = self.conn.execute(sql.replace("{table}", f'"{name}"'), params or {})
        self.conn.commit()
        return cur.fetchall()


def make_store():
    db = SqliteDb()
    db.create(APP_QUEUE, store_mod._APP_QUEUE_COLUMNS)
    db.create(store_mod.SESSION_META, store_mod._SESSION_META_COLUMNS)
    return Store(db), db


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ store


def test_enqueue_lists_oldest_first_and_scopes_to_the_session():
    st, _ = make_store()
    a = st.enqueue("s1", "qa", "first")
    time.sleep(0.01)
    b = st.enqueue("s1", "qa", "second")
    st.enqueue("s2", "qa", "other")
    items = st.queued("s1")
    assert [i["id"] for i in items] == [a, b]
    assert [i["body"] for i in items] == ["first", "second"]
    assert items[0]["status"] == "queued"
    assert sorted(st.sessions_with_queue()) == ["s1", "s2"]


def test_claim_head_takes_one_message_at_a_time_in_order():
    st, _ = make_store()
    a = st.enqueue("s1", "qa", "first")
    time.sleep(0.01)
    st.enqueue("s1", "qa", "second")
    head = st.claim_head("s1")
    assert head["id"] == a
    # Held by a claim: a second worker must not take the next one.
    assert st.claim_head("s1") is None
    st.mark_sent(a, "run-1")
    nxt = st.claim_head("s1")
    assert nxt["body"] == "second"


def test_claim_head_returns_none_when_empty_or_only_failed():
    st, db = make_store()
    assert st.claim_head("s1") is None
    qid = st.enqueue("s1", "qa", "x")
    db.execute(APP_QUEUE, "UPDATE {table} SET status = 'failed' WHERE id = :id", {"id": qid})
    assert st.claim_head("s1") is None


def test_claim_loses_the_race_when_another_worker_wrote_its_token_first(monkeypatch):
    st, db = make_store()
    qid = st.enqueue("s1", "qa", "x")
    real = db.execute

    def racing(name, sql, params=None):
        if sql.startswith("UPDATE") and "'sending'" in sql:
            # another worker wins between our SELECT and UPDATE
            real(name, "UPDATE {table} SET status='sending', claim='other' WHERE id=:id",
                 {"id": qid})
            return []
        return real(name, sql, params)

    monkeypatch.setattr(db, "execute", racing)
    assert st.claim_head("s1") is None


def test_failed_attempt_requeues_then_parks_after_max_attempts():
    st, _ = make_store()
    qid = st.enqueue("s1", "qa", "x")
    for n in range(1, MAX_ATTEMPTS):
        st.claim_head("s1")
        assert st.mark_failed_attempt(qid, "boom") == "queued"
        assert st.queued("s1")[0]["attempts"] == n
    st.claim_head("s1")
    assert st.mark_failed_attempt(qid, "boom" * 300) == "failed"
    item = st.queued("s1")[0]
    assert item["status"] == "failed" and len(item["error"]) == 500
    assert st.sessions_with_queue() == []


def test_failed_attempt_on_a_vanished_row_is_parked():
    st, _ = make_store()
    assert st.mark_failed_attempt("nope", "x") == "failed"


def test_reap_parks_only_stale_claims():
    st, db = make_store()
    old = st.enqueue("s1", "qa", "old")
    st.claim_head("s1")
    db.execute(APP_QUEUE, "UPDATE {table} SET claimed_at = :t WHERE id = :id",
               {"t": time.time() - store_mod.CLAIM_STALE_S - 5, "id": old})
    fresh = st.enqueue("s2", "qa", "fresh")
    st.claim_head("s2")
    st.reap_stale_claims()
    by_id = {i["id"]: i for s in ("s1", "s2") for i in st.queued(s)}
    assert by_id[old]["status"] == "failed"
    assert by_id[fresh]["status"] == "sending"


def test_cancel_only_withdraws_unsent_messages_of_that_session():
    st, _ = make_store()
    q = st.enqueue("s1", "qa", "x")
    assert st.cancel("s2", q) is False          # wrong session
    assert st.cancel("s1", q) is True
    assert st.queued("s1") == []
    sent = st.enqueue("s1", "qa", "y")
    st.claim_head("s1")
    st.mark_sent(sent, "r1")
    assert st.cancel("s1", sent) is False        # already a real run
    assert st.cancel("s1", "missing") is False


def test_no_database_mode_degrades_instead_of_throwing():
    st = Store(None)
    assert isinstance(st.enqueue("s1", "qa", "x"), str)
    assert st.queued("s1") == []
    assert st.sessions_with_queue() == []
    assert st.claim_head("s1") is None
    st.mark_sent("q", "r")
    assert st.mark_failed_attempt("q", "e") == "failed"
    st.reap_stale_claims()
    assert st.cancel("s1", "q") is False


def test_ensure_tables_creates_and_alters_the_queue_table():
    calls = []

    class Db:
        def create(self, n, c):
            calls.append(("create", n))

        def execute(self, n, sql, params=None):
            calls.append(("exec", n, sql))
            return []

    Store(Db()).ensure_tables()
    assert ("create", APP_QUEUE) in calls
    assert any(c[0] == "exec" and c[1] == APP_QUEUE and "ADD COLUMN IF NOT EXISTS claimed_at" in c[2]
               for c in calls)


# ------------------------------------------------------------- sessions


@pytest.fixture
def platform(monkeypatch):
    """Scriptable platform: ``state['busy']`` and a log of dispatches."""
    state = {"busy": False, "sent": [], "fail": False}

    async def busy(base, token, sid):
        return state["busy"]

    async def dispatch(base, token, store, agent, text, session_id=None):
        if state["fail"]:
            raise ap_client.APError("refused")
        state["sent"].append((agent, text, session_id))
        return {"run_id": f"run-{len(state['sent'])}", "session_id": session_id,
                "status": "pending"}

    monkeypatch.setattr(sm, "session_busy", busy)
    monkeypatch.setattr(sm, "dispatch", dispatch)
    return state


def test_send_dispatches_immediately_when_the_session_is_idle(platform):
    st, _ = make_store()
    out = run(sm.send_or_queue("b", "t", st, "qa", "hi", "s1"))
    assert out["run_id"] == "run-1" and "queued" not in out
    assert st.queued("s1") == []


def test_send_without_a_database_dispatches_even_when_busy(platform):
    platform["busy"] = True
    out = run(sm.send_or_queue("b", "t", Store(None), "qa", "hi", "s1"))
    assert out["run_id"] == "run-1"


def test_send_during_a_running_turn_is_queued_server_side_not_dispatched(platform):
    platform["busy"] = True
    st, _ = make_store()
    out = run(sm.send_or_queue("b", "t", st, "qa", "is this the latest?", "s1"))
    assert out["queued"] is True and out["status"] == "queued"
    assert platform["sent"] == []
    assert [i["body"] for i in st.queued("s1")] == ["is this the latest?"]


def test_queued_message_is_dispatched_by_the_drain_once_idle_with_no_tab(platform):
    platform["busy"] = True
    st, _ = make_store()
    run(sm.send_or_queue("b", "t", st, "qa", "hello", "s1"))
    assert run(sm.drain_all("b", "t", st)) == 0          # still mid-turn
    platform["busy"] = False
    assert run(sm.drain_all("b", "t", st)) == 1
    assert platform["sent"] == [("qa", "hello", "s1")]
    # No duplicate on the next pass.
    assert run(sm.drain_all("b", "t", st)) == 0
    assert len(platform["sent"]) == 1


def test_a_second_send_keeps_order_behind_an_existing_queue(platform):
    platform["busy"] = True
    st, _ = make_store()
    run(sm.send_or_queue("b", "t", st, "qa", "one", "s1"))
    platform["busy"] = False
    # Idle now, but "one" is still waiting: "two" must not jump ahead. The
    # inline drain sends "one" and "two" stays queued.
    out = run(sm.send_or_queue("b", "t", st, "qa", "two", "s1"))
    assert out["queued"] is True
    assert [t for _, t, _ in platform["sent"]] == ["one"]
    assert [i["body"] for i in st.queued("s1")] == ["two"]


def test_queue_that_is_drained_inline_reports_the_run(platform):
    st, _ = make_store()
    st.enqueue("s1", "qa", "stale")      # leftover; session idle
    out = run(sm.send_or_queue("b", "t", st, "qa", "new", "s1"))
    # the head ("stale") went out, ours is still queued behind it
    assert out["queued"] is True
    assert platform["sent"][0][1] == "stale"


def test_inline_drain_of_our_own_message_returns_a_run_id(platform):
    st, _ = make_store()
    calls = {"n": 0}
    real_busy = sm.session_busy

    async def busy_once(base, token, sid):
        calls["n"] += 1
        return calls["n"] == 1        # busy at the send check, idle at the drain

    sm.session_busy = busy_once
    try:
        out = run(sm.send_or_queue("b", "t", st, "qa", "mine", "s1"))
    finally:
        sm.session_busy = real_busy
    assert out["run_id"] == "run-1" and "queued" not in out


def test_failed_dispatch_retries_then_parks_visibly(platform):
    st, _ = make_store()
    st.enqueue("s1", "qa", "x")
    platform["fail"] = True
    for _ in range(MAX_ATTEMPTS):
        assert run(sm.drain_session("b", "t", st, "s1")) is None
    item = st.queued("s1")[0]
    assert item["status"] == "failed" and "refused" in item["error"]
    assert run(sm.drain_all("b", "t", st)) == 0


def test_drain_session_is_a_noop_when_empty_or_claimed_elsewhere(platform):
    st, _ = make_store()
    assert run(sm.drain_session("b", "t", st, "s1")) is None
    st.enqueue("s1", "qa", "x")
    st.claim_head("s1")
    assert run(sm.drain_session("b", "t", st, "s1")) is None


def test_drain_all_survives_one_sessions_upstream_error(monkeypatch, platform):
    st, _ = make_store()
    st.enqueue("s1", "qa", "x")

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sm, "session_busy", boom)
    assert run(sm.drain_all("b", "t", st)) == 0
    assert len(st.queued("s1")) == 1       # not lost


def test_session_busy_reads_the_platforms_run_rows(monkeypatch):
    async def fake_get(client, base, token, path, **params):
        assert path == "/api/runs" and params["session_id"] == "s1"
        return fake_get.rows

    monkeypatch.setattr(ap_client, "get_json", fake_get)
    fake_get.rows = [{"status": "success"}, {"status": "queued"}]
    assert run(sm.session_busy("b", "t", "s1")) is True
    fake_get.rows = [{"status": "success"}, {"status": "error"}]
    assert run(sm.session_busy("b", "t", "s1")) is False
    fake_get.rows = {"not": "a list"}
    assert run(sm.session_busy("b", "t", "s1")) is False
    fake_get.rows = [{}]                    # no status = pending = busy
    assert run(sm.session_busy("b", "t", "s1")) is True


def test_shape_queue_exposes_what_the_thread_needs():
    out = sm.shape_queue(
        [{"id": "q1", "body": "hi", "created_at": 5.0, "status": "queued"},
         {"id": "q2", "body": "x", "created_at": 6.0, "status": "failed", "error": "bad"}]
    )
    assert out[0] == {"queue_id": "q1", "text": "hi", "at": 5.0, "status": "queued", "error": None}
    assert out[1]["error"] == "bad"


def test_thread_carries_the_queue_so_a_reload_rehydrates_it(monkeypatch):
    st, _ = make_store()
    st.enqueue("s1", "qa", "waiting")

    async def fake_get(client, base, token, path, **params):
        return {"session_id": "s1"} if path.startswith("/api/sessions") else []

    monkeypatch.setattr(ap_client, "get_json", fake_get)
    out = run(sm.build_thread("b", "t", st, "s1"))
    assert [q["text"] for q in out["session"]["queued"]] == ["waiting"]


# --------------------------------------------------------------- routes


def test_message_route_returns_the_queued_shape(monkeypatch, platform):
    platform["busy"] = True
    st, _ = make_store()

    async def agent_for(base, token, store, sid):
        return "qa"

    monkeypatch.setattr("workspace_agent_app.routes._agent_for", agent_for)
    client = TestClient(build_routes(lambda: ("http://ap", "tok"), st))
    body = client.post("/api/sessions/s1/messages", json={"text": "hi"}).json()
    assert body["queued"] is True and body["queue_id"]


def test_cancel_route_withdraws_then_409s_once_sent():
    st, _ = make_store()
    client = TestClient(build_routes(lambda: ("http://ap", "tok"), st))
    q = st.enqueue("s1", "qa", "x")
    assert client.delete(f"/api/sessions/s1/queue/{q}").json() == {"cancelled": True}
    sent = st.enqueue("s1", "qa", "y")
    st.claim_head("s1")
    st.mark_sent(sent, "r1")
    assert client.delete(f"/api/sessions/s1/queue/{sent}").status_code == 409


# --------------------------------------------------------------- plugin


def test_plugin_drain_loop_passes_skips_without_creds_and_survives_errors(monkeypatch):
    seen = []

    async def fake_drain(base, token, store):
        seen.append((base, token))
        if len(seen) == 2:
            raise RuntimeError("boom")

    monkeypatch.setattr(plugin_mod.sessions_mod, "drain_all", fake_drain)
    monkeypatch.setattr(plugin_mod, "DRAIN_INTERVAL_S", 0)
    creds = iter([("", ""), ("b", "t"), ("b", "t"), ("b", "t")])

    async def go():
        p = plugin_mod.WorkspaceAgentPlugin()
        task = asyncio.create_task(p._drain_loop(lambda: next(creds), None))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert seen[0] == ("b", "t") and len(seen) >= 2     # empty creds skipped; error survived


def test_activate_starts_the_drain_and_deactivate_stops_it():
    async def go():
        ctx = MagicMock()
        ctx.config = {}
        p = plugin_mod.WorkspaceAgentPlugin()
        await p.activate(ctx)
        task = p._drain_task
        assert not task.done()
        await p.deactivate()
        await asyncio.sleep(0)
        assert task.cancelled()

    asyncio.run(go())
