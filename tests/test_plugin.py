"""Unit tests for workspace_agent_app/plugin.py's WorkspaceAgentPlugin —
activate()/deactivate(), and the config-resolver closure it hands to
build_routes() (so a settings change takes effect with no restart).

`ctx` is a lightweight double, not the real F4 runtime — activate() only
touches `ctx.routes`, `ctx.secrets` and `ctx.config`.

Run: python -m pytest tests/test_plugin.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace_agent_app import ap_client  # noqa: E402
from workspace_agent_app.plugin import WorkspaceAgentPlugin  # noqa: E402


def _make_ctx(config=None, secret_token=None):
    ctx = MagicMock()
    ctx.config = config or {}
    ctx.secrets.read.return_value = secret_token
    return ctx


def test_activate_registers_routes_exactly_once():
    ctx = _make_ctx()
    asyncio.run(WorkspaceAgentPlugin().activate(ctx))
    ctx.routes.register.assert_called_once()


def test_resolver_prefers_own_config_over_the_inherited_file():
    ctx = _make_ctx({"agents_platform_base": "http://own"}, secret_token="own-tok")
    asyncio.run(WorkspaceAgentPlugin().activate(ctx))

    # ctx.routes.register() receives the FastAPI app build_routes() built
    # around the resolver closure — exercise the closure indirectly through
    # the route that calls it rather than reaching into plugin internals.
    app = ctx.routes.register.call_args[0][0]
    from fastapi.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/api/bootstrap")
    # Own config was fully set, so the resolver did not fall through to
    # "unconfigured" (503) — the real outbound httpx call then fails to
    # connect to http://own, which surfaces as 502.
    assert resp.status_code == 502


def test_resolver_falls_back_to_the_runners_app_file_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(ap_client, "_runners_config", lambda: {})
    ctx = _make_ctx(config={}, secret_token=None)

    plugin = WorkspaceAgentPlugin()
    asyncio.run(plugin.activate(ctx))
    app = ctx.routes.register.call_args[0][0]

    from fastapi.testclient import TestClient

    client = TestClient(app)
    with caplog.at_level("WARNING"):
        resp1 = client.get("/api/bootstrap")
        resp2 = client.get("/api/bootstrap")

    assert resp1.status_code == 503
    assert resp2.status_code == 503
    assert sum("Fleet will serve 503" in r.message for r in caplog.records) == 1


def test_deactivate_completes_without_touching_ctx():
    assert asyncio.run(WorkspaceAgentPlugin().deactivate()) is None
