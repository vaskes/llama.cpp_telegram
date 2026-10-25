"""Tests for T38 bash_command tool.

Covers:
  1. Profile gate: tool def only loads in personal profile
  2. Token gate: tool def is disabled if AGENT_STACK_TOKEN is empty
  3. Tool def shape: matches llama-server's tools list schema
  4. HTTP integration (mocked): success / 401 / network error / timeout
  5. tool_result_to_text rendering
"""

import os
import sys
from unittest.mock import patch

import pytest

# Repo path so 'personal_tools' is importable from tests/
_REPO = os.path.join(os.path.dirname(__file__), "..", "telegram-bot")
sys.path.insert(0, _REPO)


@pytest.fixture
def personal_with_token(monkeypatch):
    """Set the env to personal + token. Yields the module."""
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "personal")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token-abc")
    monkeypatch.setenv("AGENT_STACK_URL", "http://sandbox.test:8090")
    # Re-import to pick up env
    for mod in list(sys.modules):
        if mod == "personal_tools.bash_command":
            del sys.modules[mod]
    import personal_tools.bash_command as m
    yield m


@pytest.fixture
def public_with_token(monkeypatch):
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "public")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token-abc")
    for mod in list(sys.modules):
        if mod == "personal_tools.bash_command":
            del sys.modules[mod]
    import personal_tools.bash_command as m
    yield m


@pytest.fixture
def personal_no_token(monkeypatch):
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "personal")
    monkeypatch.delenv("AGENT_STACK_TOKEN", raising=False)
    for mod in list(sys.modules):
        if mod == "personal_tools.bash_command":
            del sys.modules[mod]
    import personal_tools.bash_command as m
    yield m


def test_is_enabled_in_personal_with_token(personal_with_token):
    assert personal_with_token.is_enabled_for_active_profile() is True


def test_is_disabled_in_public_profile_even_with_token(public_with_token):
    """CRITICAL: public profile can NEVER enable bash, even if
    AGENT_STACK_TOKEN is set. This is the single most important
    property of this module.
    """
    assert public_with_token.is_enabled_for_active_profile() is False, (
        "public profile must NEVER have bash_command enabled, "
        "regardless of env. This is a security boundary."
    )


def test_is_disabled_in_personal_without_token(personal_no_token):
    """Defence in depth: even in personal profile, an empty token
    disables the tool. The bot must not start crashing or call
    the sandbox without a token.
    """
    assert personal_no_token.is_enabled_for_active_profile() is False


def test_tool_def_has_required_openai_shape(personal_with_token):
    """The tool definition must match llama-server's tools list schema:
    {type: "function", function: {name, description, parameters: {type, properties, required}}}
    """
    d = personal_with_token.BASH_COMMAND_TOOL_DEF
    assert d["type"] == "function"
    fn = d["function"]
    assert fn["name"] == "bash_command"
    assert isinstance(fn["description"], str) and len(fn["description"]) > 50
    p = fn["parameters"]
    assert p["type"] == "object"
    assert "command" in p["properties"]
    assert "session_id" in p["properties"]
    assert p["required"] == ["command"]


@pytest.mark.asyncio
async def test_call_bash_command_success(personal_with_token):
    """Mock the side-car: returns a typical payload. Verify our
    handler parses it and surfaces fields the LLM can use.
    """
    fake_response = {
        "stdout": "hello\n",
        "stderr": "",
        "exit_code": 0,
        "duration_ms": 12,
        "timed_out": False,
    }
    class FakeResp:
        status_code = 200
        def json(self): return fake_response
        text = ""
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json, headers):
            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Bearer ")
            assert json["command"] == "echo hello"
            return FakeResp()
    with patch("personal_tools.bash_command.httpx.AsyncClient", FakeClient):
        result = await personal_with_token.call_bash_command("echo hello",
        chat_type="private",
    )
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.asyncio
async def test_call_bash_command_token_rejected_401(personal_with_token):
    """If AGENT_STACK_TOKEN is wrong, the side-car returns 401.
    Our handler must surface that as a clear error, not a stack trace.
    """
    class FakeResp:
        status_code = 401
        text = "unauthorized"
        def json(self): raise ValueError("not json")
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw): return FakeResp()
    with patch("personal_tools.bash_command.httpx.AsyncClient", FakeClient):
        result = await personal_with_token.call_bash_command("ls",
        chat_type="private",
    )
    assert result["ok"] is False
    assert "AGENT_STACK_TOKEN" in result["error"] or "rejected" in result["error"]
    assert result["http_status"] == 401


@pytest.mark.asyncio
async def test_call_bash_command_sidecar_unreachable(personal_with_token):
    """If the side-car is down (network error), our handler returns
    a clean error dict, not a crash.
    """
    import httpx
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")
    with patch("personal_tools.bash_command.httpx.AsyncClient", FakeClient):
        result = await personal_with_token.call_bash_command("ls",
        chat_type="private",
    )
    assert result["ok"] is False
    assert "unreachable" in result["error"] or "ConnectError" in result["error"]


@pytest.mark.asyncio
async def test_call_bash_command_refused_in_public_profile(public_with_token):
    """Even if a misconfigured caller forgets the profile check, the
    handler itself refuses when not in personal mode.
    """
    result = await public_with_token.call_bash_command("rm -rf /",
        chat_type="private",
    )
    assert result["ok"] is False
    assert "personal" in result["error"]


def test_tool_result_to_text_renders_stdout(personal_with_token):
    r = {"ok": True, "stdout": "hello\n", "stderr": "", "exit_code": 0, "duration_ms": 12}
    text = personal_with_token.tool_result_to_text(r)
    assert "exit_code=0" in text
    assert "duration_ms=12" in text
    assert "hello" in text


def test_tool_result_to_text_truncates_large_output(personal_with_token):
    """Keep tool message size bounded. A 100KB stdout would blow the
    next request size. 8KB is the limit per the design.
    """
    big = "x" * 100_000
    r = {"ok": True, "stdout": big, "stderr": "", "exit_code": 0, "duration_ms": 12}
    text = personal_with_token.tool_result_to_text(r)
    # Truncated to <= ~8KB stdout + frame
    assert len(text) < 12_000
    assert "stdout" in text


def test_tool_result_to_text_marks_timeout(personal_with_token):
    r = {"ok": True, "stdout": "", "stderr": "", "exit_code": 124, "duration_ms": 60000, "timed_out": True}
    text = personal_with_token.tool_result_to_text(r)
    assert "TIMED_OUT" in text


@pytest.mark.asyncio
async def test_the_proxy_error_envelope_is_a_failure_not_an_empty_success(
        personal_with_token):
    """A 200 with an "error" key is a failure.

    The proxy answers a failed upstream call with HTTP 200 and
    ``{"error": "sandbox-agent request failed: <reason>"}``. The bot
    used to read that as a command that produced no output: the model
    saw an empty result, no error, and a command that the side-car had
    stopped waiting for.
    """
    class FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {"error": "sandbox-agent request failed: "}
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw): return FakeResp()
    with patch("personal_tools.bash_command.httpx.AsyncClient", FakeClient):
        result = await personal_with_token.call_bash_command(
            "sudo -n apt-get install -y bc", chat_type="private")
    assert result["ok"] is False, (
        "the error envelope was read as a successful command with no "
        "output")
    assert "60-second" in result["error"], (
        "an empty reason means the side-car stopped waiting at its own "
        f"per-command limit; the message has to say that: {result['error']!r}")
    assert "/dev/null" in result["error"], (
        "a command that reads stdin swallows the side-car's end marker, "
        "and the message has to offer the way out: "
        f"{result['error']!r}")


@pytest.mark.asyncio
async def test_a_sidecar_error_keeps_its_reason(personal_with_token):
    """A reason that survived the trip is carried, not swallowed."""
    class FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {"error": "sandbox-agent request failed: ConnectError('refused')"}
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw): return FakeResp()
    with patch("personal_tools.bash_command.httpx.AsyncClient", FakeClient):
        result = await personal_with_token.call_bash_command(
            "ls", chat_type="private")
    assert result["ok"] is False
    assert "ConnectError" in result["error"], (
        f"the reason was dropped: {result['error']!r}")


def test_the_tool_description_carries_the_stdin_policy(personal_with_token):
    """The model has to know about the stdin trap before it hits it.

    The side-car knows a command is over because a marker it printed
    after the command shows up in the pane. A command that reads
    standard input reads that marker instead -- apt-get does it
    through debconf -- and the answer is lost even though the command
    finished. A description that does not say so sends the model
    around the same loop: install, wait a minute, time out, install.
    """
    desc = personal_with_token.BASH_COMMAND_TOOL_DEF["function"]["description"]
    assert "/dev/null" in desc, (
        "the tool description does not mention the stdin trap")
    assert "standard input" in desc, (
        "the tool description does not say what the trap is")
