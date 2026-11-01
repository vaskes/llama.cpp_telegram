"""The file door: the model reads a file WHOLE, from the first byte to the last.

Measured on production, 2026-11-01: the operator asked the bot to read
``/workspace/characters.json`` (511 949 bytes) and the model answered as
if it had read the file. It had not, and three things of the bot's own
doing stood between the file and the model's context:

  * the session detector read the ANSWER's text, found the words
    "session", "unknown" and "expired" IN THE FILE, called the session
    forgotten and killed the door on a read that had succeeded;
  * the registry cut every tool result to 24 000 characters;
  * the iteration cut it again to 12 000, dropping the middle.

So the model fell back to the shell, got a fragment, and said it had
read the file. The operator's test was simple, and it is the one these
tests keep: read the file from beginning to end, all of it.

The file door is a door of its own and it has a budget of its own: the
server's read budget, not the bot's tool budget. The bot checks that the
text it holds encodes to exactly the number of bytes the server
declared, so "this is the whole file" is a fact and not a hope.

The wire is faked at the HTTP level; the client, the registry, the
dispatch and the iteration are the real ones.
"""
import json
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import llm_dispatch  # noqa: E402
import llm_iteration  # noqa: E402
import mcp_client  # noqa: E402
import mcp_registry  # noqa: E402
from llm_artifact import extract_files  # noqa: E402
from llm_dispatch import DispatchDeps, ToolExecution  # noqa: E402
from mcp_client import McpClient, McpServerConfig  # noqa: E402
from mcp_registry import McpRegistry  # noqa: E402

# `McpFile` and `tool_result_content` are what this wave added, and they
# are reached THROUGH the module on purpose: a tripwire that cannot be
# collected on the old tree cannot say which behaviour broke.


# -- the wire ---------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._body)


class FakeHttp:
    """The wire, faked at the HTTP level. Everything above it is real."""

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
            raise AssertionError(
                "the client made more calls than the test allowed")
        return self._responses.pop(0)


def _install(monkeypatch, responses):
    fake = FakeHttp(responses)
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", fake)
    return fake


def _ok(result, session=None):
    headers = {"content-type": "application/json"}
    if session:
        headers["mcp-session-id"] = session
    return FakeResponse(200, {"jsonrpc": "2.0", "id": "x", "result": result},
                        headers)


def _config(**kwargs):
    defaults = dict(name="sandbox", url="http://sandbox.invalid/mcp")
    defaults.update(kwargs)
    return McpServerConfig(**defaults)


def _file_body(lines: int = 4000) -> str:
    """A file body with a head and a tail you can look for.

    It is bigger than both tool budgets on purpose (24 000 characters
    for the registry, 12 000 for the iteration), so a cut anywhere in
    the chain shows up as a missing tail.
    """
    head = "FILE-HEAD-MARKER\n"
    tail = "FILE-TAIL-MARKER\n"
    middle = "".join(f"line {i:06d} of the file body\n" for i in range(lines))
    return head + middle + tail


def _wire(body=None, *, name="characters.json", mime="application/json",
          size=None):
    """The responses a handshake and one tool call produce."""
    if body is None:
        body = _file_body()
    if size is None:
        size = len(body.encode("utf-8"))
    return [
        _ok({"protocolVersion": "2024-11-05"}),
        FakeResponse(202, {}, {"content-type": "application/json"}),
        _ok({"tools": [{"name": "read_file", "description": "read a file"}]}),
        _ok({"content": [{"type": "text", "text": body}],
            "structuredContent": {"name": name, "size": size, "mime": mime}}),
    ]


def _registry(monkeypatch):
    """A real registry, over the fake wire."""
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "personal")
    return McpRegistry([McpServerConfig(name="sandbox",
                                        url="http://sandbox.invalid/mcp")])


def _deps(**over):
    base = dict(bot=object(), chat_id=4242, current_message_id=99,
                chat_type="private", iteration=1,
                custom_tools={}, donsetch_tools={})
    base.update(over)
    return DispatchDeps(**base)


# -- the detector: transport says, data does not ----------------------------


async def test_a_result_that_mentions_a_session_is_not_a_forgotten_session(
        monkeypatch):
    """The file may talk about sessions. That is DATA, not a transport fact.

    Measured on production, 2026-11-01: this very file carried the
    words "session", "unknown" and "expired", and the detector read
    them out of the ANSWER, called the session forgotten and killed the
    door on a read that had succeeded.
    """
    body = "the session expired or is unknown, and here is the file"
    _install(monkeypatch, [_ok({
        "content": [{"type": "text", "text": body}],
        "structuredContent": {"name": "a.json",
                              "size": len(body.encode("utf-8")),
                              "mime": "application/json"},
    })])
    client = McpClient(_config())

    result = await client.call_tool("read_file", {"path": "/tmp/a.json"})

    assert result.text == body
    assert result.file is not None, "it is the whole file, and it says so"


async def test_a_404_still_means_a_forgotten_session(monkeypatch):
    """The status code is a transport fact, and it still means what it meant."""
    fake = _install(monkeypatch, [
        FakeResponse(404, {"detail": "unknown session"}),
        _ok({}, session="sess-2"),
        FakeResponse(202, {}, {"content-type": "application/json"}),
        _ok({"content": [{"type": "text", "text": "second try"}]}),
    ])
    client = McpClient(_config())

    result = await client.call_tool("t", {})

    assert result.text == "second try"
    assert [r["json"].get("method") for r in fake.requests] == [
        "tools/call", "initialize", "notifications/initialized", "tools/call"]


async def test_a_jsonrpc_error_that_names_the_session_is_retried(monkeypatch):
    """An error envelope is a transport fact too, even with a 200."""
    fake = _install(monkeypatch, [
        FakeResponse(200, {"jsonrpc": "2.0", "id": "x", "error": {
            "code": -32001, "message": "session expired or unknown"}}),
        _ok({}, session="sess-3"),
        FakeResponse(202, {}, {"content-type": "application/json"}),
        _ok({"content": [{"type": "text", "text": "second try"}]}),
    ])
    client = McpClient(_config())

    result = await client.call_tool("t", {})

    assert result.text == "second try"
    assert fake.requests[1]["json"].get("method") == "initialize"


# -- what counts as the whole file -----------------------------------------


async def test_the_declared_size_decides_whether_we_hold_the_whole_file(
        monkeypatch):
    """"This is the whole file" is a fact we can check, not a hope."""
    body = "the whole file"
    _install(monkeypatch, [_ok({
        "content": [{"type": "text", "text": body}],
        "structuredContent": {"name": "a.txt",
                              "size": len(body.encode("utf-8")),
                              "mime": "text/plain"},
    })])
    client = McpClient(_config())

    whole = await client.call_tool("read_file", {})

    assert whole.file is not None
    assert whole.file.name == "a.txt"
    assert whole.file.size == len(body.encode("utf-8"))

    _install(monkeypatch, [_ok({
        "content": [{"type": "text", "text": body}],
        "structuredContent": {"name": "a.txt", "size": 99999,
                              "mime": "text/plain"},
    })])
    client = McpClient(_config())

    piece = await client.call_tool("read_file", {})

    assert piece.file is None, "a file we cannot vouch for is not a file"


async def test_a_binary_file_is_still_not_inlined(monkeypatch):
    """The blob of a binary file is still not inlined -- and it is not a file."""
    _install(monkeypatch, [_ok({
        "content": [{"type": "resource", "resource": {
            "uri": "file://probe.bin",
            "mimeType": "application/octet-stream",
            "blob": "QUJDREVG",
        }}],
        "structuredContent": {"name": "probe.bin", "size": 2048,
                              "mime": "application/octet-stream"},
    })])
    client = McpClient(_config())

    result = await client.call_tool("read_file", {})

    assert "probe.bin" in result.text
    assert "QUJDREVG" not in result.text
    assert result.file is None


# -- the door: the file reaches the model whole -----------------------------


async def test_the_registry_does_not_cut_a_whole_file(monkeypatch):
    body = _file_body()
    _install(monkeypatch, _wire(body))
    registry = _registry(monkeypatch)
    await registry.ensure_ready()

    result = await registry.call("sandbox_read_file", {"path": "/tmp/a.json"})

    assert "FILE-HEAD-MARKER" in result.text
    assert "FILE-TAIL-MARKER" in result.text, "the tail is the whole point"
    assert "truncated at" not in result.text
    assert len(result.text) > mcp_registry.MAX_RESULT_CHARS


async def test_an_ordinary_result_is_still_capped(monkeypatch):
    """The web tools keep their budget. Only a file gets the file's."""
    _install(monkeypatch, [
        _ok({"protocolVersion": "2024-11-05"}),
        FakeResponse(202, {}, {"content-type": "application/json"}),
        _ok({"tools": [{"name": "web_fetch", "description": "fetch"}]}),
        _ok({"content": [{"type": "text", "text": "x" * 60000}]}),
    ])
    registry = _registry(monkeypatch)
    await registry.ensure_ready()

    result = await registry.call("sandbox_web_fetch", {"url": "http://x"})

    assert "truncated at" in result.text
    assert result.file is None


async def test_the_model_is_told_the_file_is_complete(monkeypatch):
    body = _file_body()
    _install(monkeypatch, _wire(body))
    registry = _registry(monkeypatch)
    await registry.ensure_ready()

    result = await registry.call("sandbox_read_file", {"path": "/tmp/a.json"})

    assert "characters.json" in result.text
    assert str(len(body.encode("utf-8"))) in result.text
    assert "complete file" in result.text


async def test_the_dispatch_hands_the_model_the_whole_file(monkeypatch):
    body = _file_body()
    _install(monkeypatch, _wire(body))
    registry = _registry(monkeypatch)
    await registry.ensure_ready()
    monkeypatch.setattr(mcp_registry, "get_registry", lambda: registry)

    outcome = await llm_dispatch.execute_tool(
        "sandbox_read_file", {"path": "/tmp/a.json"}, _deps())

    assert "FILE-TAIL-MARKER" in outcome.text
    assert outcome.file is not None
    assert outcome.empty is False, "a file is a result, whatever it says"
    # The ledger keeps what the MODEL saw: a quote from the middle of
    # the file has to be recognizable as output (2026-10-30).
    assert outcome.ledger["output"] == outcome.text


async def test_a_quote_from_the_middle_of_a_file_is_not_a_deliverable(
        monkeypatch):
    """The 2026-10-30 failure, applied to the file door.

    The model quoted the middle of a long output, the ledger held a
    narrower window than the model had seen, the quote looked like a
    file, and the user got answer.txt with the execution artifacts in
    it.  A comparison against the wrong window cannot see the quote it
    is supposed to see.
    """
    body = _file_body()
    _install(monkeypatch, _wire(body))
    registry = _registry(monkeypatch)
    await registry.ensure_ready()
    monkeypatch.setattr(mcp_registry, "get_registry", lambda: registry)

    outcome = await llm_dispatch.execute_tool(
        "sandbox_read_file", {"path": "/tmp/a.json"}, _deps())

    quote = "\n".join(body.splitlines()[1000:1010])
    answer = "here is the part you asked for:\n```json\n" + quote + "\n```"

    prose, files = extract_files(answer,
                                 tool_outputs=[outcome.ledger["output"]])

    assert files == [], "a quote of the file is not a document to deliver"
    assert quote in prose


def test_the_iteration_shows_the_model_the_whole_file():
    body = _file_body()
    outcome = ToolExecution(text=body,
                             file=mcp_client.McpFile(
                                 name="characters.json",
                                 size=len(body.encode("utf-8")),
                                 mime="application/json"))

    content = llm_iteration.tool_result_content(outcome)

    assert content == body
    assert "FILE-TAIL-MARKER" in content


def test_an_ordinary_result_is_still_cut_for_the_model():
    outcome = ToolExecution(text="x" * 50000)

    content = llm_iteration.tool_result_content(outcome)

    assert "characters were dropped from the middle" in content
    assert len(content) < 50000
