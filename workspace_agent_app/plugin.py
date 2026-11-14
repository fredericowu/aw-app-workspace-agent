"""Entrypoint referenced by aw-app.json's ``runtime.entrypoint``
("workspace_agent_app.plugin:WorkspaceAgentPlugin").

Tier-1, no `ui:code` — the frontend is a plain static bundle (frontend/)
served by this app's own routes and shown through a declarative `iframe`
window (windows/main.json). `activate(ctx)` just registers that sub-app
through the gated `ctx.routes` facade (capability `routes:register`).

Config is resolved through a callable rather than read once, so a settings
change (or a credential rotated on the Agents Platform Runners app) takes
effect on the next request with no restart — see ap_client.py for the full
resolution order.
"""

from __future__ import annotations

import logging

from . import ap_client
from . import routes as routes_mod

log = logging.getLogger("aw_apps.workspace_agent")


class WorkspaceAgentPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx

        def _resolve_config() -> tuple[str, str]:
            secret_token = (
                ctx.secrets.read("agents_platform_token") if hasattr(ctx, "secrets") else None
            )
            base, token, source = ap_client.resolve_credentials(
                getattr(ctx, "config", {}) or {}, secret_token
            )
            if source == "unconfigured" and not self._warned_unconfigured:
                log.warning(
                    "no Agents Platform base/token configured and the "
                    "agents-platform-runners app's own config could not be "
                    "read either — Fleet will serve 503 until one is set."
                )
                self._warned_unconfigured = True
            return base, token

        self._warned_unconfigured = False
        ctx.routes.register(routes_mod.build_routes(_resolve_config))

        log.info("aw-app-workspace-agent activated: routes mounted")

    async def deactivate(self) -> None:
        log.info("aw-app-workspace-agent deactivated")
