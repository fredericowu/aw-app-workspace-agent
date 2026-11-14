"""Coverage for the HTTP primitives in workspace_agent_app/ap_client.py that
the Sessions surface added: write calls and the SSE passthrough.

These are the two places where getting the transport subtly wrong produces a
symptom nobody attributes to the transport:

* A **write** that collapses the platform's own 400/404/409/429 refusals into
  a bare "upstream error" throws away the only part the user can act on — an
  unknown agent slug, a QA dedup, a budget refusal.
* A **stream** that buffers, or that dies without saying so, is
  indistinguishable from an agent still thinking. Both failure modes have to
  reach the client as a frame it can render.

Run: python -m pytest tests/test_ap_transport.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_agent_app import ap_client  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _post(handler, body=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await ap_client.post_json(c, "http://ap", "tok", "/api/x", body or {})


# ---- writes ---------------------------------------------------------------


def test_post_sends_the_bearer_and_returns_the_body():
    def handler(request):
        assert request.headers["authorization"] == "Bearer tok"
        assert json.loads(request.content) == {"input": {"input": "go"}}
        return httpx.Response(200, json={"run_id": "r1"})

    assert run(_post(handler, {"input": {"input": "go"}})) == {"run_id": "r1"}


def test_patch_uses_the_patch_verb():
    def handler(request):
        assert request.method == "PATCH"
        return httpx.Response(200, json={"name": "n"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await ap_client.patch_json(c, "http://ap", "tok", "/api/x", {"name": "n"})

    assert run(go()) == {"name": "n"}


@pytest.mark.parametrize(
    "status, detail",
    [
        (404, "not found"),
        (400, "call_me_back=true but there is no session to resolve the callback to"),
        (409, "a QA run for this context already exists"),
        (429, "target budget exceeded"),
    ],
)
def test_a_platform_refusal_reaches_the_user_verbatim(status, detail):
    """These four carry the only useful part of the failure. A bare 502
    "upstream error" would make a wrong agent slug and an exhausted budget
    look identical."""

    def handler(request):
        return httpx.Response(status, json={"detail": detail})

    with pytest.raises(ap_client.APError, match=detail[:30]):
        run(_post(handler))


def test_a_refusal_with_a_non_json_body_falls_back_to_its_text():
    def handler(request):
        return httpx.Response(400, text="plain text explosion")

    with pytest.raises(ap_client.APError, match="plain text explosion"):
        run(_post(handler))


def test_a_refusal_with_no_detail_still_names_the_status():
    def handler(request):
        return httpx.Response(409, json={})

    with pytest.raises(ap_client.APError, match="refused the request \\(409\\)"):
        run(_post(handler))


def test_a_bad_token_on_a_write_reads_the_same_as_on_a_read():
    def handler(request):
        return httpx.Response(401, json={"detail": "nope"})

    with pytest.raises(ap_client.APError, match="token invalid or expired"):
        run(_post(handler))


def test_a_500_on_a_write_is_raised_for_status():
    def handler(request):
        return httpx.Response(500, json={})

    with pytest.raises(httpx.HTTPStatusError):
        run(_post(handler))


def test_an_unreachable_platform_on_a_write_is_an_aperror():
    def handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(ap_client.APError, match="could not reach"):
        run(_post(handler))


# ---- SSE passthrough ------------------------------------------------------


def _stream(monkeypatch, handler):
    """Patch httpx.AsyncClient so stream_sse — which makes its OWN client,
    deliberately, so the socket outlives the first yield — hits the fake."""

    class Patched(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(ap_client.httpx, "AsyncClient", Patched)

    async def collect():
        return b"".join(
            [c async for c in ap_client.stream_sse("http://ap", "tok", "/api/runs/r1/stream")]
        )

    return run(collect())


def test_stream_passes_upstream_frames_through_byte_for_byte(monkeypatch):
    """The client's own EventSource parser has to see the frames the platform
    wrote — any re-chunking or re-encoding here is a bug waiting to happen."""
    frames = (
        b'event: llm_token\ndata: {"payload":{"delta":"he"}}\n\n'
        b'event: llm_token\ndata: {"payload":{"delta":"llo"}}\n\n'
        b'event: done\ndata: {"done":true}\n\n'
    )

    def handler(request):
        assert request.headers["authorization"] == "Bearer tok"
        assert request.url.path == "/api/runs/r1/stream"
        return httpx.Response(200, content=frames, headers={"content-type": "text/event-stream"})

    assert _stream(monkeypatch, handler) == frames


def test_a_bad_token_on_the_stream_arrives_as_a_renderable_error_frame(monkeypatch):
    """The response has already begun by the time a stream fails, so there is
    no HTTP status left to use — the failure has to be a frame."""

    def handler(request):
        return httpx.Response(401, json={"detail": "nope"})

    out = _stream(monkeypatch, handler)
    assert out.startswith(b"event: error\n")
    assert b"token invalid or expired" in out


def test_an_upstream_stream_error_status_is_reported_not_swallowed(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="unavailable")

    out = _stream(monkeypatch, handler)
    assert b"upstream stream returned 503" in out


def test_a_dropped_stream_connection_is_reported_not_silently_closed(monkeypatch):
    """A silently-closed stream leaves the UI on "thinking" forever — which
    is exactly the failure the 15s run poll exists to backstop, but the user
    should still be told."""

    def handler(request):
        raise httpx.ReadError("connection reset")

    out = _stream(monkeypatch, handler)
    assert b"event: error" in out
    assert b"connection reset" in out


def test_the_error_frame_is_valid_sse():
    frame = ap_client._sse_error("boom")
    assert frame.startswith(b"event: error\ndata: ")
    assert frame.endswith(b"\n\n")
    payload = json.loads(frame.split(b"data: ", 1)[1].strip())
    assert payload == {"kind": "error", "error": "boom"}
