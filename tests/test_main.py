"""Unit tests for workspace_agent_app/__main__.py's main()/_resolve_config —
the standalone-mode entrypoint (`python -m workspace_agent_app`).
build_standalone_app()/SLUG are covered by test_standalone.py. uvicorn.run is
mocked so this never actually binds a socket.

Run: python -m pytest tests/test_main.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import workspace_agent_app.__main__ as main_mod  # noqa: E402


def test_main_starts_uvicorn_on_the_default_port_and_host():
    with patch.object(main_mod, "uvicorn") as mock_uvicorn:
        main_mod.main()

    mock_uvicorn.run.assert_called_once()
    _, kwargs = mock_uvicorn.run.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == main_mod.DEFAULT_PORT


def test_main_honors_port_and_host_env_overrides(monkeypatch):
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("AW_APP_HOST", "0.0.0.0")

    with patch.object(main_mod, "uvicorn") as mock_uvicorn:
        main_mod.main()

    _, kwargs = mock_uvicorn.run.call_args
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9999


def test_resolve_config_reads_the_agents_platform_env_vars(monkeypatch):
    monkeypatch.setenv("AW_AGENTS_PLATFORM_BASE", "http://envbase")
    monkeypatch.setenv("AW_AGENTS_PLATFORM_TOKEN", "envtoken")
    assert main_mod._resolve_config() == ("http://envbase", "envtoken")


def test_resolve_config_is_empty_when_unset(monkeypatch):
    monkeypatch.delenv("AW_AGENTS_PLATFORM_BASE", raising=False)
    monkeypatch.delenv("AW_AGENTS_PLATFORM_TOKEN", raising=False)
    assert main_mod._resolve_config() == ("", "")
