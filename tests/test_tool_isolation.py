"""Tests for T38.5 tool isolation.

The single most important property of this module is:
the bot MUST NEVER execute a tool_call whose function name is not
in KNOWN_TOOL_NAMES. These tests pin that property at every layer
(validator, integration) and document the threat model.
"""

import os
import sys

import pytest

_REPO = os.path.join(os.path.dirname(__file__), "..", "telegram-bot")
sys.path.insert(0, _REPO)

from tool_isolation import (
    KNOWN_TOOL_NAMES,
    UnknownToolNameError,
    extract_function_names,
    is_known_tool_name,
    known_tool_names,
    validate_tool_calls,
)


# --- whitelist shape --------------------------------------------------------

def test_known_tool_names_includes_required_bot_tools():
    """The whitelist must contain every tool the bot actually
    dispatches today. If a new tool is added to the bot but
    forgotten here, this test fails loud and early.
    """
    expected = {
        "donsetch_web_search",
        "donsetch_web_fetch",
        "donsetch_web_crawl",
        "donsetch_web_screenshot",
        "react_to_message",
        "bash_command",
    }
    missing = expected - set(KNOWN_TOOL_NAMES)
    extra = set(KNOWN_TOOL_NAMES) - expected
    assert not missing, f"bot-side tools missing from whitelist: {missing}"
    assert not extra, f"whitelist has stale names: {extra}"


def test_known_tool_names_does_not_advertise_native_llama_tools():
    """The whitelist must NEVER include names that look like
    native llama.cpp tools (shell, exec, agent, etc.). If a
    future maintainer accidentally adds one, this test fails.
    """
    forbidden = {
        "shell", "shell_tool", "exec", "exec_command", "bash",
        "agent", "agent_run", "agent_loop",
        "read", "write", "read_file", "write_file",  # NOT in this bot
    }
    leaked = forbidden & set(KNOWN_TOOL_NAMES)
    assert not leaked, (
        f"native-llama / non-bot names leaked into whitelist: {leaked}. "
        f"These would be available to the model even if llama-server "
        f"is run with --agent. Remove them."
    )


def test_known_tool_names_is_frozenset():
    """The whitelist is a frozenset so callers cannot mutate it
    at runtime. If this assertion ever fails, someone changed
    the type and we lost the immutability guarantee.
    """
    assert isinstance(KNOWN_TOOL_NAMES, frozenset)


# --- is_known_tool_name ------------------------------------------------------

def test_is_known_tool_name_true_for_whitelist():
    for n in KNOWN_TOOL_NAMES:
        assert is_known_tool_name(n) is True, f"{n!r} should be known"


def test_is_known_tool_name_false_for_native_llama_names():
    for n in ("shell", "exec", "agent", "agent_run", "bash", "eval"):
        assert is_known_tool_name(n) is False, f"{n!r} must NOT be known"


def test_is_known_tool_name_false_for_empty_and_garbage():
    assert is_known_tool_name("") is False
    assert is_known_tool_name("__proto__") is False
    assert is_known_tool_name("../etc/passwd") is False
    assert is_known_tool_name("bash_command; rm -rf /") is False


# --- extract_function_names --------------------------------------------------

def test_extract_function_names_handles_well_formed():
    tcs = [
        {"function": {"name": "bash_command", "arguments": "{}"}},
        {"function": {"name": "donsetch_web_search"}},
    ]
    assert extract_function_names(tcs) == ["bash_command", "donsetch_web_search"]


def test_extract_function_names_handles_malformed():
    """A poisoned tool_call entry must not crash the parser;
    it must surface as an empty string so the validator rejects it.
    """
    tcs = [
        {"function": {"name": "bash_command"}},     # OK
        {"function": None},                          # missing function dict
        "not a dict",                                # completely wrong type
        {"function": {"name": None}},                # name is None
        {},                                          # empty
    ]
    names = extract_function_names(tcs)
    assert names[0] == "bash_command"
    assert names[1:] == ["", "", "", ""]


def test_extract_function_names_handles_empty_list():
    assert extract_function_names([]) == []
    assert extract_function_names(None) == []  # defensively


# --- validate_tool_calls: the core guard ------------------------------------

def test_validate_tool_calls_passes_for_whitelisted_names():
    tcs = [
        {"function": {"name": "bash_command"}},
        {"function": {"name": "donsetch_web_search"}},
        {"function": {"name": "react_to_message"}},
    ]
    names = validate_tool_calls(tcs)
    assert names == ["bash_command", "donsetch_web_search", "react_to_message"]


def test_validate_tool_calls_rejects_native_shell_tool():
    """The single most important test: if llama-server returns a
    tool_call for the native 'shell' tool, we refuse to dispatch.
    This is the only thing standing between a successful --agent
    attack and a host shell escape.
    """
    tcs = [{"function": {"name": "shell", "arguments": "{}"}}]
    with pytest.raises(UnknownToolNameError) as exc_info:
        validate_tool_calls(tcs)
    assert "shell" in exc_info.value.unknown_names
    assert "known =" in str(exc_info.value)


def test_validate_tool_calls_rejects_exec_tool():
    tcs = [{"function": {"name": "exec", "arguments": "{}"}}]
    with pytest.raises(UnknownToolNameError):
        validate_tool_calls(tcs)


def test_validate_tool_calls_rejects_agent_tool():
    tcs = [{"function": {"name": "agent", "arguments": "{}"}}]
    with pytest.raises(UnknownToolNameError):
        validate_tool_calls(tcs)


def test_validate_tool_calls_rejects_mixed_batch():
    """If even ONE name in a batch is unknown, the whole batch
    is rejected. We cannot safely execute the known ones while
    skipping the unknown one, because the unknown one may have
    been executed server-side and the model may be feeding us a
    forged 'result' for it.
    """
    tcs = [
        {"function": {"name": "bash_command"}},       # known
        {"function": {"name": "shell"}},              # unknown - native tool
    ]
    with pytest.raises(UnknownToolNameError):
        validate_tool_calls(tcs)


def test_validate_tool_calls_rejects_malformed_entry():
    """A malformed entry surfaces as empty string, which is
    not in the whitelist, so the whole batch is rejected.
    """
    tcs = [
        {"function": {"name": "bash_command"}},
        {"function": None},   # malformed
    ]
    with pytest.raises(UnknownToolNameError):
        validate_tool_calls(tcs)


def test_validate_tool_calls_passes_empty_list():
    """The non-tool path (model just answered in plain text)
    must not be blocked by the isolation check. Empty list
    is a no-op.
    """
    assert validate_tool_calls([]) == []
    assert validate_tool_calls(None) == []


# --- known_tool_names accessor -----------------------------------------------

def test_known_tool_names_returns_whitelist():
    """Callers go through this accessor so test code and runtime
    code see the same set. Pin that the accessor returns the
    canonical frozenset.
    """
    assert known_tool_names() is KNOWN_TOOL_NAMES
