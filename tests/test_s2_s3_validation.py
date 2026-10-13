"""S2 + S3 tests: bash_command argument validation.

#S2 (Mavis wave-2): command field is not type-validated. The
LLM crafts `command`; a misbehaving LLM could send non-string,
megabyte-long, or NUL-bearing commands. Without validation, the
sandbox gets them verbatim and either crashes or DoS's the LLM
context on the next iteration.

#S3 (Mavis wave-2): session_id is not pattern-validated. The
sandbox uses session_id as a tmux session key; a session_id
with shell metacharacters could let the LLM reference arbitrary
tmux sessions, escape the sandbox's session-id parser, or
collide with system sessions.

These tests cover _validate_command() and _validate_session_id()
and the full call_bash_command() path with the bad inputs.
"""
import asyncio
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot"))

from personal_tools.bash_command import (
    _validate_command, _validate_session_id, call_bash_command,
)


# === _validate_command ===

def test_command_string_ok():
    ok, normalised, err = _validate_command("ls -l")
    assert ok is True
    assert normalised == "ls -l"
    assert err is None


def test_command_empty_string_rejected():
    ok, normalised, err = _validate_command("")
    assert ok is False
    assert "non-empty" in err


def test_command_none_rejected():
    ok, normalised, err = _validate_command(None)
    assert ok is False
    assert "string" in err


def test_command_int_rejected():
    ok, normalised, err = _validate_command(42)
    assert ok is False
    assert "string" in err
    assert "int" in err


def test_command_list_rejected():
    """A misbehaving LLM that sends a list of commands instead
    of a single string must be rejected at our boundary, not at
    the sandbox's JSON parser."""
    ok, normalised, err = _validate_command(["ls", "-l"])
    assert ok is False
    assert "string" in err


def test_command_dict_rejected():
    """A dict payload must also be rejected."""
    ok, normalised, err = _validate_command({"cmd": "ls"})
    assert ok is False
    assert "string" in err


def test_command_too_long_rejected():
    """A 1MB command would fill the LLM context on the next
    iteration and DoS the bot."""
    ok, normalised, err = _validate_command("a" * 9000)
    assert ok is False
    assert "too long" in err
    assert "8192" in err


def test_command_at_limit_accepted():
    """Exactly at the limit should pass."""
    ok, normalised, err = _validate_command("a" * 8192)
    assert ok is True
    assert err is None


def test_command_with_nul_rejected():
    """NUL bytes confuse some tmux/JSON parsers. Reject."""
    ok, normalised, err = _validate_command("ls\x00-la")
    assert ok is False
    assert "NUL" in err


def test_command_with_unicode_accepted():
    """Real names with Cyrillic / CJK / diacritics must survive."""
    ok, normalised, err = _validate_command("echo Привет 李四")
    assert ok is True
    assert normalised == "echo Привет 李四"


def test_command_with_newline_accepted():
    """Newlines are fine in commands (multiline scripts). Only
    NUL is forbidden."""
    ok, normalised, err = _validate_command("ls\necho hi")
    assert ok is True


# === _validate_session_id ===

def test_session_id_none_accepted():
    """None is the default; means "use chat-N auto-derived"."""
    ok, normalised, err = _validate_session_id(None)
    assert ok is True
    assert normalised is None
    assert err is None


def test_session_id_simple_ok():
    ok, normalised, err = _validate_session_id("chat-12345")
    assert ok is True
    assert normalised == "chat-12345"


def test_session_id_alphanumeric_ok():
    ok, normalised, err = _validate_session_id("abcXYZ012")
    assert ok is True


def test_session_id_with_dot_underscore_ok():
    ok, normalised, err = _validate_session_id("user.session_1")
    assert ok is True


def test_session_id_empty_rejected():
    ok, normalised, err = _validate_session_id("")
    assert ok is False
    assert "match" in err


def test_session_id_with_path_traversal_rejected():
    """../../etc/passwd would let the LLM try to escape the
    sandbox's session-id namespace."""
    ok, normalised, err = _validate_session_id("../../etc/passwd")
    assert ok is False


def test_session_id_with_shell_metachars_rejected():
    """`session-id; rm -rf /` would let the LLM try to inject
    shell metacharacters into the sandbox's session-id parser."""
    for bad in ("a;b", "a`b", "a$(b)", "a|b", "a&b", "a\nb", "a b"):
        ok, _, err = _validate_session_id(bad)
        assert ok is False, f"should reject {bad!r}"


def test_session_id_with_unicode_rejected():
    """Emoji / non-ASCII could be a tmux escape or a parser
    bypass attempt."""
    for bad in ("user李四", "user-👤", "user_ñ"):
        ok, _, err = _validate_session_id(bad)
        # Most non-ASCII will be rejected by the ASCII pattern.
        # Some Latin-1 supplement chars may pass, but emoji/CJK
        # will not. Verify those don't.
        if any(ord(c) > 127 for c in bad):
            assert ok is False, f"should reject {bad!r}"


def test_session_id_too_long_rejected():
    """A 1KB session_id is suspicious; cap at 128 chars."""
    ok, normalised, err = _validate_session_id("a" * 200)
    assert ok is False


def test_session_id_int_rejected():
    ok, normalised, err = _validate_session_id(12345)
    assert ok is False
    assert "string" in err


def test_session_id_list_rejected():
    ok, normalised, err = _validate_session_id(["a", "b"])
    assert ok is False
    assert "string" in err


# === call_bash_command: integration with validation ===

def test_call_bash_command_rejects_non_string_command(monkeypatch):
    """The full path: if the LLM passes a non-string command,
    call_bash_command returns ok=False before any network call."""
    # Use the simplest check: rate limit and profile are needed to
    # get to the validation point. Set them.
    import os
    os.environ["LLAMABOT_DEPLOYMENT_PROFILE"] = "personal"
    os.environ["AGENT_STACK_TOKEN"] = "test-token-12345"
    import importlib
    import personal_tools.bash_command as bc
    importlib.reload(bc)

    # Patch out the network call entirely so we know any
    # return is from the validators.
    called = {"value": False}
    async def fake_post(*a, **kw):
        called["value"] = True
        class R: status_code = 200; text = "{}"
        return R()
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(bc.call_bash_command(None, chat_type="private"))
    assert result["ok"] is False
    assert "string" in result["error"]
    assert called["value"] is False  # no network call


def test_call_bash_command_rejects_oversized_command(monkeypatch):
    import os
    os.environ["LLAMABOT_DEPLOYMENT_PROFILE"] = "personal"
    os.environ["AGENT_STACK_TOKEN"] = "test-token-12345"
    import importlib
    import personal_tools.bash_command as bc
    importlib.reload(bc)

    called = {"value": False}
    async def fake_post(*a, **kw):
        called["value"] = True
        class R: status_code = 200; text = "{}"
        return R()
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(bc.call_bash_command("a" * 10000, chat_type="private"))
    assert result["ok"] is False
    assert "too long" in result["error"]
    assert called["value"] is False


def test_call_bash_command_rejects_bad_session_id(monkeypatch):
    import os
    os.environ["LLAMABOT_DEPLOYMENT_PROFILE"] = "personal"
    os.environ["AGENT_STACK_TOKEN"] = "test-token-12345"
    import importlib
    import personal_tools.bash_command as bc
    importlib.reload(bc)

    called = {"value": False}
    async def fake_post(*a, **kw):
        called["value"] = True
        class R: status_code = 200; text = "{}"
        return R()
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(bc.call_bash_command(
        "ls", session_id="../../etc/passwd", chat_type="private",
    ))
    assert result["ok"] is False
    assert "match" in result["error"]
    assert called["value"] is False


def test_call_bash_command_accepts_good_inputs(monkeypatch):
    """The happy path: a well-formed command and session_id
    proceed to the network call (which we mock to return ok)."""
    import os
    os.environ["LLAMABOT_DEPLOYMENT_PROFILE"] = "personal"
    os.environ["AGENT_STACK_TOKEN"] = "test-token-12345"
    import importlib
    import personal_tools.bash_command as bc
    importlib.reload(bc)

    called = {"value": False}
    async def fake_post(*a, **kw):
        called["value"] = True
        class R:
            status_code = 200
            text = "{}"
            def json(self): return {"ok": True, "stdout": "hi", "stderr": "", "exit_code": 0, "duration_ms": 5}
        return R()
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(bc.call_bash_command(
        "ls", session_id="chat-12345", chat_type="private",
    ))
    assert called["value"] is True
    assert result["ok"] is True
    assert result["stdout"] == "hi"
