"""T50: unit tests for tool-isolation against the real
sandbox. These are fast, no network, and exercise the
core security boundary:

  1. validate_tool_calls() rejects unknown names
     (this is the T38.5 guard).
  2. KNOWN_TOOL_NAMES contains exactly the 6 whitelisted
     tools and nothing else.
  3. The dispatch chain in call_llama has a working arm
     for each whitelisted tool.
  4. The unhandled-tool branch produces a synthetic tool
     result that lists the available tools (T43/T49).

These tests do NOT need the sandbox. They run fast and
catch any regression in the tool-isolation layer.
"""
import ast
import pathlib

import pytest


TESTS_DIR = pathlib.Path(__file__).parent
TI_PATH = pathlib.Path(__file__).parent.parent / "telegram-bot" / "tool_isolation.py"
# T8.1: the tool loop moved to llm_iteration.py. Every assertion
# below is about the loop's body, not about call_llama, which now
# only builds a ToolLoop and awaits its run().
CALL_LLAMA = pathlib.Path(__file__).parent.parent / "telegram-bot" / "llm_iteration.py"


def test_known_tool_names_exact_set():
    """The whitelist must contain exactly the documented
    tools. Adding a tool requires updating KNOWN_TOOL_NAMES
    AND shipping a handler. Removing a tool requires
    updating both the whitelist and the system prompt.

    The set is spelled out here, name by name, and not as a number: a
    count next to a list goes stale the moment the list moves, and
    then the document asserts something nobody checked.

    The live probe found what this test could not: a
    tool that is dispatched but not in the whitelist is soft-rejected
    by the isolation layer, and the model loops on it until the loop
    gives up.
    """
    sys_path = str(pathlib.Path(__file__).parent.parent / "telegram-bot")
    import sys
    import sys; sys.path.insert(0, sys_path)
    from tool_isolation import KNOWN_TOOL_NAMES

    expected = frozenset({
        "donsetch_web_search",
        "donsetch_web_fetch",
        "donsetch_web_crawl",
        "donsetch_web_screenshot",
        "react_to_message",
        "bash_command",
        "send_file",
        "delete_messages",
        "search_messages",
    })
    assert KNOWN_TOOL_NAMES == expected, \
        f"KNOWN_TOOL_NAMES changed: {KNOWN_TOOL_NAMES!r}"


def test_validate_tool_calls_accepts_whitelisted():
    import sys; sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot"))
    from tool_isolation import validate_tool_calls, KNOWN_TOOL_NAMES

    # Build a list of valid tool calls.
    valid_calls = [
        {"id": f"call_{n}", "type": "function",
         "function": {"name": name, "arguments": "{}"}}
        for n, name in enumerate(KNOWN_TOOL_NAMES)
    ]
    result = validate_tool_calls(valid_calls)
    assert sorted(result) == sorted(KNOWN_TOOL_NAMES)


def test_validate_tool_calls_rejects_unknown():
    from tool_isolation import validate_tool_calls, UnknownToolNameError

    bad = [
        {"id": "c1", "type": "function",
         "function": {"name": "exec_shell_command", "arguments": "{}"}},
    ]
    with pytest.raises(UnknownToolNameError) as ei:
        validate_tool_calls(bad)
    assert "exec_shell_command" in ei.value.unknown_names


def test_validate_tool_calls_rejects_mixed():
    """Even one unknown name rejects the whole batch.
    This is the strict-mode behaviour: we cannot know
    whether the LLM has already executed the unknown
    tool inside llama-server and is handing us a forged
    result, or whether it expects us to execute it
    client-side. Both cases require rejection.
    """
    from tool_isolation import validate_tool_calls, UnknownToolNameError

    mixed = [
        {"id": "c1", "type": "function",
         "function": {"name": "bash_command", "arguments": "{}"}},
        {"id": "c2", "type": "function",
         "function": {"name": "exec_shell_command", "arguments": "{}"}},
    ]
    with pytest.raises(UnknownToolNameError) as ei:
        validate_tool_calls(mixed)
    assert "exec_shell_command" in ei.value.unknown_names


def test_call_llama_has_synthetic_tool_result():
    """The unhandled-tool branch in call_llama must
    produce a synthetic tool result that mentions the
    available tools. T49 relies on this feedback to let
    the model retry with a whitelisted tool."""
    # Block D4: the message is UNAVAILABLE_TOOL_MESSAGE in llm_dispatch.
    # Asserting the constant's text rather than the f-string that used to
    # sit inline is stronger -- the message cannot be reworded or dropped
    # without this going red.
    from llm_dispatch import UNAVAILABLE_TOOL_MESSAGE
    src = CALL_LLAMA.read_text()
    assert "tool {name} unavailable in this mode" in UNAVAILABLE_TOOL_MESSAGE
    # It must list bash_command in the MESSAGE so the model can see what
    # is available. This read call_llama.py, where bash_command appears
    # for unrelated reasons -- so after Block D4 it passed no matter what
    # the message said. A mutation that dropped the whole tool list was
    # reported as a survivor because of it.
    assert "bash_command" in UNAVAILABLE_TOOL_MESSAGE, (
        "the unavailable-tool message no longer names the tools, so the "
        "model cannot tell what it may retry with")
    for tool in ("donsetch_web_search", "react_to_message"):
        assert tool in UNAVAILABLE_TOOL_MESSAGE, \
            f"the unavailable-tool message stopped naming {tool}"
    # The synthetic result is appended to msgs (not just
    # logged), so the model sees it on the next iter.
    assert "msgs.append" in src


def test_call_llama_does_not_early_return_on_saw_unavailable():
    src_text = CALL_LLAMA.read_text()
    assert "T49: removed the early-return" in src_text
    assert "continuing" in src_text
    import re
    block = re.search(
        r"if saw_unavailable_tool:.*?\n\n        ",
        src_text, re.DOTALL,
    )
    assert block, "post-loop saw_unavailable_tool block not found"
    body = block.group(0)
    assert "return" not in body, (
        f"T49 regressed: post-loop block has return: {body[:200]}"
    )



def test_call_llama_has_stuck_call_detector():
    """T32c: the model that keeps asking for the same
    forbidden tool must be stopped by the stuck-call
    detector after 2 iters."""
    src = CALL_LLAMA.read_text()
    # The detector moved to llm_loop_guard.py in TECH-DEBT #3 Block C3.
    # Reading only call_llama.py reports it deleted.
    guard_src = CALL_LLAMA.parent.joinpath("llm_loop_guard.py").read_text()
    assert "current_calls == self.prev_calls" in guard_src
    assert "same tool calls as previous iter; aborting" in guard_src
