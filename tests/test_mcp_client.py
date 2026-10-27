"""The MCP protocol client: what the bot says to a server, and what it does
with the answer.

The server side is not mocked away -- the client's whole job is the wire
format (the session header, the JSON-RPC envelope, the content blocks), so
the fake here is at the HTTP level and everything above it is the real
code.

Three things are pinned here, and each of them was measured on a live
server before it was written:

  * a server may NOT hand out a session (the sandbox does not), and the
    client must work with it;
  * a server may answer in SSE instead of JSON, and both are MCP;
  * a session the server forgot is worth exactly one re-initialize --
    the error says so, and the retry is what fixes it.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT))

import mcp_client  # noqa: E402
from mcp_client import (  # noqa: E402
    McpClient,
    McpError,
    McpServerConfig,
    McpSessionExpired,
)

pytestmark = pytest.mark.asyncio


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Stands in for httpx.AsyncClient.

    ``responses`` is a list: each POST pops the next one. ``requests``
    records what was sent, so a test can assert on the headers and the
    envelope without reading the fake's guts.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        if not self._responses:
            raise AssertionError("the client made more calls than the test allowed")
        return self._responses.pop(0)


def _install(monkeypatch, responses):
    fake = FakeClient(responses)
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", fake)
    return fake


def _config(**kwargs):
    defaults = dict(name="srv", url="http://mcp.invalid/mcp")
    defaults.update(kwargs)
    return McpServerConfig(**defaults)


def _ok(result, session=None):
    headers = {"content-type": "application/json"}
    if session:
        headers["mcp-session-id"] = session
    return FakeResponse(200, {"jsonrpc": "2.0", "id": "x", "result": result},
                        headers)


async def test_initialize_asks_for_the_protocol_and_remembers_the_session(monkeypatch):
    fake = _install(monkeypatch, [
        _ok({"protocolVersion": "2024-11-05"}, session="sess-1"),
        FakeResponse(202, {}, {"content-type": "application/json"}),
    ])
    client = McpClient(_config())

    sid = await client.initialize()

    assert sid == "sess-1"
    first = fake.requests[0]["json"]
    assert first["method"] == "initialize"
    assert first["params"]["protocolVersion"] == mcp_client.PROTOCOL_VERSION
    assert first["params"]["clientInfo"]["name"] == mcp_client.CLIENT_NAME
    # The notification the spec asks for, sent right after the handshake.
    assert fake.requests[1]["json"]["method"] == "notifications/initialized"


async def test_the_handshake_does_not_borrow_the_call_budget(monkeypatch):
    """The handshake runs while the model waits for its tool list.

    A server that cannot be reached in five seconds is not going to
    be reached in sixty either, and the model is waiting for the
    list. A tool call is the other way round: a crawl is slow on
    purpose, and it may have its own budget.
    """
    seen = []

    class Spy(FakeClient):
        def __call__(self, *args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return self

    fake = Spy([
        _ok({"protocolVersion": "2024-11-05"}),
        FakeResponse(202, {}, {"content-type": "application/json"}),
        _ok({"tools": [{"name": "read_file", "description": "read"}]}),
        _ok({"content": [{"type": "text", "text": "the file"}]}),
    ])
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", fake)
    client = McpClient(_config())

    await client.initialize()
    await client.list_tools()
    await client.call_tool("read_file", {"path": "/tmp/x"})

    assert seen[0] == 5.0, "the handshake gets the short budget"
    assert seen[1] == 5.0, "the tool list is part of the handshake"
    assert seen[-1] == 60.0, "the call gets the long one"


async def test_a_server_without_a_session_is_not_a_broken_server(monkeypatch):
    """The sandbox side-car hands out no session id.

    The client must not insist on one: the second call carries no
    Mcp-Session-Id header, and nothing fails.
    """
    fake = _install(monkeypatch, [
        _ok({"protocolVersion": "2024-11-05"}),          # initialize
        FakeResponse(202, {}, {"content-type": "application/json"}),
        _ok({"tools": [{"name": "read_file", "description": "read"}]}),
    ])
    client = McpClient(_config())

    assert await client.initialize() is None
    tools = await client.list_tools()

    assert [t["name"] for t in tools] == ["read_file"]
    assert "Mcp-Session-Id" not in fake.requests[2]["headers"]


async def test_the_session_id_is_sent_back_on_the_next_call(monkeypatch):
    fake = _install(monkeypatch, [
        _ok({}, session="sess-2"),
        FakeResponse(202, {}, {"content-type": "application/json"}),
        _ok({"tools": []}),
    ])
    client = McpClient(_config())
    await client.initialize()
    await client.list_tools()

    assert fake.requests[2]["headers"]["Mcp-Session-Id"] == "sess-2"


async def test_a_tool_result_is_flattened_into_text(monkeypatch):
    _install(monkeypatch, [_ok({"content": [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ]})])
    client = McpClient(_config())

    assert await client.call_tool("t", {}) == "hello\nworld"


async def test_an_image_block_becomes_a_line_and_not_a_blob(monkeypatch):
    _install(monkeypatch, [_ok({"content": [
        {"type": "image", "mimeType": "image/png", "data": "A" * 40},
    ]})])
    client = McpClient(_config())

    out = await client.call_tool("t", {})

    assert "image/png" in out
    assert "40 bytes" in out
    assert "AAAA" not in out


async def test_a_result_the_server_marked_as_an_error_says_so(monkeypatch):
    _install(monkeypatch, [_ok({"content": [
        {"type": "text", "text": "no such file"},
    ], "isError": True})])
    client = McpClient(_config())

    out = await client.call_tool("t", {})

    assert out.startswith("[mcp tool error:")
    assert "no such file" in out


async def test_an_empty_result_is_named_and_not_invented(monkeypatch):
    _install(monkeypatch, [_ok({"content": []})])
    client = McpClient(_config())

    assert await client.call_tool("t", {}) == "[mcp: empty result]"


async def test_a_session_the_server_forgot_is_re_initialized_once(monkeypatch):
    """The one error worth retrying.

    The server answers "unknown session", the client handshakes again
    and repeats the call. Exactly once: a server that keeps forgetting
    is down, and a retry loop would hide that.
    """
    fake = _install(monkeypatch, [
        FakeResponse(404, {"detail": "unknown session"}),
        _ok({}, session="sess-3"),
        FakeResponse(202, {}, {"content-type": "application/json"}),
        _ok({"content": [{"type": "text", "text": "second try"}]}),
    ])
    client = McpClient(_config())

    out = await client.call_tool("t", {})

    assert out == "second try"
    assert [r["json"].get("method") for r in fake.requests] == [
        "tools/call", "initialize", "notifications/initialized", "tools/call"]


async def test_a_server_that_keeps_forgetting_is_not_retried_forever(monkeypatch):
    fake = _install(monkeypatch, [
        FakeResponse(404, {"detail": "unknown session"}),
        _ok({}, session="sess-4"),
        FakeResponse(202, {}, {"content-type": "application/json"}),
        FakeResponse(404, {"detail": "unknown session"}),
    ])
    client = McpClient(_config())

    with pytest.raises(McpSessionExpired):
        await client.call_tool("t", {})

    # four requests: call, handshake, notification, call -- and no fifth.
    assert len(fake.requests) == 4


async def test_an_sse_answer_is_read_as_well_as_a_json_one(monkeypatch):
    body = ("event: message\n"
            "data: {\"jsonrpc\":\"2.0\",\"id\":\"x\",\"result\":{\"content\":"
            "[{\"type\":\"text\",\"text\":\"from sse\"}]}}\n\n")
    response = FakeResponse(200, {}, {"content-type": "text/event-stream"},
                            text=body)
    _install(monkeypatch, [response])
    client = McpClient(_config())

    assert await client.call_tool("t", {}) == "from sse"


async def test_an_http_error_is_named_with_its_status(monkeypatch):
    _install(monkeypatch, [FakeResponse(403, {"detail": "invalid token"})])
    client = McpClient(_config())

    with pytest.raises(McpError) as exc:
        await client.list_tools()

    assert "403" in str(exc.value)
    assert "invalid token" in str(exc.value)


async def test_a_json_rpc_error_is_named_with_its_message(monkeypatch):
    _install(monkeypatch, [FakeResponse(200, {
        "jsonrpc": "2.0", "id": "x",
        "error": {"code": -32602, "message": "unknown tool"},
    })])
    client = McpClient(_config())

    with pytest.raises(McpError) as exc:
        await client.list_tools()

    assert "unknown tool" in str(exc.value)


async def test_a_transport_failure_is_named_rather_than_swallowed(monkeypatch):
    class Boom:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            raise OSError("connection refused")

    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", Boom)
    client = McpClient(_config())

    with pytest.raises(McpError) as exc:
        await client.list_tools()

    assert "cannot reach" in str(exc.value)


async def test_the_filters_are_on_the_servers_own_names():
    config = _config(include=("read_file",), exclude=("bash",))

    assert config.wants("read_file") is True
    assert config.wants("bash") is False
    assert config.wants("file_info") is False

    everything = _config()
    assert everything.wants("anything") is True
