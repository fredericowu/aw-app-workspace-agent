"""The Sessions surface: the rail, a session's thread, and dispatch.

The product constraint this module exists to honour: **the sessions shown
here are the same CLI sessions Telegram and every agent dispatch create.**
So everything below is built on ``GET /api/sessions`` (the real
``CliSession`` rows) joined to ``GET /api/runs`` — never on
``POST /api/playground/chat``, which keys its own history on
``initiator_kind="chat"``, never creates a ``CliSession``, and therefore can
neither appear in the rail nor resume a session Telegram started.

**A message is a run, not a chat turn.** ``run.input.input`` is what was
said, ``run.output.text`` is the reply, and ``tokens_in`` / ``tokens_out`` /
``cost_usd`` on that same row are what the turn actually cost. There is no
separate message store to keep in sync, which is the point.

### The rail join, without an N+1

One ``GET /api/sessions?limit=100`` plus ONE ``GET
/api/runs?summary=true&limit=1000`` — then grouped by ``session_id`` here.
Whether that sweep is enough for a given session is an exact test, not a
guess: ``CliSessionOut.run_count`` is authoritative, so a session is fully
covered iff the sweep yielded that many runs for it. Anything short falls
back to one lazy ``GET /api/runs?session_id=…`` for that session alone, and
the derived result is cached in ``session_meta`` (see store.py for which
columns are a one-off derivation and which are a snapshot).

In the steady state that makes a rail refresh exactly two upstream calls,
however many sessions are listed.

### The precondition to keep in mind when changing any of this

Every fact derived from a session's FIRST run — its User-vs-Agents bucket,
its fallback title, when it started — is only valid from a **complete** view
of that session's runs. The sweep is the newest 1000 runs *platform-wide*,
so for any session older than that window ``runs[0]`` is a mid-session run
rather than the root. ``derive()`` and ``_persist()`` both take a
``complete`` flag for exactly this reason, and getting it wrong is not a
visible crash: it is a Telegram session quietly filed under Agents. That
shipped once (2026-11-14, v0.3.0) and was caught only by running the
installed app against real data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from . import ap_client

log = logging.getLogger("aw_apps.workspace_agent.sessions")

#: The rail asks for this many sessions (upstream caps at 500, but each row
#: costs two queries over there — the modest cadence the design calls for).
SESSION_LIMIT = 100

#: One sweep, sized to cover well past the window the rail displays.
SWEEP_LIMIT = 1000

#: Concurrency cap on the lazy per-session backfill. Only cold-cache loads
#: and genuinely-grown sessions reach it, but a cold start with 100 uncovered
#: sessions must not open 100 sockets at once.
_BACKFILL_CONCURRENCY = 8

#: Initiator kinds that mean "a human typed this into a chat channel". A
#: telegram ROOT run is the user; a telegram_system run is the platform
#: talking to itself, which is why it is not here.
_USER_INITIATOR_KINDS = frozenset({"telegram", "watch"})

#: Statuses the platform treats as terminal (api/runs.py's stream route uses
#: exactly this set to decide whether to close with a `done` event).
TERMINAL_STATUSES = frozenset({"success", "error", "cancelled"})

#: How much of a run's input becomes a session title.
_TITLE_CHARS = 58


def _epoch(value: Any) -> float:
    """ISO-8601 (what the platform returns) to epoch seconds (what the
    frontend's ``ago()``/``dayLabel()`` helpers take). 0 for anything
    unparseable, so a missing timestamp sorts last rather than throwing."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


#: Public name for the same helper — notifications.py needs it too.
epoch = _epoch


async def fetch_sweep(client: httpx.AsyncClient, base: str, token: str) -> list[dict]:
    """The one ``GET /api/runs?summary=true&limit=SWEEP_LIMIT`` sweep every
    rail build is keyed on. Also notifications.py's only input for the flow
    escalations feed: a ``flow_needs_human`` run is read off this exact same
    call rather than a second upstream query shaped for that feature alone."""
    sweep = await ap_client.get_json(
        client, base, token, "/api/runs", summary="true", limit=SWEEP_LIMIT
    )
    return sweep if isinstance(sweep, list) else []


def flagged_runs(sweep: list[dict]) -> list[dict]:
    """The sweep's ``flow_needs_human`` runs — the System tab's escalations
    feed. Riding on the rail's own sweep is what keeps that feed from costing
    a second ``/api/runs`` call: every rail build already holds this list."""
    return [r for r in sweep if r.get("flow_needs_human")]


def run_prompt(run: dict) -> str:
    """What was said to the agent. ``input`` is a dict on every real run, but
    tolerate a bare string — older rows and ``run_sync`` both exist."""
    inp = run.get("input")
    if isinstance(inp, dict):
        return str(inp.get("input") or "")
    return str(inp or "")


def run_reply(run: dict) -> str:
    out = run.get("output")
    if isinstance(out, dict):
        return str(out.get("text") or "")
    return str(out or "")


def _one_line(text: str, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed[:limit] if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _is_user_started(root: dict) -> bool:
    """Did a human start this session in a chat channel?

    Checked on the session's ROOT run only. ``source_device`` is consulted
    alongside ``initiator_kind`` deliberately: the bucketing rule is pinned
    to today's initiator vocabulary, and a second independent signal for the
    same fact makes an upstream rename degrade instead of silently
    reclassifying every Telegram session as agent-started.
    """
    if root.get("parent_run_id"):
        return False
    return (
        root.get("initiator_kind") in _USER_INITIATOR_KINDS
        or root.get("source_device") in _USER_INITIATOR_KINDS
    )


def derive(row: dict, runs: list[dict], meta: dict | None, *, complete: bool = False) -> dict:
    """Derive one session's display facts.

    ``runs`` is every run of this session we can see, ascending by start.
    ``meta`` is the cached row, if any.

    ``complete`` says whether ``runs[0]`` really is the session's ROOT run,
    and it decides who wins:

    * ``complete=True`` — derive from the runs and let that OVERRIDE the
      cache. The root is authoritative, so this is also the repair path for a
      row an earlier partial view got wrong.
    * ``complete=False`` — prefer the cache, because ``runs[0]`` is then just
      the oldest run that happened to be inside the sweep window. Deriving a
      bucket or a title from that is what mis-bucketed a 486-run Telegram
      session as Agents (2026-11-14); see ``_persist``.

    Two things ignore the flag, on purpose:

    * ``created_from == "app"`` always buckets as the user's. It is an
      app-local fact, not something any run can tell us, and the
      initiator-kind heuristic cannot distinguish an app-started session
      from any other API dispatch.
    * the real ``CliSession.name`` always wins as the title. It is shared
      with every other consumer (that is what ``PATCH /api/sessions``
      writes), so a derived preview must never shadow it.
    """
    meta = meta or {}
    root = runs[0] if runs else {}
    newest = runs[-1] if runs else {}

    created_from = meta.get("created_from")
    if created_from == "app":
        bucket = "user"
    elif complete and runs:
        bucket = "user" if _is_user_started(root) else "agent"
    else:
        bucket = meta.get("bucket") or ("user" if _is_user_started(root) else "agent")

    # The agent you would be talking to now is whoever ran most recently, and
    # the newest run we can SEE is authoritative for that on either path —
    # a session handed from Architect to Coder to QA must not keep answering
    # as the agent that opened it. The cache is only the fallback.
    agent = newest.get("source_slug") or root.get("source_slug") or meta.get("agent_slug") or ""

    title = (row.get("name") or "").strip()
    if not title:
        derived_title = _one_line(run_prompt(root), _TITLE_CHARS)
        title = (
            derived_title if (complete and derived_title) else (meta.get("title") or derived_title)
        )
    if not title:
        title = f"Session {str(row.get('session_id') or '')[:8]}"

    first_at = _epoch(root.get("started_at"))
    if not (complete and first_at):
        first_at = meta.get("first_run_at") or first_at

    return {
        "agent_slug": agent,
        "bucket": bucket,
        "title": title,
        "origin": meta.get("origin") or None,
        "created_from": created_from,
        "first_run_at": first_at,
    }


def totals(runs: list[dict]) -> dict:
    return {
        "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in runs), 6),
        "tokens_in": sum(int(r.get("tokens_in") or 0) for r in runs),
        "tokens_out": sum(int(r.get("tokens_out") or 0) for r in runs),
    }


def shape_session(
    row: dict, runs: list[dict], derived: dict, totals_: dict, meta: dict | None
) -> dict:
    """One rail entry, in the shape frontend/app.js holds in ``S.sessions``."""
    meta = meta or {}
    sid = row.get("session_id") or ""
    retired = bool(row.get("retired_at"))
    archived = bool(meta.get("archived"))
    newest = runs[-1] if runs else {}
    last_status = row.get("last_status") or newest.get("status") or None

    # The rail preview comes from the newest run we can see, so the list says
    # something useful before its thread has ever been opened. The thread
    # view replaces it with the real messages.
    preview = _one_line(run_reply(newest) or run_prompt(newest), 110)

    return {
        "id": sid,
        "session_id": sid,
        "agent": derived["agent_slug"],
        "title": derived["title"],
        "source": derived["bucket"],
        "origin": derived["origin"],
        "state": "archived" if archived else "open",
        "pinned": bool(meta.get("pinned")),
        "unread": 0,
        "target": None,
        "updated_at": _epoch(row.get("last_run_at")) or _epoch(row.get("updated_at")),
        "created_at": derived["first_run_at"],
        "run_count": int(row.get("run_count") or 0),
        "last_status": last_status,
        "retired": retired,
        "retired_reason": row.get("retired_reason") or None,
        "preview": preview,
        "messages": [],
        "loaded": False,
        **totals_,
    }


# --------------------------------------------------------------- rail build


def _group_by_session(runs: list[dict]) -> dict[str, list[dict]]:
    """Group a sweep by session, ascending by start within each session.

    Upstream returns runs newest-first; the thread and every "root run"
    derivation want oldest-first, so the sort happens once here rather than
    at each of the half-dozen call sites that would otherwise have to
    remember.
    """
    grouped: dict[str, list[dict]] = {}
    for r in runs:
        sid = r.get("session_id")
        if sid:
            grouped.setdefault(sid, []).append(r)
    for rs in grouped.values():
        rs.sort(key=lambda r: _epoch(r.get("started_at")))
    return grouped


async def _session_runs(
    client: httpx.AsyncClient, base: str, token: str, session_id: str
) -> list[dict]:
    runs = await ap_client.get_json(
        client,
        base,
        token,
        "/api/runs",
        session_id=session_id,
        summary="true",
        limit=SWEEP_LIMIT,
    )
    runs = runs if isinstance(runs, list) else []
    runs.sort(key=lambda r: _epoch(r.get("started_at")))
    return runs


async def build_rail(base: str, token: str, store, full: bool = True) -> dict:
    """The whole rail: ``{"sessions": [...], "backfilled": n, "full": bool}``.

    ``backfilled`` is how many sessions needed their own upstream call this
    pass — 0 in the steady state. It rides on the response rather than only
    reaching a log, because "the rail got expensive" is otherwise invisible
    until someone profiles it.

    ``full=False`` takes the cheap path (``build_rail_fast``), which is what
    the ~10s poll uses. MEASURED, on this workspace, 2026-11-14:

        GET /api/sessions?limit=100   4.75s   (two queries per row upstream)
        GET /api/sessions?limit=10    0.53s   (linear: ~48ms per row)
        GET /api/runs?summary&limit=1000  0.40s

    So the prescribed two-call rail costs ~5s, and polling it every 10s would
    put a ~50% duty cycle on an endpoint shared with every other consumer of
    the platform — the opposite of the "keep the cadence modest" the design
    calls for. Shrinking ``limit`` is NOT the fix: ``/api/sessions`` is
    ordered by ``CliSession.updated_at``, which has ``onupdate=_now`` and so
    only moves when the session ROW is written — a new run does not touch it.
    Measured on the same data, 120 of 498 adjacent pairs are already inverted
    against ``last_run_at``, so a smaller window would start dropping active
    sessions rather than only inactive ones.
    """
    if not full:
        return await build_rail_fast(base, token, store)
    cached = store.all_meta()
    async with httpx.AsyncClient() as client:
        rows, sweep = await asyncio.gather(
            ap_client.get_json(client, base, token, "/api/sessions", limit=SESSION_LIMIT),
            fetch_sweep(client, base, token),
        )
        rows = rows if isinstance(rows, list) else []
        by_session = _group_by_session(sweep)

        # Decide per session whether the sweep is enough, and only then fan
        # out. run_count is authoritative, so this is an exact test.
        needs_backfill: list[dict] = []
        plan: dict[str, list[dict] | None] = {}
        for row in rows:
            sid = row.get("session_id")
            if not sid:
                continue
            want = int(row.get("run_count") or 0)
            have = by_session.get(sid, [])
            meta = cached.get(sid) or {}
            if want and len(have) >= want:
                plan[sid] = have  # covered — exact, free
            elif meta.get("cached_run_count") == want and want and meta.get("derived_complete"):
                # Skipping the fetch needs BOTH halves of the cache to be
                # trustworthy, not just the cost snapshot. `derived_complete`
                # is the second half: it records that the root-derived
                # columns were written from a view that actually held the
                # root run. Without that condition this shortcut silently
                # defeated the repair path — a session whose cost snapshot
                # was still valid was never refetched, so a bucket written
                # from a partial view could never be corrected. That is why
                # v0.3.1's self-heal did not fire on `planner sonnet`.
                plan[sid] = None
            else:
                needs_backfill.append(row)

        if needs_backfill:
            sem = asyncio.Semaphore(_BACKFILL_CONCURRENCY)

            async def one(row: dict) -> tuple[str, list[dict]]:
                async with sem:
                    sid = row["session_id"]
                    try:
                        return sid, await _session_runs(client, base, token, sid)
                    except (ap_client.APError, httpx.HTTPError) as exc:
                        # One session's backfill failing must not fail the
                        # rail — it renders from the sweep's partial view and
                        # tries again next refresh.
                        log.warning("rail backfill failed for session %s: %s", sid[:8], exc)
                        return sid, by_session.get(sid, [])

            for sid, runs in await asyncio.gather(*(one(r) for r in needs_backfill)):
                plan[sid] = runs

    sessions = []
    for row in rows:
        sid = row.get("session_id")
        if not sid or sid not in plan:
            continue
        meta = cached.get(sid) or {}
        runs = plan[sid]
        # run_count is authoritative, so this is an exact test of "is runs[0]
        # really this session's root run" — the precondition every
        # root-derived fact depends on. It also covers a backfill that hit
        # the 1000-run ceiling.
        complete = runs is not None and len(runs) >= int(row.get("run_count") or 0)
        derived = derive(row, runs if runs is not None else [], meta, complete=complete)
        if runs is None:
            totals_ = {
                "cost_usd": meta.get("cost_usd") or 0.0,
                "tokens_in": meta.get("tokens_in") or 0,
                "tokens_out": meta.get("tokens_out") or 0,
            }
        else:
            totals_ = totals(runs)
            _persist(store, sid, row, meta, derived, totals_, complete=complete)
        sessions.append(shape_session(row, runs or [], derived, totals_, meta))

    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return {
        "sessions": sessions,
        "backfilled": len(needs_backfill),
        "full": True,
        "flagged_runs": flagged_runs(sweep),
    }


async def build_rail_fast(base: str, token: str, store) -> dict:
    """The ~10s rail refresh: ONE upstream call (the sweep) plus one read of
    this app's own table. No ``/api/sessions`` — see build_rail's docstring
    for the measurement that makes that worth doing.

    What the sweep cannot give, and what happens instead:

    * the upstream ``name`` and ``retired_at`` — carried in the cache,
      written by the last full refresh. A session this app has never seen
      has neither, so it renders with its derived title and as not-retired
      until the next full refresh. For a brand-new session that is also the
      truth: ``CliSession.name`` starts empty.
    * the authoritative ``run_count`` — advanced here by EXACT delta, not
      re-estimated: a run counts as new iff it started strictly after the
      cached ``last_run_at``. A run can never be both newer than that and
      outside the newest-1000 sweep, so the arithmetic cannot miss one. The
      next full refresh overwrites it from ``run_count`` regardless, so any
      drift is self-healing rather than permanent.

    A session the sweep shows but the cache does not is NEW to this app, and
    surfacing it here is what satisfies "a session started from Telegram
    appears within one refresh" — its first run is in the sweep immediately,
    whether or not ``/api/sessions`` has been called since.
    """
    cached = store.all_meta()
    async with httpx.AsyncClient() as client:
        sweep = await fetch_sweep(client, base, token)
    by_session = _group_by_session(sweep)

    sessions = []
    for sid in set(cached) | set(by_session):
        meta = cached.get(sid) or {}
        runs = by_session.get(sid, [])
        known = bool(meta)
        baseline = float(meta.get("last_run_at") or 0.0)
        fresh = [r for r in runs if _epoch(r.get("started_at")) > baseline]

        if not known:
            # Never seen it: the sweep is all we have, and for a new session
            # it is also everything there is.
            count = len(runs)
            totals_ = totals(runs)
            last_at = max((_epoch(r.get("started_at")) for r in runs), default=0.0)
        elif fresh:
            delta = totals(fresh)
            count = int(meta.get("cached_run_count") or 0) + len(fresh)
            totals_ = {
                "cost_usd": round((meta.get("cost_usd") or 0.0) + delta["cost_usd"], 6),
                "tokens_in": (meta.get("tokens_in") or 0) + delta["tokens_in"],
                "tokens_out": (meta.get("tokens_out") or 0) + delta["tokens_out"],
            }
            last_at = max(_epoch(r.get("started_at")) for r in fresh)
        else:
            count = int(meta.get("cached_run_count") or 0)
            totals_ = {
                "cost_usd": meta.get("cost_usd") or 0.0,
                "tokens_in": meta.get("tokens_in") or 0,
                "tokens_out": meta.get("tokens_out") or 0,
            }
            last_at = baseline

        newest = (fresh or runs)[-1] if (fresh or runs) else {}
        # A synthetic CliSessionOut, so derive/shape_session stay one code
        # path across both refreshes rather than growing a fast-path variant.
        row = {
            "session_id": sid,
            "name": meta.get("name") or "",
            "run_count": count,
            "last_run_at": last_at,
            "updated_at": last_at,
            "last_status": newest.get("status") or meta.get("last_status"),
            "retired_at": meta.get("retired_at"),
            "retired_reason": meta.get("retired_reason"),
        }
        derived = derive(row, runs, meta, complete=False)
        if fresh or not known:
            # NEVER complete: the sweep is the newest 1000 runs platform-wide,
            # so for any session older than that window runs[0] is a
            # mid-session run, not the root. Writing a bucket or title from
            # that view is what mis-bucketed a Telegram session on 2026-11-14
            # — see _persist. The snapshot columns are still exact (they are
            # deltas, not root-derived), and the ≤60s full build fills in and
            # corrects the rest.
            _persist(store, sid, row, meta, derived, totals_, complete=False)
        sessions.append(shape_session(row, runs, derived, totals_, meta))

    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return {
        "sessions": sessions[:SESSION_LIMIT],
        "backfilled": 0,
        "full": False,
        "flagged_runs": flagged_runs(sweep),
    }


def _persist(
    store,
    sid: str,
    row: dict,
    meta: dict,
    derived: dict,
    totals_: dict,
    *,
    complete: bool,
) -> None:
    """Write back only what actually changed.

    Three groups, with different rules:

    * **derive-once** (agent, bucket, title, first run) — written ONLY from a
      ``complete`` view, i.e. one where ``runs[0]`` really is the session's
      root run. Caught live on 2026-11-14: a 486-run Telegram session
      ("planner sonnet") was bucketed **Agents** because the first request
      after install took the cheap path on a cold cache, where the sweep
      holds only that session's recent runs — so ``runs[0]`` was a mid-session
      ``wakeup`` rather than the ``telegram`` root, and "derive once, never
      recompute" then made the wrong answer permanent. That is the product
      constraint failing silently, which is exactly the thing this app exists
      to get right.
    * **the cost snapshot** — written whenever the run count it is keyed on
      has moved, because cost is not immutable.
    * **the /api/sessions mirror** (name, retired, last status/run) — always
      written, so the cheap refresh can read what only that endpoint knows
      without calling it. Writing the same value back is a harmless no-op
      upsert; the alternative is a staleness rule per column.

    A complete view OVERWRITES a derive-once value rather than only filling a
    blank one. That is what makes the bug above self-healing: a row poisoned
    by a partial view is corrected by the next full build (≤60s), with no
    migration and no manual repair. The anti-drift property still holds,
    because an incomplete view can no longer write these columns at all.
    """
    write: dict[str, Any] = {}
    if complete:
        for col, key in (
            ("agent_slug", "agent_slug"),
            ("bucket", "bucket"),
            ("title", "title"),
            ("first_run_at", "first_run_at"),
        ):
            if derived.get(key) and meta.get(col) != derived[key]:
                write[col] = derived[key]
        # Provenance, not a value: records that the root-derived columns above
        # came from a view holding the root run. build_rail reads it to decide
        # whether it may skip refetching a session — a cost snapshot being
        # valid says nothing about whether the bucket is. Only ever written
        # True; an incomplete view must not downgrade a trustworthy row.
        if not meta.get("derived_complete"):
            write["derived_complete"] = True
    want = int(row.get("run_count") or 0)
    if meta.get("cached_run_count") != want:
        write.update(totals_, cached_run_count=want)
    write.update(
        name=row.get("name") or "",
        retired_at=row.get("retired_at") or None,
        retired_reason=row.get("retired_reason") or None,
        last_status=row.get("last_status") or None,
        last_run_at=_epoch(row.get("last_run_at")),
    )
    try:
        store.upsert_meta(sid, **write)
    except Exception:  # noqa: BLE001 — a cache write must never fail a read
        log.warning("could not persist session_meta for %s", sid[:8], exc_info=True)


# -------------------------------------------------------------- thread view


def build_messages(runs: list[dict]) -> list[dict]:
    """One run becomes a user turn plus (once it answers) an agent turn.

    The agent turn carries that run's real ``cost_usd`` / token counts — the
    PO's "one keystroke to spend real money without showing it is a product
    defect" lands exactly here. An in-flight run still emits its user turn
    and a ``pending`` agent turn, which is what the UI renders as thinking
    and attaches the event stream to.
    """
    messages: list[dict] = []
    for r in runs:
        at = _epoch(r.get("started_at"))
        prompt = run_prompt(r)
        if prompt:
            messages.append(
                {
                    "role": "user",
                    "text": prompt,
                    "at": at,
                    "run_id": r.get("id"),
                }
            )
        status = r.get("status") or "pending"
        reply = run_reply(r)
        error = r.get("error")
        if status in TERMINAL_STATUSES and not reply and error:
            reply = f"The run ended as `{status}`: {error}"
        messages.append(
            {
                "role": "agent",
                "text": reply,
                "at": _epoch(r.get("ended_at")) or at,
                "run_id": r.get("id"),
                "status": status,
                "pending": status not in TERMINAL_STATUSES,
                "cost_usd": float(r.get("cost_usd") or 0),
                "tokens_in": int(r.get("tokens_in") or 0),
                "tokens_out": int(r.get("tokens_out") or 0),
                "model": r.get("model_slug") or None,
                "error": error or None,
            }
        )
    return messages


async def build_thread(base: str, token: str, store, session_id: str) -> dict:
    """A session's full transcript.

    Deliberately NOT ``summary=true``: that truncates ``input`` to ~200
    chars, which is right for a rail title and wrong for the thing the user
    came here to read.
    """
    async with httpx.AsyncClient() as client:
        row, runs = await asyncio.gather(
            ap_client.get_json(client, base, token, f"/api/sessions/{session_id}"),
            ap_client.get_json(
                client, base, token, "/api/runs", session_id=session_id, limit=SWEEP_LIMIT
            ),
        )
    runs = runs if isinstance(runs, list) else []
    runs.sort(key=lambda r: _epoch(r.get("started_at")))
    row = row if isinstance(row, dict) else {"session_id": session_id}

    meta = store.meta(session_id) or {}
    # This fetch is per-session and unsummarized, so it holds the root run
    # unless the session has more runs than the limit — same exact test as
    # the full rail build.
    complete = bool(runs) and len(runs) >= int(row.get("run_count") or 0)
    derived = derive(row, runs, meta, complete=complete)
    totals_ = totals(runs)
    _persist(store, session_id, row, meta, derived, totals_, complete=complete)

    session = shape_session(row, runs, derived, totals_, meta)
    session["messages"] = build_messages(runs)
    session["loaded"] = True
    # The run to stream, if this session is mid-turn.
    live = next(
        (r for r in reversed(runs) if (r.get("status") or "") not in TERMINAL_STATUSES), None
    )
    session["live_run_id"] = live.get("id") if live else None
    session["queued"] = shape_queue(store.queued(session_id))
    return {"session": session}


# ----------------------------------------------------------------- dispatch


async def dispatch(
    base: str, token: str, store, agent_slug: str, text: str, session_id: str | None = None
) -> dict:
    """Send a message. New session when ``session_id`` is None, continuation
    otherwise.

    The platform mints the session id, not us — ``run_sync``'s own comment
    block records why client-minted ids were reverted — so a new session's id
    is unknown until the CLI has started and the executor has persisted it on
    the run row. The response therefore carries ``run_id`` immediately
    (which is what lets the UI show in-flight state inside 2s) and
    ``session_id: null``; the caller polls ``run_status`` until it resolves.
    """
    body: dict[str, Any] = {"input": {"input": text}}
    if session_id:
        body["session_id"] = session_id
    async with httpx.AsyncClient() as client:
        data = await ap_client.post_json(client, base, token, f"/api/agents/{agent_slug}/run", body)
    run_id = (data or {}).get("run_id")
    if not run_id:
        raise ap_client.APError("the platform accepted the dispatch but returned no run_id")
    if not session_id:
        # Only a brand-new session needs the marker: it is what buckets this
        # session as the user's own once its id resolves.
        try:
            store.record_dispatch(run_id, agent_slug)
        except Exception:  # noqa: BLE001 — losing the marker costs a bucket, not the run
            log.warning("could not record dispatch %s", run_id, exc_info=True)
    return {"run_id": run_id, "session_id": session_id, "status": "pending"}


async def session_busy(base: str, token: str, session_id: str) -> bool:
    """Is a turn of this session still running (or waiting on its lock)?

    Asked of the platform, not inferred from local state: the turn may belong
    to Telegram or another agent, and only the run rows know.
    """
    async with httpx.AsyncClient() as client:
        runs = await ap_client.get_json(
            client,
            base,
            token,
            "/api/runs",
            session_id=session_id,
            summary="true",
            limit=10,
        )
    runs = runs if isinstance(runs, list) else []
    return any((r.get("status") or "pending") not in TERMINAL_STATUSES for r in runs)


async def send_or_queue(
    base: str, token: str, store, agent_slug: str, text: str, session_id: str
) -> dict:
    """Continue a session — now if it is idle, otherwise durably queue.

    A message is queued server-side whenever the session is mid-turn OR
    already has queued messages (so order is kept). The platform would also
    serialise a resume behind its per-session lock, but that lock has a
    30-minute safety valve that lets a waiter through unserialised, and it
    does not hand out in queue order; holding the message here until the
    session is genuinely idle avoids both. Without a database (standalone)
    there is nothing to hold it in, so it dispatches straight away and
    leaves ordering to the platform.
    """
    if store.db is None:
        return await dispatch(base, token, store, agent_slug, text, session_id=session_id)
    if not store.queued(session_id) and not await session_busy(base, token, session_id):
        return await dispatch(base, token, store, agent_slug, text, session_id=session_id)
    qid = store.enqueue(session_id, agent_slug, text)
    sent = await drain_session(base, token, store, session_id)
    if sent and sent["queue_id"] == qid:
        return {"run_id": sent["run_id"], "session_id": session_id, "status": "pending"}
    return {"queued": True, "queue_id": qid, "session_id": session_id, "status": "queued"}


async def drain_session(base: str, token: str, store, session_id: str) -> dict | None:
    """Dispatch the head of one session's queue if the session is idle.

    Returns ``{"queue_id", "run_id"}`` for what was sent, else None. One
    message per call: each is a turn of its own, and the next can only go once
    this one's run has ended and the session is idle again.
    """
    if not store.queued(session_id) or await session_busy(base, token, session_id):
        return None
    item = store.claim_head(session_id)
    if item is None:
        return None
    # The idle check above is a snapshot: another worker may have dispatched
    # the previous row between it and our claim. Ask again now that we hold
    # the claim, and give it back untouched if the session is busy.
    try:
        busy = await session_busy(base, token, session_id)
    except Exception:  # noqa: BLE001 — nothing sent yet, so give the claim back
        store.release_claim(item["id"])
        raise
    if busy:
        store.release_claim(item["id"])
        return None
    try:
        sent = await dispatch(
            base, token, store, item["agent_slug"], item["body"], session_id=session_id
        )
    except ap_client.APUnknownOutcome as exc:
        # The request may have created a run before we lost the answer.
        # Re-sending would risk a duplicate turn, so park it visibly.
        store.park_failed(item["id"], f"outcome unknown, not re-sent: {exc}")
        log.warning("queued dispatch %s outcome unknown: %s", item["id"][:8], exc)
        return None
    except ap_client.APError as exc:  # a clear refusal: nothing was created
        status = store.mark_failed_attempt(item["id"], str(exc))
        log.warning("queued dispatch %s refused (%s): %s", item["id"][:8], status, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — anything else is ambiguous too
        store.park_failed(item["id"], f"outcome unknown, not re-sent: {exc}")
        log.warning("queued dispatch %s failed unexpectedly: %s", item["id"][:8], exc)
        return None
    store.mark_sent(item["id"], sent["run_id"])
    return {"queue_id": item["id"], "run_id": sent["run_id"]}


async def drain_all(base: str, token: str, store) -> int:
    """One pass of the background drain: every session with something waiting.
    Returns how many messages were dispatched."""
    store.reap_stale_claims()
    sent = 0
    for sid in store.sessions_with_queue():
        try:
            if await drain_session(base, token, store, sid):
                sent += 1
        except (ap_client.APError, httpx.HTTPError) as exc:
            log.warning("queue drain skipped session %s: %s", sid[:8], exc)
    return sent


def shape_queue(items: list[dict]) -> list[dict]:
    """Queue rows as the thread view shows them."""
    return [
        {
            "queue_id": i["id"],
            "text": i["body"],
            "at": i["created_at"],
            "status": i["status"],
            "error": i.get("error") or None,
        }
        for i in items
    ]


async def run_status(base: str, token: str, store, run_id: str) -> dict:
    """Poll one run. Two jobs: resolve a new session's id, and be the
    belt-and-braces fallback that stops a wedged SSE stream from stranding
    the UI in "thinking" forever."""
    async with httpx.AsyncClient() as client:
        r = await ap_client.get_json(client, base, token, f"/api/runs/{run_id}")
    r = r if isinstance(r, dict) else {}
    session_id = r.get("session_id")
    status = r.get("status") or "pending"

    if session_id:
        d = store.dispatch(run_id)
        if d and not d.get("session_id"):
            try:
                store.resolve_dispatch(run_id, session_id)
            except Exception:  # noqa: BLE001
                log.warning("could not resolve dispatch %s", run_id, exc_info=True)

    return {
        "run_id": run_id,
        "session_id": session_id,
        "status": status,
        "done": status in TERMINAL_STATUSES,
        "text": run_reply(r),
        "error": r.get("error") or None,
        "cost_usd": float(r.get("cost_usd") or 0),
        "tokens_in": int(r.get("tokens_in") or 0),
        "tokens_out": int(r.get("tokens_out") or 0),
        "model": r.get("model_slug") or None,
        "started_at": _epoch(r.get("started_at")),
        "ended_at": _epoch(r.get("ended_at")),
        "agent": r.get("source_slug") or None,
    }


async def rename(base: str, token: str, session_id: str, name: str) -> dict:
    """Rename through the REAL ``PATCH /api/sessions/{session_id}`` — so the
    name is shared with Telegram and every other consumer, not shadowed in
    this app's own table."""
    async with httpx.AsyncClient() as client:
        row = await ap_client.patch_json(
            client, base, token, f"/api/sessions/{session_id}", {"name": name}
        )
    return {"title": (row or {}).get("name") or name}
