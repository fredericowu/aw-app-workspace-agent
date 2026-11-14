# aw-app-workspace-agent

A command-center view over this workspace's Agents Platform fleet — see
every agent that can be dispatched, which ones are running right now, and
(in later cards) hold sessions and answer agent questions, without leaving
the workspace.

This is step 1 of 3: the app scaffold and the **Fleet** view, wired to the
live platform. Sessions and System are separate cards and are not wired yet.

## What's here

- **Tier-1, in-process app** (`aw-app.json`), no `ui:code` — the window is a
  declarative `iframe` (`windows/main.json`) pointing at this app's own
  route, which serves the frontend directly (`frontend/`).
- **Backend** (`workspace_agent_app/`): `GET api/bootstrap` aggregates
  `/api/agents` + `/api/agent-groups` + `/api/models` + `/api/runs` from the
  real Agents Platform REST API and reshapes them for the frontend;
  `GET api/pulse` is a cheap periodic poll of running-agent counts.
  `ap_client.py` holds the field-mapping and the credential-resolution
  chain: this app's own config, falling back to the Agents Platform Runners
  app's saved config so installing this app needs no extra setup.
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
