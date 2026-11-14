"""Boot smoke test for workspace_agent_app/__main__.py's standalone FastAPI
app. build_routes() is the SAME factory used in integrated mode, so this
only proves the mount prefix and that the frontend is reachable at it —
the route behavior itself is covered by test_routes.py.

Run: python -m pytest tests/test_standalone.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_agent_app.__main__ import SLUG, build_standalone_app  # noqa: E402


def test_standalone_mounts_the_api_under_the_slug_prefix():
    client = TestClient(build_standalone_app())
    resp = client.get(f"/api/apps/{SLUG}/api/bootstrap")
    assert resp.status_code == 503  # no env credentials set in this test


def test_standalone_serves_the_frontend_at_the_mount_root():
    client = TestClient(build_standalone_app())
    resp = client.get(f"/api/apps/{SLUG}/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
