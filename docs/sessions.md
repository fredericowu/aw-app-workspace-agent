# Sessions — design note

How `aw-app-workspace-agent` shows, threads and continues real CLI sessions.
Written from the shipped code; every number here was measured against the
live platform on 2026-11-14, not estimated.

## The constraint everything follows from

The sessions this app shows **are** the CLI sessions Telegram and every
agent dispatch create. Not a mirror of them, not a parallel store keyed to
them — the same `CliSession` rows, reached through `GET /api/sessions` and
`GET /api/runs`.

`POST /api/playground/chat` exists and would have been easier: it threads
its own history and returns a `run_id`. It is deliberately unused, because
it keys on `initiator_kind="chat"` + `initiator_id`, never creates a
`CliSession`, and so can neither appear in `/api/sessions` nor resume a
session Telegram started. A hybrid (playground for app-born chats, real
sessions for the rail) fails the same way one step removed: it creates a
store whose sessions Telegram can never resume.

**A message is a run.** `run.input.input` is what was said,
`run.output.text` is the reply, `tokens_in`/`tokens_out`/`cost_usd` on the
same row are what the turn cost, and `GET /api/runs/{id}/events` is its tool
trace. Nothing about a conversation is stored by this app.

## The rail, without an N+1

A naive rail is one `GET /api/runs?session_id=…` per listed session. With
100 sessions that is 100 round trips per refresh.

Instead: one `GET /api/sessions?limit=100` plus **one**
`GET /api/runs?summary=true&limit=1000`, grouped by `session_id` in the app
backend. Whether that sweep suffices for a given session is an **exact
test**, not a guess — `CliSessionOut.run_count` is authoritative, so a
session is fully covered iff the sweep yielded that many runs for it.
Anything short falls back to one call for that session alone.

Derived facts land in `session_meta` (see `store.py`) and split three ways:

| Group | Columns | Rule |
|---|---|---|
| Root-derived | `bucket`, `title`, `first_run_at`, `agent_slug` | Only valid from a **complete** view of the session's runs — see below. Written only from one, and a complete view always wins over a value an earlier partial one wrote. |
| Snapshot | `cost_usd`, `tokens_in`, `tokens_out` | Cost is **not** immutable. Keyed on the `cached_run_count` it was computed at; the moment upstream's `run_count` moves, it is recomputed. |
| App-local | `pinned`, `archived` | Neither exists upstream. `CliSessionUpdate` accepts only name/description, and `retired_at` is the executor's own death marker, not a user action. |

### The completeness precondition — the one that already bit

Every fact taken from a session's **first** run (its bucket, its fallback
title, when it started) is only valid if `runs[0]` really *is* that first
run. The sweep is the newest 1000 runs **platform-wide**, so for any session
older than that window `runs[0]` is a mid-session run, not the root.

`run_count` makes this an exact test rather than a judgement call: the view
is complete iff we hold at least that many runs. `derive()` and `_persist()`
both take a `complete` flag, and an incomplete view may not write these
columns at all.

**This shipped wrong once, in v0.3.0.** The original rule was "derive once,
never recompute", which sounds conservative and is wrong in the direction
that matters: it let the *first* view win even when that view could not see
the root, then locked the mistake in forever. The first request after install
took the cheap path on a cold cache, and a 486-run Telegram session
(`planner sonnet`, root `initiator_kind: telegram`) was filed under
**Agents** — the product constraint failing silently, with no error anywhere.
Unit tests passed throughout; only running the installed app against real
data showed it.

The rule is now about the *view*, not the ordering: an authoritative view may
always correct, a partial one may never write. That also makes the fix
self-healing — rows poisoned by v0.3.0 are corrected by the next full build,
with no migration and no manual repair.

Two things ignore the flag on purpose: `created_from == "app"` always buckets
as the user's (an app-local fact no run can report, and the initiator-kind
heuristic cannot tell an app dispatch from any other API dispatch), and the
real `CliSession.name` always wins as the title.

Rename is **not** app-local: it goes through the real
`PATCH /api/sessions/{session_id}`, so the name is shared with Telegram and
every other consumer.

### Why the refresh is two-tier

Measured, this workspace, 2026-11-14:

| Call | Time |
|---|---|
| `GET /api/sessions?limit=100` | **4.75s** |
| `GET /api/sessions?limit=50` | 2.67s |
| `GET /api/sessions?limit=10` | 0.53s |
| `GET /api/runs?summary=true&limit=1000` | 0.40s |

`/api/sessions` costs ~48ms per row, because it runs two queries per row
upstream. So the prescribed two-call rail is ~5s, and polling it every 10s
would put a **~50% duty cycle** on an endpoint shared with every other
consumer of the platform.

Shrinking `limit` is not the fix. `/api/sessions` orders by
`CliSession.updated_at`, which is `onupdate=_now` — it moves when the
session **row** is written, and a new run does not touch it. Measured on the
same data, **120 of 498** adjacent pairs are already inverted against
`last_run_at`, so a smaller window would start dropping *active* sessions
rather than only inactive ones. (It is good enough as a window: all 25
sessions with a run in the last hour were inside the 100-row cut. The app
re-sorts the result by `last_run_at`, so what the user sees is ordered by
activity even though the window was not chosen that way.)

So:

- **Full build** (`?full=1`) — `/api/sessions` + the sweep. First paint, then
  once a minute. ~5-6s.
- **Cheap refresh** (default) — the sweep **only**, joined against
  `session_meta`. Every 10s. **0.5s measured**, a 12x reduction.

The cheap path advances `run_count`/cost by **exact delta**: a run counts as
new iff it started strictly after the cached `last_run_at`, and a run can
never be both newer than that and outside the newest-1000 sweep. Any drift
is overwritten by the next full build from the authoritative `run_count`, so
the arithmetic is self-healing rather than load-bearing.

A session the sweep shows but the cache does not is new to this app, and
surfacing it from the sweep alone is what satisfies *"a session started from
Telegram appears within one refresh"* — its first run is in the sweep
immediately, whether or not `/api/sessions` has been called since. What the
sweep cannot give is the upstream `name` and `retired_at`; those are
mirrored into `session_meta` by each full build, and a never-before-seen
session renders with its derived title until the next one. For a brand-new
session that is also the truth — `CliSession.name` starts empty.

## User vs Agents

Decided from the session's **root** run:

1. Started by this app (recorded in `session_meta` at dispatch) → **User**.
2. `parent_run_id` set → **Agents**, whatever started it.
3. `initiator_kind` or `source_device` in `telegram` / `watch` → **User**.
4. Anything else (`agent_run`, `wakeup`, `workflow_run`, `mcp`,
   `telegram_system`, `plan_cadence`, …) → **Agents**.

Rule 1 matters because the initiator-kind heuristic cannot tell an
app-started session from any other API dispatch. Rule 3 consults
`source_device` as well as `initiator_kind` on purpose: the rule is pinned
to today's initiator vocabulary, and a second independent signal for the
same fact makes an upstream rename degrade instead of silently
reclassifying every Telegram session.

Known edge, accepted for v1: an AP-UI quick-test session buckets as Agents.

## Sending

`POST /api/agents/{slug}/run` — no `session_id` for a new session,
`session_id` set to continue. That is exactly how the Telegram agent resumes
a session. The endpoint returns `run_id` immediately, which is what lets the
UI show in-flight state inside the 2s acceptance criterion; the 3-15 minutes
that follow block nothing.

Three consequences the UI has to handle, all of them normal rather than
edge cases:

- **The platform mints the session id, not us** (`run_sync`'s own comment
  block records why client-minted ids were reverted). So a new session's id
  is unknown for the first few seconds. The tab is keyed on `run:<run_id>`
  until the run poll resolves it, and a second message is **queued**, not
  dispatched — sending with no `session_id` would fork a second session.
- **A draft is not a session.** A CLI session only exists once a run has
  created one, so picking an agent without typing a kickoff message opens a
  client-only draft tab; the first send is what makes it real. Drafts are
  never persisted, so closing the tab discards one.
- **Not every agent leaves a resumable session.** Only a coding-CLI model
  mints a `CliSession`. Measured: **84 of 92** live agents are `provider:
  runner`; the other 8 (openai, echo, fake, direct anthropic) answer over a
  plain API call and leave nothing to resume. The Fleet gallery offers all
  of them, so `shape_models` carries a `resumable` flag: the picker warns
  before you choose one, and if a first run finishes with no `session_id` the
  session is marked un-continuable, the queue is released rather than
  waiting forever, and each later message dispatches a standalone run.

## Streaming

The open run streams through an app-backend passthrough of
`GET /api/runs/{id}/stream`, with `read=None` (an agent can go minutes
between events) and no buffering. Reconnect logic is deliberately absent:
the upstream route replays every persisted event from the DB on each connect
and closes a finished run with a `done` event, so `EventSource`'s native
auto-reconnect is already lossless across an app restart, a platform deploy
or a sleeping laptop.

`aiter_bytes`, not `aiter_raw` — httpx offers `Accept-Encoding: gzip` by
default, and raw bytes would be compressed bytes forwarded under a
`text/event-stream` header with no `Content-Encoding` to explain them.

Belt and braces: while a run is in flight the client **also** polls the run
row every 15s. A stream that wedges is indistinguishable from an agent still
thinking, and the failure mode is a UI stuck on "thinking" forever. Stream
and poll race into the same idempotent `finishRun`.

The reply text and the real cost are read off the **run row**, not
accumulated from the stream — one authoritative read instead of two
half-truths.

## Cost

Per-turn cost and token counts render on every finished agent turn, and the
session total in the thread header. A zero-cost turn shows `$0` rather than
hiding the line: a visible zero is information, while an absent line makes a
free turn and an expensive one look identical. Sub-cent costs are never
rounded to `$0.00`.

## What this makes harder later

- `session_meta` is a second store. If the platform grows native archive
  (`retired_at` already exists on the row) or richer session metadata, a
  merge/migration is owed.
- The bucketing rule bakes in today's initiator-kind vocabulary. The
  `source_device` cross-check softens a rename but does not remove the
  coupling.
- iframe mode closes the door on SPA slot integration (e.g. a nav badge for
  pending questions); that would need `ui:code`/component-mode rework.
- One-user scale is assumed throughout — poll cadences, and one SSE
  connection per in-flight session inside the core process.
