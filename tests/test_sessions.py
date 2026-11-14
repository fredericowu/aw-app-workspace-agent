"""Coverage for workspace_agent_app/sessions.py — the rail join, the
User-vs-Agents bucketing, the thread, and dispatch.

What these tests are actually guarding, in rough order of how badly it
breaks the app if it regresses:

1. **The rail is not an N+1.** build_rail must make exactly two upstream
   calls when the sweep already covers every listed session. A regression
   here is invisible in the UI and only shows up as load on the platform.
2. **Bucketing.** A Telegram session landing in "Agents" (or vice versa) is
   the product constraint failing silently.
3. **The thread is unsummarized.** summary=true truncates input to ~200
   chars, which is right for a title and wrong for the transcript.
4. **A new session does not fork.** Dispatch with no session_id must record
   the app-dispatch marker; dispatch with one must pass it through.

Run: python -m pytest tests/test_sessions.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_agent_app import ap_client, sessions  # noqa: E402
from workspace_agent_app import routes as routes_mod  # noqa: E402
from workspace_agent_app.store import Store  # noqa: E402

# ---------------------------------------------------------------- fixtures


class FakeStore(Store):
    """In-memory stand-in for the two workspace tables, so the derive-once
    and cache-key behaviour can be tested without a Postgres."""

    def __init__(self):
        super().__init__(None)
        self.rows: dict[str, dict] = {}
        self.dispatches: dict[str, dict] = {}

    def all_meta(self):
        return {k: dict(v) for k, v in self.rows.items()}

    def meta(self, session_id):
        row = self.rows.get(session_id)
        return dict(row) if row else None

    def upsert_meta(self, session_id, **fields):
        self.rows.setdefault(session_id, {}).update(fields)

    def set_flag(self, session_id, column, value):
        self.upsert_meta(session_id, **{column: value})
        return value

    def record_dispatch(self, run_id, agent_slug):
        self.dispatches[run_id] = {"run_id": run_id, "agent_slug": agent_slug, "session_id": None}

    def dispatch(self, run_id):
        d = self.dispatches.get(run_id)
        return dict(d) if d else None

    def resolve_dispatch(self, run_id, session_id):
        self.dispatches.setdefault(run_id, {"run_id": run_id, "agent_slug": ""})
        self.dispatches[run_id]["session_id"] = session_id
        self.upsert_meta(session_id, bucket="user", created_from="app")


def run(coro):
    return asyncio.run(coro)


def _epoch_of(iso: str) -> float:
    return sessions._epoch(iso)


def session_row(session_id, run_count=1, name="", **extra):
    return {
        "session_id": session_id,
        "name": name,
        "run_count": run_count,
        "last_run_at": "2026-11-14T16:00:00+00:00",
        "updated_at": "2026-11-14T16:00:00+00:00",
        "last_status": "success",
        "retired_at": None,
        "retired_reason": None,
        **extra,
    }


def run_row(run_id, session_id, started, **extra):
    return {
        "id": run_id,
        "session_id": session_id,
        "status": "success",
        "started_at": started,
        "ended_at": started,
        "input": {"input": "hello there"},
        "output": {"text": "hi back"},
        "source_slug": "architect",
        "initiator_kind": "agent_run",
        "parent_run_id": None,
        "source_device": None,
        "cost_usd": 1.5,
        "tokens_in": 100,
        "tokens_out": 20,
        "model_slug": "claude-sonnet",
        "error": None,
        **extra,
    }


def transport(handler):
    """Patch httpx.AsyncClient so sessions.py's own calls hit `handler`."""
    return httpx.MockTransport(handler)


class _PatchedClient(httpx.AsyncClient):
    handler = None

    def __init__(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(type(self).handler)
        super().__init__(*a, **kw)


@pytest.fixture
def intercept(monkeypatch):
    """Route every upstream call to a recorded fake, and hand the test the
    list of paths that were actually requested — which is how the "no N+1"
    assertion is made at all."""
    calls: list[str] = []

    def install(routes: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(f"{request.method} {path}?{request.url.query.decode()}")
            for prefix, payload in routes.items():
                if path == prefix:
                    body = payload(request) if callable(payload) else payload
                    return httpx.Response(200, json=body)
            return httpx.Response(404, json={"detail": f"no fake for {path}"})

        _PatchedClient.handler = staticmethod(handler)
        monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)
        return calls

    return install


# ------------------------------------------------------------------ helpers


def test_epoch_parses_iso_and_tolerates_junk():
    assert sessions._epoch("2026-11-14T16:00:00+00:00") > 0
    assert sessions._epoch("2026-11-14T16:00:00Z") > 0
    assert sessions._epoch(12345.0) == 12345.0
    assert sessions._epoch("not a date") == 0.0
    assert sessions._epoch(None) == 0.0


def test_run_prompt_and_reply_tolerate_both_shapes():
    assert sessions.run_prompt({"input": {"input": "a"}}) == "a"
    assert sessions.run_prompt({"input": "a"}) == "a"
    assert sessions.run_prompt({}) == ""
    assert sessions.run_reply({"output": {"text": "b"}}) == "b"
    assert sessions.run_reply({"output": None}) == ""


def test_group_by_session_sorts_oldest_first():
    """Upstream returns newest-first; every root-run derivation wants the
    opposite, so the flip happens once in the grouper."""
    grouped = sessions._group_by_session(
        [
            run_row("r2", "s1", "2026-11-14T16:00:00+00:00"),
            run_row("r1", "s1", "2026-11-14T15:00:00+00:00"),
            run_row("r3", None, "2026-11-14T14:00:00+00:00"),
        ]
    )
    assert [r["id"] for r in grouped["s1"]] == ["r1", "r2"]
    assert None not in grouped


def test_totals_sum_cost_and_tokens():
    t = sessions.totals(
        [
            run_row("r1", "s1", "2026-11-14T15:00:00+00:00"),
            run_row(
                "r2", "s1", "2026-11-14T16:00:00+00:00", cost_usd=0.5, tokens_in=10, tokens_out=2
            ),
        ]
    )
    assert t == {"cost_usd": 2.0, "tokens_in": 110, "tokens_out": 22}


# ---------------------------------------------------------------- bucketing


@pytest.mark.parametrize(
    "root, expected",
    [
        # A human typing into Telegram is the user, and it is the ROOT run that
        # decides — this is the product constraint the whole app exists for.
        ({"initiator_kind": "telegram", "parent_run_id": None}, "user"),
        ({"initiator_kind": "watch", "parent_run_id": None}, "user"),
        # source_device is a second, independent signal for the same fact, so an
        # upstream rename of the initiator vocabulary degrades instead of
        # silently reclassifying every Telegram session.
        (
            {"initiator_kind": "something_new", "source_device": "telegram", "parent_run_id": None},
            "user",
        ),
        # telegram_system is the platform talking to itself, not a person.
        ({"initiator_kind": "telegram_system", "parent_run_id": None}, "agent"),
        # Anything with a parent was opened by another run, whatever started it.
        ({"initiator_kind": "telegram", "parent_run_id": "r0"}, "agent"),
        # The residual root cases.
        ({"initiator_kind": "agent_run", "parent_run_id": None}, "agent"),
        ({"initiator_kind": "wakeup", "parent_run_id": None}, "agent"),
        ({"initiator_kind": "workflow_run", "parent_run_id": None}, "agent"),
        # An initiator kind this build has never seen must not crash or default
        # to "user" — an unknown automation is still an automation.
        ({"initiator_kind": "plan_cadence", "parent_run_id": None}, "agent"),
        ({}, "agent"),
    ],
)
def test_bucketing_from_the_root_run(root, expected):
    derived = sessions.derive(session_row("s1"), [{**run_row("r1", "s1", ""), **root}], None)
    assert derived["bucket"] == expected


def test_a_session_this_app_started_is_always_the_users():
    """The initiator-kind heuristic cannot tell an app-started session from
    any other API dispatch, so the app-dispatch marker overrides it."""
    derived = sessions.derive(
        session_row("s1"),
        [run_row("r1", "s1", "2026-11-14T16:00:00+00:00", initiator_kind="agent_run")],
        {"created_from": "app"},
    )
    assert derived["bucket"] == "user"


# ----------------------------------------------------------------- derive


def test_the_real_session_name_always_wins_over_a_cached_title():
    """PATCH /api/sessions writes a name shared with every other consumer —
    a cached preview of the first prompt must never shadow it."""
    derived = sessions.derive(
        session_row("s1", name="Rollout plan"),
        [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        {"title": "hello there"},
    )
    assert derived["title"] == "Rollout plan"


def test_title_falls_back_to_the_first_prompt_then_to_the_id():
    unnamed = sessions.derive(
        session_row("s1"),
        [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        None,
    )
    assert unnamed["title"] == "hello there"

    empty = sessions.derive(session_row("abcdef1234"), [], None)
    assert empty["title"] == "Session abcdef12"


def test_agent_is_the_most_recent_runner_not_the_first():
    """A session can be handed between agents (Architect -> Coder -> QA);
    the one you would be talking to now is whoever ran last."""
    derived = sessions.derive(
        session_row("s1", run_count=2),
        [
            run_row("r1", "s1", "2026-11-14T15:00:00+00:00", source_slug="architect"),
            run_row("r2", "s1", "2026-11-14T16:00:00+00:00", source_slug="coder-opus"),
        ],
        None,
    )
    assert derived["agent_slug"] == "coder-opus"


def test_shape_session_marks_a_retired_session_and_carries_cost():
    row = session_row("s1", retired_at="2026-11-14T16:30:00+00:00", retired_reason="session_dead")
    runs = [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")]
    derived = sessions.derive(row, runs, None)
    shaped = sessions.shape_session(
        row, runs, derived, sessions.totals(runs), {"pinned": True, "archived": True}
    )
    assert shaped["retired"] is True
    assert shaped["retired_reason"] == "session_dead"
    assert shaped["pinned"] is True
    assert shaped["state"] == "archived"
    assert shaped["cost_usd"] == 1.5
    assert shaped["preview"] == "hi back"
    # The transcript is fetched lazily — the rail entry must not pretend to
    # have one, or openSession would render an empty thread as loaded.
    assert shaped["messages"] == [] and shaped["loaded"] is False


# -------------------------------------------------------------- rail build


def test_rail_makes_exactly_two_upstream_calls_when_the_sweep_covers_everything(intercept):
    """THE no-N+1 guarantee. run_count is authoritative, so "is the sweep
    enough" is an exact test rather than a guess — and when it passes, no
    per-session call may be made at all."""
    calls = intercept(
        {
            "/api/sessions": [session_row("s1", run_count=2), session_row("s2", run_count=1)],
            "/api/runs": [
                run_row("r3", "s2", "2026-11-14T16:00:00+00:00"),
                run_row("r2", "s1", "2026-11-14T15:30:00+00:00"),
                run_row("r1", "s1", "2026-11-14T15:00:00+00:00"),
            ],
        }
    )
    store = FakeStore()
    out = run(sessions.build_rail("http://ap", "tok", store))

    assert out["backfilled"] == 0
    assert len(calls) == 2, calls
    assert [s["id"] for s in out["sessions"]] == ["s1", "s2"]
    assert out["sessions"][0]["cost_usd"] == 3.0


def test_rail_backfills_only_the_sessions_the_sweep_missed(intercept):
    """s1's run_count says 5 but the sweep only carries 1, so that one
    session gets its own call. s2 is complete and must NOT."""

    def runs(request):
        if request.url.params.get("session_id") == "s1":
            return [run_row(f"r{i}", "s1", f"2026-11-14T1{i}:00:00+00:00") for i in range(5)]
        return [
            run_row("r9", "s2", "2026-11-14T16:00:00+00:00"),
            run_row("r0", "s1", "2026-11-14T10:00:00+00:00"),
        ]

    calls = intercept(
        {
            "/api/sessions": [session_row("s1", run_count=5), session_row("s2", run_count=1)],
            "/api/runs": runs,
        }
    )
    store = FakeStore()
    out = run(sessions.build_rail("http://ap", "tok", store))

    assert out["backfilled"] == 1
    per_session = [c for c in calls if "session_id=s" in c]
    assert len(per_session) == 1 and "session_id=s1" in per_session[0]
    assert next(s for s in out["sessions"] if s["id"] == "s1")["cost_usd"] == 7.5


def test_a_cached_cost_snapshot_skips_the_refetch_until_the_run_count_moves(intercept):
    """Cost is NOT immutable, so the snapshot is keyed on the run_count it
    was computed at. Same count -> no call. Count moved -> refetch."""
    calls = intercept(
        {
            "/api/sessions": [session_row("s1", run_count=5)],
            "/api/runs": lambda req: (
                [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")]
                if req.url.params.get("session_id")
                else []
            ),
        }
    )
    store = FakeStore()
    store.upsert_meta(
        "s1",
        agent_slug="architect",
        bucket="agent",
        title="cached",
        first_run_at=1.0,
        cost_usd=42.0,
        tokens_in=7,
        tokens_out=3,
        cached_run_count=5,
    )

    out = run(sessions.build_rail("http://ap", "tok", store))
    assert out["backfilled"] == 0
    assert len(calls) == 2, calls
    assert out["sessions"][0]["cost_usd"] == 42.0
    assert out["sessions"][0]["title"] == "cached"

    calls.clear()
    store.rows["s1"]["cached_run_count"] = 4  # a new turn has landed
    out = run(sessions.build_rail("http://ap", "tok", store))
    assert out["backfilled"] == 1
    assert out["sessions"][0]["cost_usd"] == 1.5


def test_rail_persists_derive_once_columns_and_never_rewrites_them(intercept):
    intercept(
        {
            "/api/sessions": [session_row("s1", run_count=1)],
            "/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        }
    )
    store = FakeStore()
    run(sessions.build_rail("http://ap", "tok", store))
    assert store.rows["s1"]["agent_slug"] == "architect"
    assert store.rows["s1"]["title"] == "hello there"
    assert store.rows["s1"]["cached_run_count"] == 1

    # A later pass where the newest run is a different agent must not rewrite
    # the derive-once columns: the session's first run is immutable, and
    # re-deriving a title from a partial view is how titles start drifting.
    store.rows["s1"]["title"] = "pinned title"
    run(sessions.build_rail("http://ap", "tok", store))
    assert store.rows["s1"]["title"] == "pinned title"


def test_one_sessions_backfill_failing_does_not_fail_the_rail(intercept):
    """A single bad session must degrade to its partial view, not take the
    whole rail down — it retries on the next refresh anyway."""

    def runs(request):
        if request.url.params.get("session_id"):
            raise httpx.ConnectError("boom")
        return [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")]

    intercept(
        {
            "/api/sessions": [session_row("s1", run_count=9), session_row("s2", run_count=9)],
            "/api/runs": runs,
        }
    )
    out = run(sessions.build_rail("http://ap", "tok", FakeStore()))
    assert [s["id"] for s in out["sessions"]] == ["s1", "s2"]
    assert out["sessions"][0]["cost_usd"] == 1.5  # the partial view
    assert out["sessions"][1]["cost_usd"] == 0.0  # nothing visible at all


def test_rail_skips_rows_with_no_session_id(intercept):
    intercept(
        {
            "/api/sessions": [{"session_id": None}, session_row("s1")],
            "/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        }
    )
    out = run(sessions.build_rail("http://ap", "tok", FakeStore()))
    assert [s["id"] for s in out["sessions"]] == ["s1"]


# ----------------------------------------------------- the cheap (~10s) rail


def test_fast_rail_makes_one_upstream_call_and_never_touches_api_sessions(intercept):
    """The whole reason the fast path exists. GET /api/sessions measured
    ~4.8s on this workspace (two queries per row upstream), so a 10s poll of
    it would sit at a ~50% duty cycle on an endpoint every other consumer of
    the platform shares."""
    calls = intercept({"/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")]})
    store = FakeStore()
    out = run(sessions.build_rail("http://ap", "tok", store, full=False))

    assert len(calls) == 1, calls
    assert not any("/api/sessions" in c for c in calls)
    assert out["full"] is False
    assert [s["id"] for s in out["sessions"]] == ["s1"]


def test_fast_rail_surfaces_a_session_this_app_has_never_seen(intercept):
    """This is acceptance criterion 3: a session started from Telegram
    appears within one refresh. Its first run is in the sweep immediately,
    so it must not wait for the next full refresh to show up."""
    intercept(
        {
            "/api/runs": [
                run_row(
                    "r1",
                    "tg-new",
                    "2026-11-14T16:00:00+00:00",
                    initiator_kind="telegram",
                    source_device="telegram",
                    source_slug="telegram-sonnet-aw",
                )
            ]
        }
    )
    store = FakeStore()
    out = run(sessions.build_rail("http://ap", "tok", store, full=False))
    s = out["sessions"][0]
    assert s["id"] == "tg-new"
    assert s["source"] == "user"  # bucketed, not just listed
    assert s["agent"] == "telegram-sonnet-aw"  # and attributed
    assert s["run_count"] == 1 and s["cost_usd"] == 1.5


def test_fast_rail_advances_the_run_count_by_exact_delta(intercept):
    """Not an estimate: a run counts as new iff it started strictly after the
    cached last_run_at, and a run can never be both newer than that and
    outside the newest-1000 sweep."""
    old = _epoch_of("2026-11-14T15:00:00+00:00")
    intercept(
        {
            "/api/runs": [
                run_row("r1", "s1", "2026-11-14T15:00:00+00:00"),  # already counted
                run_row("r2", "s1", "2026-11-14T16:00:00+00:00"),  # new
            ]
        }
    )
    store = FakeStore()
    store.upsert_meta(
        "s1",
        agent_slug="architect",
        bucket="agent",
        title="t",
        first_run_at=old,
        cost_usd=10.0,
        tokens_in=500,
        tokens_out=50,
        cached_run_count=4,
        last_run_at=old,
    )

    out = run(sessions.build_rail("http://ap", "tok", store, full=False))
    s = out["sessions"][0]
    assert s["run_count"] == 5  # 4 cached + 1 genuinely new
    assert s["cost_usd"] == 11.5  # 10.0 + the new run's 1.5
    assert s["tokens_in"] == 600
    # ...and the advanced baseline is persisted, so the next poll does not
    # count r2 a second time.
    assert store.rows["s1"]["cached_run_count"] == 5
    out2 = run(sessions.build_rail("http://ap", "tok", store, full=False))
    assert out2["sessions"][0]["run_count"] == 5
    assert out2["sessions"][0]["cost_usd"] == 11.5


def test_fast_rail_leaves_an_idle_session_exactly_as_cached(intercept):
    now = _epoch_of("2026-11-14T16:00:00+00:00")
    intercept({"/api/runs": []})  # nothing ran at all
    store = FakeStore()
    store.upsert_meta(
        "s1",
        agent_slug="qa-sonnet",
        bucket="agent",
        title="cached",
        first_run_at=1.0,
        cost_usd=7.25,
        tokens_in=11,
        tokens_out=3,
        cached_run_count=2,
        last_run_at=now,
        name="Named upstream",
        last_status="success",
    )

    out = run(sessions.build_rail("http://ap", "tok", store, full=False))
    s = out["sessions"][0]
    assert (s["run_count"], s["cost_usd"], s["tokens_in"]) == (2, 7.25, 11)
    assert s["title"] == "Named upstream"  # the real name still wins
    assert s["last_status"] == "success"


def test_fast_rail_carries_the_cached_retired_flag(intercept):
    """retired_at only comes from /api/sessions, which the fast path skips —
    so a retired session must not come back looking resumable."""
    intercept({"/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")]})
    store = FakeStore()
    store.upsert_meta(
        "s1",
        agent_slug="a",
        bucket="agent",
        title="t",
        first_run_at=1.0,
        cached_run_count=1,
        last_run_at=0.0,
        retired_at="2026-11-14T16:30:00+00:00",
        retired_reason="session_dead",
    )
    out = run(sessions.build_rail("http://ap", "tok", store, full=False))
    assert out["sessions"][0]["retired"] is True
    assert out["sessions"][0]["retired_reason"] == "session_dead"


def test_fast_rail_is_bounded_even_though_the_cache_grows_forever(intercept):
    """session_meta accumulates every session ever seen; the response must
    not grow with it."""
    intercept({"/api/runs": []})
    store = FakeStore()
    for i in range(sessions.SESSION_LIMIT + 25):
        store.upsert_meta(
            f"s{i:04d}",
            agent_slug="a",
            bucket="agent",
            title="t",
            first_run_at=float(i),
            cached_run_count=1,
            last_run_at=float(i),
        )
    out = run(sessions.build_rail("http://ap", "tok", store, full=False))
    assert len(out["sessions"]) == sessions.SESSION_LIMIT
    # and it keeps the most recent ones, not an arbitrary slice
    assert out["sessions"][0]["id"] == f"s{sessions.SESSION_LIMIT + 24:04d}"


def test_the_full_rail_mirrors_what_only_api_sessions_knows(intercept):
    """The fast path reads name/retired/last_status out of the cache, so the
    full path has to put them there — otherwise the cheap refresh would drop
    a rename and un-retire a dead session."""
    intercept(
        {
            "/api/sessions": [
                session_row(
                    "s1",
                    name="Rollout",
                    last_status="error",
                    retired_at="2026-11-14T16:30:00+00:00",
                    retired_reason="session_dead",
                )
            ],
            "/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        }
    )
    store = FakeStore()
    run(sessions.build_rail("http://ap", "tok", store))
    assert store.rows["s1"]["name"] == "Rollout"
    assert store.rows["s1"]["retired_at"] == "2026-11-14T16:30:00+00:00"
    assert store.rows["s1"]["retired_reason"] == "session_dead"
    assert store.rows["s1"]["last_status"] == "error"
    assert store.rows["s1"]["last_run_at"] > 0


def test_build_rail_defaults_to_the_full_build(intercept):
    """Bootstrap and the periodic refresh rely on the default being
    authoritative, not cheap."""
    calls = intercept(
        {
            "/api/sessions": [session_row("s1")],
            "/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        }
    )
    out = run(sessions.build_rail("http://ap", "tok", FakeStore()))
    assert out["full"] is True
    assert any("/api/sessions" in c for c in calls)


# --------------------------------------------------------------- thread view


def test_thread_fetches_runs_UNsummarized(intercept):
    """summary=true truncates input to ~200 chars. Right for a rail title,
    wrong for the thing the user came here to read."""
    calls = intercept(
        {
            "/api/sessions/s1": session_row("s1", run_count=1),
            "/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        }
    )
    run(sessions.build_thread("http://ap", "tok", FakeStore(), "s1"))
    runs_call = next(c for c in calls if c.startswith("GET /api/runs?"))
    assert "summary" not in runs_call, runs_call


def test_thread_pairs_each_run_into_a_user_and_an_agent_turn(intercept):
    intercept(
        {
            "/api/sessions/s1": session_row("s1", run_count=1),
            "/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        }
    )
    out = run(sessions.build_thread("http://ap", "tok", FakeStore(), "s1"))
    msgs = out["session"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "agent"]
    assert msgs[0]["text"] == "hello there"
    assert msgs[1]["text"] == "hi back"
    # The PO's cost requirement lands exactly here, on the turn.
    assert msgs[1]["cost_usd"] == 1.5
    assert (msgs[1]["tokens_in"], msgs[1]["tokens_out"]) == (100, 20)
    assert out["session"]["live_run_id"] is None


def test_thread_reports_an_in_flight_run_so_the_ui_can_stream_it(intercept):
    intercept(
        {
            "/api/sessions/s1": session_row("s1", run_count=1, last_status="running"),
            "/api/runs": [
                run_row(
                    "r1",
                    "s1",
                    "2026-11-14T16:00:00+00:00",
                    status="running",
                    output=None,
                    ended_at=None,
                )
            ],
        }
    )
    out = run(sessions.build_thread("http://ap", "tok", FakeStore(), "s1"))
    assert out["session"]["live_run_id"] == "r1"
    assert out["session"]["messages"][-1]["pending"] is True


def test_a_failed_run_shows_its_error_instead_of_an_empty_bubble():
    """A run that ends with no output text and an error must say so — an
    empty agent bubble reads as the agent having nothing to say."""
    msgs = sessions.build_messages(
        [
            run_row(
                "r1",
                "s1",
                "2026-11-14T16:00:00+00:00",
                status="error",
                output=None,
                error="container died",
            ),
        ]
    )
    assert "container died" in msgs[-1]["text"]
    assert msgs[-1]["pending"] is False


def test_a_run_with_no_prompt_still_yields_its_reply():
    msgs = sessions.build_messages(
        [
            run_row("r1", "s1", "2026-11-14T16:00:00+00:00", input={}),
        ]
    )
    assert [m["role"] for m in msgs] == ["agent"]


# ----------------------------------------------------------------- dispatch


def test_a_new_session_dispatch_sends_no_session_id_and_records_the_marker(intercept):
    """Sending session_id on a new session is impossible (there isn't one);
    the marker is what later buckets this session as the user's own."""
    seen = {}

    def handler(request):
        import json as _json

        seen.update(_json.loads(request.content))
        return {"run_id": "r42", "target_id": "t1"}

    intercept({"/api/agents/architect/run": handler})
    store = FakeStore()
    out = run(sessions.dispatch("http://ap", "tok", store, "architect", "go"))

    assert out == {"run_id": "r42", "session_id": None, "status": "pending"}
    assert seen == {"input": {"input": "go"}}
    assert "session_id" not in seen
    assert store.dispatches["r42"]["agent_slug"] == "architect"


def test_a_continuation_passes_session_id_and_records_no_marker(intercept):
    """Without session_id this would silently open a SECOND session instead
    of continuing the one on screen."""
    seen = {}

    def handler(request):
        import json as _json

        seen.update(_json.loads(request.content))
        return {"run_id": "r43"}

    intercept({"/api/agents/coder-opus/run": handler})
    store = FakeStore()
    out = run(
        sessions.dispatch("http://ap", "tok", store, "coder-opus", "next", session_id="sess-1")
    )
    assert seen["session_id"] == "sess-1"
    assert out["session_id"] == "sess-1"
    assert store.dispatches == {}


def test_dispatch_raises_when_the_platform_returns_no_run_id(intercept):
    intercept({"/api/agents/a/run": {"target_id": "t1"}})
    with pytest.raises(ap_client.APError, match="no run_id"):
        run(sessions.dispatch("http://ap", "tok", FakeStore(), "a", "go"))


def test_run_status_resolves_a_new_sessions_id_exactly_once(intercept):
    """This poll is how a `run:<id>` tab becomes a real session, and it is
    called repeatedly — the resolve must be idempotent."""
    intercept({"/api/runs/r42": run_row("r42", "sess-new", "2026-11-14T16:00:00+00:00")})
    store = FakeStore()
    store.record_dispatch("r42", "architect")

    out = run(sessions.run_status("http://ap", "tok", store, "r42"))
    assert out["session_id"] == "sess-new"
    assert out["done"] is True
    assert out["cost_usd"] == 1.5
    assert store.rows["sess-new"] == {"bucket": "user", "created_from": "app"}

    store.rows["sess-new"]["bucket"] = "touched"
    run(sessions.run_status("http://ap", "tok", store, "r42"))
    assert store.rows["sess-new"]["bucket"] == "touched"


def test_run_status_reports_an_unfinished_run_as_not_done(intercept):
    """`done` is what releases the UI from its thinking state, so a pending
    or running row must never set it."""
    for status in ("pending", "running"):
        intercept(
            {
                "/api/runs/r1": run_row(
                    "r1", "s1", "2026-11-14T16:00:00+00:00", status=status, output=None
                )
            }
        )
        out = run(sessions.run_status("http://ap", "tok", FakeStore(), "r1"))
        assert out["done"] is False, status

    for status in ("success", "error", "cancelled"):
        intercept({"/api/runs/r1": run_row("r1", "s1", "2026-11-14T16:00:00+00:00", status=status)})
        out = run(sessions.run_status("http://ap", "tok", FakeStore(), "r1"))
        assert out["done"] is True, status


def test_run_status_tolerates_a_run_with_no_session_yet(intercept):
    intercept(
        {
            "/api/runs/r1": run_row(
                "r1", None, "2026-11-14T16:00:00+00:00", status="pending", output=None
            )
        }
    )
    out = run(sessions.run_status("http://ap", "tok", FakeStore(), "r1"))
    assert out["session_id"] is None and out["done"] is False


# ------------------------------------------------- resolving a session's agent


def test_agent_for_prefers_the_cache_and_makes_no_upstream_call(intercept):
    calls = intercept({})
    store = FakeStore()
    store.upsert_meta("s1", agent_slug="coder-opus")
    assert run(routes_mod._agent_for("http://ap", "tok", store, "s1")) == "coder-opus"
    assert calls == []


def test_agent_for_falls_back_to_the_sessions_newest_run(intercept):
    """A session the rail has never derived — deep in the list, or created
    since the last refresh — must still be answerable."""
    intercept(
        {"/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00", source_slug="qa-sonnet")]}
    )
    assert run(routes_mod._agent_for("http://ap", "tok", FakeStore(), "s1")) == "qa-sonnet"


def test_agent_for_says_why_it_cannot_resolve_an_empty_session(intercept):
    intercept({"/api/runs": []})
    with pytest.raises(ap_client.APError, match="no runs to read a slug from"):
        run(routes_mod._agent_for("http://ap", "tok", FakeStore(), "s1"))


# ------------------------------------------- cache writes must never fail a read


def test_a_failing_cache_write_does_not_fail_the_rail(intercept):
    """session_meta is a cache. If Postgres is unhappy the rail still has to
    render from what it just fetched — degrade, don't 502."""

    class Broken(FakeStore):
        def upsert_meta(self, session_id, **fields):
            raise RuntimeError("relation does not exist")

    intercept(
        {
            "/api/sessions": [session_row("s1")],
            "/api/runs": [run_row("r1", "s1", "2026-11-14T16:00:00+00:00")],
        }
    )
    out = run(sessions.build_rail("http://ap", "tok", Broken()))
    assert out["sessions"][0]["title"] == "hello there"


def test_a_failing_dispatch_marker_does_not_fail_the_dispatch(intercept):
    """Losing the marker costs a bucket (the session shows as agent-started),
    not the run the user just paid for."""

    class Broken(FakeStore):
        def record_dispatch(self, run_id, agent_slug):
            raise RuntimeError("relation does not exist")

    intercept({"/api/agents/a/run": {"run_id": "r1"}})
    out = run(sessions.dispatch("http://ap", "tok", Broken(), "a", "go"))
    assert out["run_id"] == "r1"


def test_a_failing_resolve_does_not_fail_the_poll(intercept):
    """This poll is what releases the UI from its thinking state — it must
    survive a cache write failing."""

    class Broken(FakeStore):
        def resolve_dispatch(self, run_id, session_id):
            raise RuntimeError("relation does not exist")

    intercept({"/api/runs/r1": run_row("r1", "sess-new", "2026-11-14T16:00:00+00:00")})
    store = Broken()
    store.record_dispatch("r1", "a")
    out = run(sessions.run_status("http://ap", "tok", store, "r1"))
    assert out["session_id"] == "sess-new" and out["done"] is True


def test_rename_goes_through_the_real_patch(intercept):
    """Renaming in this app's own table would hide the name from Telegram and
    every other consumer — the whole point is that it is shared."""
    calls = intercept({"/api/sessions/s1": lambda req: {"name": "Rollout"}})
    out = run(sessions.rename("http://ap", "tok", "s1", "Rollout"))
    assert out == {"title": "Rollout"}
    assert calls == ["PATCH /api/sessions/s1?"]
