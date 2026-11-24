"""App-local state for the Sessions surface — two workspace tables behind
``ctx.db`` (capability ``db:own-tables``, so every name carries the enforced
``app__workspace-agent__`` prefix).

Why a table and not a dict: the workspace runs N uvicorn worker processes
(``AW_WORKSPACE_WORKERS``), and nothing routes a given browser to a given
worker. A dispatch recorded in one worker's memory is invisible to the
worker that later polls the run, so every fact the UI depends on across two
requests has to be in the database. The tables ARE the cache.

``session_meta`` — one row per CLI session this app has seen.

Four kinds of column live here, and the distinctions are the whole point:

* **Root-derived**: ``agent_slug``, ``bucket``, ``title``, ``origin``,
  ``first_run_at``. These come from a session's FIRST run, so they are only
  valid when written from a view that actually held that run — see
  ``sessions.derive``/``_persist`` and the ``complete`` flag. Caching them is
  what keeps the rail off an N+1 per-session fetch.
* **Provenance**: ``derived_complete``. Whether the columns above came from
  such a view. ``sessions.build_rail`` refuses to skip refetching a session
  until this is true, which is what makes a bad value repairable instead of
  permanent.
* **Snapshot**: ``cost_usd``, ``tokens_in``, ``tokens_out``, keyed on
  ``cached_run_count``; plus ``name``/``retired_at``/``retired_reason``/
  ``last_status``/``last_run_at`` mirrored from ``GET /api/sessions`` so the
  cheap refresh can skip that call. A session's cost is NOT immutable (every
  new turn adds to it), so the cost columns are trusted only while the
  session's upstream ``run_count`` still matches the count they were
  computed at.
* **App-local state**: ``pinned`` / ``archived``. Neither exists upstream —
  ``CliSessionUpdate`` accepts only name/description, and ``retired_at`` is
  the executor's own death marker, not a user-facing archive. Rename, by
  contrast, is NOT here: it goes to the real ``PATCH /api/sessions/{id}`` so
  the name is shared with Telegram and every other consumer.

A cost snapshot being valid says nothing about whether the root-derived
columns are, which is why those two groups are tracked separately rather
than behind one "is this row fresh" flag.

``app_queue`` — messages sent to a session whose turn was still running. The
queue is server-side and durable on purpose: it used to live in the browser's
memory, which meant the agent saw nothing until the whole turn ended and a
reload lost the message silently. ``sessions.drain_*`` dispatches the head of
each session's queue once that session is idle, whether or not any tab is open.

``app_dispatch`` — one row per run THIS app dispatched, so a session the user
started here can be bucketed as "User" rather than falling through the
initiator-kind heuristic to "Agents". ``session_id`` starts NULL (the CLI
mints it, the platform persists it on the run row some seconds later) and is
backfilled by the run poll.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

log = logging.getLogger("aw_apps.workspace_agent.store")

SESSION_META = "app__workspace-agent__session_meta"
APP_DISPATCH = "app__workspace-agent__app_dispatch"
APP_QUEUE = "app__workspace-agent__app_queue"

#: A message that fails to dispatch this many times is parked as ``failed``
#: (visible and dismissable in the thread) instead of retrying forever.
MAX_ATTEMPTS = 3

#: A claim older than this is a worker that died between claiming and
#: recording the outcome. The dispatch may or may not have reached the
#: platform, so it is parked as ``failed`` rather than re-sent: at-most-once
#: beats a duplicate turn the user did not ask for.
CLAIM_STALE_S = 120.0

#: Statuses the thread shows: waiting, being sent right now, or failed.
_OPEN_STATUSES = ("queued", "sending", "failed")

_SESSION_META_COLUMNS = """
    session_id        TEXT PRIMARY KEY,
    agent_slug        TEXT,
    bucket            TEXT,
    title             TEXT,
    origin            TEXT,
    created_from      TEXT,
    first_run_at      DOUBLE PRECISION,
    pinned            BOOLEAN NOT NULL DEFAULT FALSE,
    archived          BOOLEAN NOT NULL DEFAULT FALSE,
    cost_usd          DOUBLE PRECISION,
    tokens_in         BIGINT,
    tokens_out        BIGINT,
    cached_run_count  INTEGER,
    name              TEXT,
    retired_at        TEXT,
    retired_reason    TEXT,
    last_status       TEXT,
    last_run_at       DOUBLE PRECISION,
    derived_complete  BOOLEAN NOT NULL DEFAULT FALSE
"""

_APP_DISPATCH_COLUMNS = """
    run_id      TEXT PRIMARY KEY,
    agent_slug  TEXT NOT NULL,
    session_id  TEXT
"""

_APP_QUEUE_COLUMNS = """
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    agent_slug  TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    claim       TEXT,
    claimed_at  DOUBLE PRECISION,
    run_id      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT
"""

_QUEUE_FIELDS = (
    "id",
    "session_id",
    "agent_slug",
    "body",
    "created_at",
    "status",
    "run_id",
    "attempts",
    "error",
)

_META_FIELDS = (
    "agent_slug",
    "bucket",
    "title",
    "origin",
    "created_from",
    "first_run_at",
    "pinned",
    "archived",
    "cost_usd",
    "tokens_in",
    "tokens_out",
    "cached_run_count",
    # Snapshot of what only GET /api/sessions can tell us, so the ~10s
    # refresh can skip that call entirely — see sessions.build_rail_fast.
    "name",
    "retired_at",
    "retired_reason",
    "last_status",
    "last_run_at",
    # Provenance for the root-derived columns above — see sessions._persist.
    # build_rail will not skip refetching a session until this is true.
    "derived_complete",
)


class Store:
    """Thin wrapper over ``ctx.db``. Every method is a no-op returning empty
    when ``db`` is None — that is the standalone/dev mode (``__main__.py``),
    where there is no workspace database to talk to. The Sessions surface
    degrades to "nothing pinned, nothing archived, derive every time" rather
    than failing, which is exactly what a dev run wants."""

    def __init__(self, db: Any | None) -> None:
        self.db = db

    def ensure_tables(self) -> None:
        """Create both tables, then bring an already-created one up to the
        current column list.

        The second half is load-bearing, not belt-and-braces. ``ctx.db.create``
        is ``CREATE TABLE IF NOT EXISTS``, and the runtime deliberately never
        drops an app's tables (a plain version bump is uninstall+install, so
        dropping would wipe real data on every routine update). So on an
        UPGRADE the DDL above is a no-op and a column added in this release
        would simply not exist — every read of it returning as if the feature
        had never shipped. ``derived_complete`` hit exactly that on the
        v0.3.1 -> v0.3.2 upgrade.

        Additive ``ADD COLUMN IF NOT EXISTS`` rather than a ``migrations/``
        file because these tables are a cache this app wholly owns: the
        statements are idempotent, they run on every activate, and there is
        no ordering or data-rewriting step for a migration to express.
        Anything that is NOT purely additive (a type change, a backfill, a
        drop) does belong in ``migrations/``.
        """
        if self.db is None:
            return
        self.db.create(SESSION_META, _SESSION_META_COLUMNS)
        self.db.create(APP_DISPATCH, _APP_DISPATCH_COLUMNS)
        self.db.create(APP_QUEUE, _APP_QUEUE_COLUMNS)
        for name, columns_sql in (
            (SESSION_META, _SESSION_META_COLUMNS),
            (APP_DISPATCH, _APP_DISPATCH_COLUMNS),
            (APP_QUEUE, _APP_QUEUE_COLUMNS),
        ):
            for line in columns_sql.strip().splitlines():
                col = line.strip().rstrip(",")
                if not col or "PRIMARY KEY" in col:
                    continue
                self.db.execute(name, f"ALTER TABLE {{table}} ADD COLUMN IF NOT EXISTS {col}")

    # ---- session_meta -------------------------------------------------

    def all_meta(self) -> dict[str, dict]:
        """Every known session's meta row, keyed by session_id."""
        if self.db is None:
            return {}
        cols = ", ".join(("session_id",) + _META_FIELDS)
        rows = self.db.execute(SESSION_META, f"SELECT {cols} FROM {{table}}")
        return {r[0]: dict(zip(_META_FIELDS, r[1:])) for r in rows}

    def meta(self, session_id: str) -> dict | None:
        return self.all_meta().get(session_id)

    def upsert_meta(self, session_id: str, **fields) -> None:
        """Insert or update only the columns named in ``fields``.

        ``ON CONFLICT DO UPDATE`` rather than a read-modify-write: two
        workers can be building the rail at the same moment, and an upsert
        makes that a last-writer-wins on identical derived values instead of
        a lost row.
        """
        if self.db is None or not fields:
            return
        bad = set(fields) - set(_META_FIELDS)
        if bad:
            raise ValueError(f"unknown session_meta column(s): {sorted(bad)}")
        names = list(fields)
        cols = ", ".join(["session_id"] + names)
        placeholders = ", ".join([":session_id"] + [f":{n}" for n in names])
        updates = ", ".join(f"{n} = EXCLUDED.{n}" for n in names)
        self.db.execute(
            SESSION_META,
            f"INSERT INTO {{table}} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT (session_id) DO UPDATE SET {updates}",
            {"session_id": session_id, **fields},
        )

    def set_flag(self, session_id: str, column: str, value: bool) -> bool:
        """Toggle ``pinned``/``archived``. Creates the row if the user pins a
        session the rail has not derived yet."""
        if column not in ("pinned", "archived"):
            raise ValueError(f"not a toggleable column: {column!r}")
        self.upsert_meta(session_id, **{column: value})
        return value

    # ---- app_dispatch -------------------------------------------------

    def record_dispatch(self, run_id: str, agent_slug: str) -> None:
        if self.db is None:
            return
        self.db.execute(
            APP_DISPATCH,
            "INSERT INTO {table} (run_id, agent_slug) VALUES (:run_id, :agent_slug) "
            "ON CONFLICT (run_id) DO NOTHING",
            {"run_id": run_id, "agent_slug": agent_slug},
        )

    def dispatch(self, run_id: str) -> dict | None:
        if self.db is None:
            return None
        rows = self.db.execute(
            APP_DISPATCH,
            "SELECT run_id, agent_slug, session_id FROM {table} WHERE run_id = :run_id",
            {"run_id": run_id},
        )
        if not rows:
            return None
        r = rows[0]
        return {"run_id": r[0], "agent_slug": r[1], "session_id": r[2]}

    def resolve_dispatch(self, run_id: str, session_id: str) -> None:
        """A run this app dispatched has been given its session id. Record it
        on both tables: ``app_dispatch`` so a re-poll is idempotent, and
        ``session_meta`` so the rail buckets this session as the user's own
        rather than running it through the initiator-kind heuristic (which
        cannot tell an app-started session from any other API dispatch)."""
        if self.db is None:
            return
        self.db.execute(
            APP_DISPATCH,
            "UPDATE {table} SET session_id = :session_id WHERE run_id = :run_id",
            {"run_id": run_id, "session_id": session_id},
        )
        self.upsert_meta(session_id, bucket="user", created_from="app")

    # ---- app_queue ----------------------------------------------------

    def enqueue(self, session_id: str, agent_slug: str, body: str) -> str:
        """Durably hold a message for a session that is mid-turn."""
        qid = uuid.uuid4().hex
        if self.db is None:
            return qid
        self.db.execute(
            APP_QUEUE,
            "INSERT INTO {table} (id, session_id, agent_slug, body, created_at) "
            "VALUES (:id, :session_id, :agent_slug, :body, :created_at)",
            {
                "id": qid,
                "session_id": session_id,
                "agent_slug": agent_slug,
                "body": body,
                "created_at": time.time(),
            },
        )
        return qid

    def queued(self, session_id: str) -> list[dict]:
        """A session's waiting / sending / failed messages, oldest first."""
        if self.db is None:
            return []
        cols = ", ".join(_QUEUE_FIELDS)
        rows = self.db.execute(
            APP_QUEUE,
            f"SELECT {cols} FROM {{table}} WHERE session_id = :session_id "
            "AND status IN ('queued', 'sending', 'failed') ORDER BY created_at, id",
            {"session_id": session_id},
        )
        return [dict(zip(_QUEUE_FIELDS, r)) for r in rows]

    def sessions_with_queue(self) -> list[str]:
        """Sessions that have something waiting to be dispatched."""
        if self.db is None:
            return []
        rows = self.db.execute(
            APP_QUEUE,
            "SELECT DISTINCT session_id FROM {table} WHERE status IN ('queued', 'sending')",
        )
        return [r[0] for r in rows]

    def claim_head(self, session_id: str) -> dict | None:
        """Atomically take the oldest waiting message, or None.

        None when nothing is waiting, or when another worker already holds a
        claim on this session (``sending``) — one message per session at a
        time, which is what keeps order and prevents a double dispatch when
        several workers run the drain loop. The claim is a conditional UPDATE
        followed by a read of who won, because the facade does not report an
        affected-row count.
        """
        if self.db is None:
            return None
        items = self.queued(session_id)
        if not items or any(i["status"] == "sending" for i in items):
            return None
        head = next((i for i in items if i["status"] == "queued"), None)
        if head is None:
            return None
        token = uuid.uuid4().hex
        self.db.execute(
            APP_QUEUE,
            "UPDATE {table} SET status = 'sending', claim = :claim, claimed_at = :now "
            "WHERE id = :id AND status = 'queued'",
            {"claim": token, "now": time.time(), "id": head["id"]},
        )
        rows = self.db.execute(
            APP_QUEUE,
            "SELECT claim FROM {table} WHERE id = :id",
            {"id": head["id"]},
        )
        if not rows or rows[0][0] != token:
            return None
        return head

    def mark_sent(self, qid: str, run_id: str) -> None:
        if self.db is None:
            return
        self.db.execute(
            APP_QUEUE,
            "UPDATE {table} SET status = 'sent', run_id = :run_id WHERE id = :id",
            {"id": qid, "run_id": run_id},
        )

    def mark_failed_attempt(self, qid: str, error: str) -> str:
        """A dispatch failed. Back to ``queued`` for another try, or parked as
        ``failed`` once MAX_ATTEMPTS is reached. Returns the new status."""
        if self.db is None:
            return "failed"
        rows = self.db.execute(
            APP_QUEUE, "SELECT attempts FROM {table} WHERE id = :id", {"id": qid}
        )
        attempts = int(rows[0][0] or 0) + 1 if rows else MAX_ATTEMPTS
        status = "failed" if attempts >= MAX_ATTEMPTS else "queued"
        self.db.execute(
            APP_QUEUE,
            "UPDATE {table} SET status = :status, attempts = :attempts, error = :error, "
            "claim = NULL WHERE id = :id",
            {"id": qid, "status": status, "attempts": attempts, "error": error[:500]},
        )
        return status

    def reap_stale_claims(self) -> None:
        """Park claims whose worker died before recording an outcome."""
        if self.db is None:
            return
        self.db.execute(
            APP_QUEUE,
            "UPDATE {table} SET status = 'failed', "
            "error = 'worker stopped mid-dispatch; not re-sent to avoid a duplicate turn' "
            "WHERE status = 'sending' AND claimed_at < :cutoff",
            {"cutoff": time.time() - CLAIM_STALE_S},
        )

    def cancel(self, session_id: str, qid: str) -> bool:
        """Drop a waiting (or failed) message. False if it already went out."""
        if self.db is None:
            return False
        self.db.execute(
            APP_QUEUE,
            "UPDATE {table} SET status = 'cancelled' WHERE id = :id "
            "AND session_id = :session_id AND status IN ('queued', 'failed')",
            {"id": qid, "session_id": session_id},
        )
        rows = self.db.execute(APP_QUEUE, "SELECT status FROM {table} WHERE id = :id", {"id": qid})
        return bool(rows) and rows[0][0] == "cancelled"
