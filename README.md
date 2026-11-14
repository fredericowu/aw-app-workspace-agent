# aw-app-workspace-agent

A command-center view over this workspace's Agents Platform fleet — see
every agent that can be dispatched, which ones are running right now, hold
several agent conversations side by side, and (in a later card) answer the
questions agents are blocked on, without leaving the workspace.

Steps 1 and 2 of 3 are in: the app scaffold, the **Fleet** view, and
**Sessions**. The **System** tab is a separate card, blocked on an upstream
route, and is not wired yet.

## The one thing to know before reading the code

**The sessions this app shows are the same CLI sessions Telegram and every
agent dispatch create**, and **a message is a run** — your text is
`run.input`, the reply is `run.output.text`, and that run's own
`cost_usd`/token counts are what the turn cost. There is no chat store here
to keep in sync with anything.

That is a product constraint, not an implementation preference: a rail that
could not show the Telegram session you started an hour ago would defeat the
app's purpose. It is why `POST /api/playground/chat` is deliberately unused
(its history keys on `initiator_kind="chat"`, never creates a `CliSession`,
and cannot resume one), and it is what makes two things true that a chat UI
would not otherwise do:

- **Runs take 3-15 minutes**, so in-flight state is load-bearing.
- **A new session has no id until the CLI mints one**, so the first turn is
  keyed on its `run_id` and a second message is queued rather than
  dispatched — sending without the session id would fork a second session
  instead of continuing the one on screen.

`docs/sessions.md` is the full design note, including the measurements
behind the two-tier rail refresh.

## What's here

- **Tier-1, in-process app** (`aw-app.json`), no `ui:code` — the window is a
  declarative `iframe` (`windows/main.json`) pointing at this app's own
  route, which serves the frontend directly (`frontend/`).
- **Backend** (`workspace_agent_app/`):
  - `ap_client.py` — the HTTP primitives (reads, writes, the SSE
    passthrough), Fleet field-mapping, and the credential-resolution chain:
    this app's own config, falling back to the Agents Platform Runners app's
    saved config so installing this app needs no extra setup. The platform
    bearer never leaves the process.
  - `sessions.py` — the rail join, a session's thread, dispatch, and the run
    poll.
  - `store.py` — two app-owned tables (`db:own-tables`): derived session
    facts plus this app's own pin/archive state. No conversation is copied
    into them; the transcript is the platform's run log.
  - `routes.py` — `api/bootstrap`, `api/pulse`, `api/sessions*`,
    `api/runs/{id}` and the `api/runs/{id}/stream` SSE proxy.
- **Frontend** (`frontend/`): the finalized command-center UX-Proto
  prototype (vanilla JS), adapted to drop every mock fixture — no fabricated
  roster, no simulated notifications, no per-agent telemetry this build
  cannot honestly produce.

## Local development

```bash
pip install -r requirements-dev.txt httpx fastapi uvicorn
pytest tests/ -q
python -m workspace_agent_app   # standalone, binds 127.0.0.1:9401
```

Standalone mode reads credentials from `AW_AGENTS_PLATFORM_BASE` /
`AW_AGENTS_PLATFORM_TOKEN` (there is no workspace config store outside the
runtime). See the `aw-agents-platform-rest-api` skill for how to find a
platform base URL/token to test against.

## Scaffolded from aw-app-template

This repo was generated from
[`tekflox/aw-app-template`](https://github.com/tekflox/aw-app-template) —
see that repo's README and `skills/aw-create-app/SKILL.md` (also mirrored
here) for the app-authoring contract this repo follows: manifest shape,
capabilities, `contributes`, the declarative window widget vocabulary, and
how marketplace install/release works.
