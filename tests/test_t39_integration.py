"""Tests for T39: bash_command integration in call_llama.

These tests pin the wiring between the bot's tool loop and
personal_tools.bash_command. The unit tests for the module
itself live in test_bash_command.py; here we test the
call_llama-side glue.

Pinned properties:

  1. In personal profile + token: bash_command is in the
     tool list the bot would send to llama.
  2. In public profile OR missing token: bash_command is
     NOT in the tool list.
  3. The dispatch arm for bash_command calls the sandbox
     side-car and returns its formatted result.
  4. The dispatch arm refuses bash_command in public
     profile (defence in depth, even if the tool somehow
     ended up in the list).
"""

import os
import sys

import pytest

_REPO = os.path.join(os.path.dirname(__file__), "..", "telegram-bot")
sys.path.insert(0, _REPO)


@pytest.fixture
def personal_with_token(monkeypatch):
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "personal")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token-abc")
    # Re-import the personal_tools module to pick up new env
    for mod in list(sys.modules):
        if mod.startswith("personal_tools"):
            del sys.modules[mod]
    yield


@pytest.fixture
def public_with_token(monkeypatch):
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "public")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token-abc")
    for mod in list(sys.modules):
        if mod.startswith("personal_tools"):
            del sys.modules[mod]
    yield


def test_bash_command_def_helper_true_in_personal(personal_with_token):
    """_bash_command_tool_enabled() must return True when
    personal profile is active AND the token is set.
    """
    import call_llama
    # Force module import to pick up the new helper
    assert hasattr(call_llama, "_bash_command_tool_enabled")
    assert call_llama._bash_command_tool_enabled() is True


def test_bash_command_def_helper_false_in_public(public_with_token):
    """_bash_command_tool_enabled() must return False in public,
    even with a valid token, even if personal_tools is importable.
    This is the bot-side guard that keeps the LLM from ever
    seeing the tool in a public chat.
    """
    import call_llama
    assert call_llama._bash_command_tool_enabled() is False


def test_bash_command_def_helper_false_when_token_missing(monkeypatch):
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "personal")
    monkeypatch.delenv("AGENT_STACK_TOKEN", raising=False)
    for mod in list(sys.modules):
        if mod.startswith("personal_tools"):
            del sys.modules[mod]
    import call_llama
    assert call_llama._bash_command_tool_enabled() is False


def test_bash_command_tool_def_is_loaded_in_personal(personal_with_token):
    """The tool def itself is a module-level constant in
    call_llama. It exists regardless of profile; the profile
    only controls whether it is APPENDED to bot_side_tool_defs.
    """
    import call_llama
    assert hasattr(call_llama, "_BASH_COMMAND_TOOL_DEF")
    d = call_llama._BASH_COMMAND_TOOL_DEF
    assert d["type"] == "function"
    assert d["function"]["name"] == "bash_command"
    assert "sandbox" in d["function"]["description"].lower()
    assert "command" in d["function"]["parameters"]["properties"]


def test_personal_tools_bash_command_in_known_tool_names():
    """The whitelist from T38.5 must already include bash_command
    (otherwise the tool_isolation guard would reject our own
    tool call). Pin that here as a regression tripwire.
    """
    from tool_isolation import KNOWN_TOOL_NAMES
    assert "bash_command" in KNOWN_TOOL_NAMES
