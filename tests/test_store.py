"""Coverage for workspace_agent_app/store.py.

The facade (``ctx.db``) is faked here, so what is actually asserted is the
SQL this module *builds* — which is the part with real failure modes:

* **Prefix discipline.** ``db:own-tables`` enforces ``app__<slug>__`` on
  every name, so a table constant that drifts fails at activate, not at
  first use. Asserted directly on the constants.
* **Upsert, not read-modify-write.** Two workers can build the rail at the
  same moment. A SELECT-then-INSERT would lose a row; ``ON CONFLICT DO
  UPDATE`` makes the race a last-writer-wins on identical derived values.
* **Partial writes.** ``upsert_meta`` must touch only the columns it is
  handed — the whole derive-once/snapshot split in sessions.py depends on
  writing the cost snapshot without clobbering a cached title.
* **No-database mode.** Standalone (``python -m workspace_agent_app``) has
  no workspace Postgres; every method must degrade rather than throw.

Run: python -m pytest tests/test_store.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_agent_app import store as store_mod  # noqa: E402
from workspace_agent_app.store import APP_DISPATCH, SESSION_META, Store  # noqa: E402


class FakeDb:
    """Stand-in for ``ctx.db``, recording every call and returning canned
    rows for SELECTs."""

    def __init__(self, rows=None):
        self.created: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.rows = rows or {}

    def create(self, name, columns_sql):
        self.created.append((name, columns_sql))
        return name

    def execute(self, name, sql, params=None):
        self.calls.append((name, " ".join(sql.split()), params or {}))
        return self.rows.get(name, [])

    def last(self):
        return self.calls[-1]


def test_both_table_names_carry_the_enforced_app_prefix():
    """ctx.db validates this on every call — a drifted constant would break
    the app at activate() with a DbTableError, so pin it here instead."""
    prefix = "app__workspace-agent__"
    assert SESSION_META.startswith(prefix)
    assert APP_DISPATCH.startswith(prefix)


def test_ensure_tables_creates_both():
    db = FakeDb()
    Store(db).ensure_tables()
    assert [n for n, _ in db.created] == [SESSION_META, APP_DISPATCH]
    cols = dict(db.created)
    assert "session_id        TEXT PRIMARY KEY" in cols[SESSION_META]
    assert "run_id      TEXT PRIMARY KEY" in cols[APP_DISPATCH]


def test_ensure_tables_adds_columns_to_an_already_created_table():
    """REGRESSION, v0.3.1 -> v0.3.2. ctx.db.create is CREATE TABLE IF NOT
    EXISTS and the runtime never drops an app's tables (a version bump is
    uninstall+install, so dropping would wipe real data on every update). So
    on an UPGRADE the create is a no-op, and a column added in the new
    release would simply not exist — every read of it coming back as if the
    feature had never shipped. `derived_complete` hit exactly that, which is
    why the self-heal did not fire in the live app."""
    db = FakeDb()
    Store(db).ensure_tables()
    alters = [sql for name, sql, _ in db.calls if sql.startswith("ALTER TABLE")]

    # Every non-PK column is covered, idempotently.
    assert any("ADD COLUMN IF NOT EXISTS derived_complete" in a for a in alters)
    assert any("ADD COLUMN IF NOT EXISTS cached_run_count" in a for a in alters)
    assert any("ADD COLUMN IF NOT EXISTS session_id" in a for a in alters)  # app_dispatch's
    assert all("IF NOT EXISTS" in a for a in alters)

    # A primary key cannot be added this way, so those lines are skipped.
    assert not any("PRIMARY KEY" in a for a in alters)

    covered = {a.split("ADD COLUMN IF NOT EXISTS ")[1].split()[0] for a in alters}
    assert set(store_mod._META_FIELDS) <= covered


def test_all_meta_maps_rows_onto_column_names():
    row = (
        "s1",
        "architect",
        "user",
        "A title",
        "manager",
        "app",
        1.0,
        True,
        False,
        2.5,
        100,
        20,
        3,
    )
    st = Store(FakeDb({SESSION_META: [row]}))
    meta = st.all_meta()
    assert meta["s1"]["agent_slug"] == "architect"
    assert meta["s1"]["bucket"] == "user"
    assert meta["s1"]["title"] == "A title"
    assert meta["s1"]["pinned"] is True
    assert meta["s1"]["cached_run_count"] == 3
    assert st.meta("s1")["origin"] == "manager"
    assert st.meta("nope") is None


def test_upsert_meta_writes_only_the_columns_it_was_given():
    """sessions.py's whole cache strategy rests on this: writing the cost
    snapshot must not clobber a cached title, and vice versa."""
    db = FakeDb()
    Store(db).upsert_meta("s1", cost_usd=1.5, cached_run_count=2)
    name, sql, params = db.last()
    assert name == SESSION_META
    assert sql.startswith("INSERT INTO {table} (session_id, cost_usd, cached_run_count)")
    assert "ON CONFLICT (session_id) DO UPDATE SET" in sql
    assert "cost_usd = EXCLUDED.cost_usd" in sql
    assert "cached_run_count = EXCLUDED.cached_run_count" in sql
    assert "title" not in sql
    assert params == {"session_id": "s1", "cost_usd": 1.5, "cached_run_count": 2}


def test_upsert_meta_refuses_an_unknown_column():
    """The column list is interpolated into the SQL, so an unvalidated name
    would be an injection point as well as a typo."""
    db = FakeDb()
    with pytest.raises(ValueError, match="unknown session_meta column"):
        Store(db).upsert_meta("s1", title="ok", nonsense=1)
    assert db.calls == []  # nothing partially written


def test_upsert_meta_with_no_fields_is_a_no_op():
    db = FakeDb()
    Store(db).upsert_meta("s1")
    assert db.calls == []


def test_set_flag_creates_the_row_if_the_rail_never_derived_it():
    """Pinning a session the rail has not reached yet must work — the flag is
    app-local, so there is nothing upstream to wait for."""
    db = FakeDb()
    assert Store(db).set_flag("s1", "pinned", True) is True
    _, sql, params = db.last()
    assert "pinned = EXCLUDED.pinned" in sql
    assert params["pinned"] is True


def test_set_flag_refuses_anything_but_pin_and_archive():
    with pytest.raises(ValueError, match="not a toggleable column"):
        Store(FakeDb()).set_flag("s1", "title", True)


def test_record_dispatch_is_idempotent():
    """The marker is written once per new session, but a retried POST must
    not overwrite a session id the poll has already resolved."""
    db = FakeDb()
    Store(db).record_dispatch("r1", "architect")
    name, sql, params = db.last()
    assert name == APP_DISPATCH
    assert "ON CONFLICT (run_id) DO NOTHING" in sql
    assert params == {"run_id": "r1", "agent_slug": "architect"}


def test_dispatch_reads_back_a_recorded_run():
    st = Store(FakeDb({APP_DISPATCH: [("r1", "architect", None)]}))
    assert st.dispatch("r1") == {"run_id": "r1", "agent_slug": "architect", "session_id": None}


def test_dispatch_returns_none_when_unknown():
    assert Store(FakeDb({APP_DISPATCH: []})).dispatch("r1") is None


def test_resolve_dispatch_writes_both_tables():
    """One call has to do two jobs: make the poll idempotent, and record that
    THIS app started the session so the rail buckets it as the user's own
    rather than running it through the initiator-kind heuristic."""
    db = FakeDb()
    Store(db).resolve_dispatch("r1", "sess-9")
    dispatch_call = next(c for c in db.calls if c[0] == APP_DISPATCH)
    meta_call = next(c for c in db.calls if c[0] == SESSION_META)
    assert "UPDATE {table} SET session_id = :session_id" in dispatch_call[1]
    assert dispatch_call[2] == {"run_id": "r1", "session_id": "sess-9"}
    assert meta_call[2] == {"session_id": "sess-9", "bucket": "user", "created_from": "app"}


def test_every_method_degrades_without_a_database():
    """Standalone mode has no workspace Postgres. Pin/archive stop persisting
    and the rail derives fresh every time — nothing throws."""
    st = Store(None)
    st.ensure_tables()
    assert st.all_meta() == {}
    assert st.meta("s1") is None
    st.upsert_meta("s1", title="x")
    assert st.set_flag("s1", "pinned", True) is True
    st.record_dispatch("r1", "a")
    assert st.dispatch("r1") is None
    st.resolve_dispatch("r1", "s1")


def test_the_column_list_matches_the_ddl():
    """_META_FIELDS drives both the SELECT projection and the upsert, so a
    column added to the DDL without being added there is silently unreadable."""
    ddl_names = {
        line.split()[0]
        for line in store_mod._SESSION_META_COLUMNS.strip().splitlines()
        if line.strip()
    }
    assert ddl_names == {"session_id", *store_mod._META_FIELDS}
