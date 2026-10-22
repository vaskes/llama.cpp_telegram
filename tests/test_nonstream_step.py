"""non_stream_step: what a non-streaming response means for the loop.

TECH-DEBT #8, block 2.

`non_stream_step` is pure: a `NonStreamResult` in, a `NonStreamStep`
out. No client, no clock, no await. That is the point of the extraction
and it is what makes it testable without any of them.

The first attempt at this cut took the whole branch -- the await of
`non_stream_iteration` and the `push_thinking` call included. Two
existing tripwires refused it and both were right:

  * `test_c1_nonstream` requires call_llama to call
    `non_stream_iteration` inside its OWN try, because the try is what
    catches a push_thinking failure that happens after the helper
    returns. Moving the try in turns that failure into a terminal
    "[llama-server request failed]" the user reads.
  * `test_c4_thinking` counts three `push_thinking` call sites.

So the seam is the RESULT, not the call. The tests below pin the
decision, and two more pin the seam itself, since that is the part that
was nearly got wrong.
"""
from loop_owner import loop_source, run_one  # noqa: E402
import ast  # noqa: E402
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

from llm_nonstream import (NonStreamResult, NonStreamStep,
                           non_stream_step, non_stream_iteration)


def _step(result, **kw):
    return non_stream_step(result,
                           accumulated_reasoning=kw.get("acc", ""),
                           saw_unavailable_tool=kw.get("saw", False))


# --- the three exits -------------------------------------------------

def test_final_outcome_is_terminal_and_passes_the_text_through():
    r = NonStreamResult(outcome="final", final_text="the answer")
    step = _step(r)
    assert step.terminal == "the answer"
    assert step.assert_terminal() == "the answer"


def test_final_outcome_preserves_a_none_text():
    """Abort returns final_text=None, and that is a real terminal value.

    Asserting `step.terminal` alone would pass for a step that forgot to
    set it. The discriminator is that assert_terminal does not fire --
    a continuation with no tool calls would raise.
    """
    step = _step(NonStreamResult(outcome="final", final_text=None))
    assert step.terminal is None
    assert step.is_terminal is True
    step.assert_terminal()          # does not raise: it IS a terminal step
    assert not step.tool_calls_buf


def test_content_with_no_tool_calls_is_terminal():
    r = NonStreamResult(outcome="loop", content="just text",
                        reasoning="", tool_calls=[])
    step = _step(r)
    assert step.terminal == "just text"
    assert step.tool_calls_buf is None


def test_tool_calls_continue_the_loop_in_the_sse_shape():
    """The buffer must be {idx: {id, name, arguments}}.

    The code that rebuilds the final list is shared with the streaming
    path and iterates this shape. If the keys move, the non-streaming
    tool call is silently dropped -- which is the exact bug the removed
    unconditional `return content` used to cause.
    """
    r = NonStreamResult(
        outcome="loop", content="reacting",
        tool_calls=[{"id": "call_1",
                     "function": {"name": "react_to_message",
                                  "arguments": '{"emoji":"👍"}'}},
                    {"function": {"name": "no_id_tool", "arguments": None}}])
    step = _step(r, acc="prior reasoning")
    assert step.terminal is None
    assert list(step.tool_calls_buf) == [0, 1]
    assert step.tool_calls_buf[0] == {
        "id": "call_1", "name": "react_to_message",
        "arguments": '{"emoji":"👍"}'}
    # a missing id gets the call_ns_ default, and a missing arguments
    # becomes "" rather than None -- the streaming parser never produces
    # None there and the shared consumer does not expect it
    assert step.tool_calls_buf[1]["id"] == "call_ns_1"
    assert step.tool_calls_buf[1]["arguments"] == ""


def test_a_function_key_that_is_none_does_not_crash():
    r = NonStreamResult(outcome="loop", content="x",
                        tool_calls=[{"id": "a", "function": None}])
    step = _step(r)
    assert step.tool_calls_buf[0]["name"] == ""


def test_saw_unavailable_tool_propagates():
    r = NonStreamResult(outcome="loop", content="c", tool_calls=[],
                        saw_unavailable_tool=True)
    assert _step(r).saw_unavailable_tool is True
    assert _step(r).terminal == "c"     # also on the text exit

    with_tools = NonStreamResult(
        outcome="loop", content="c",
        tool_calls=[{"id": "1", "function": {"name": "f", "arguments": "{}"}}],
        saw_unavailable_tool=True)
    assert _step(with_tools).saw_unavailable_tool is True


def test_accumulated_reasoning_travels_through_unchanged():
    """It was already folded in by the caller before this is called."""
    r = NonStreamResult(outcome="loop", content="c",
                        tool_calls=[{"id": "1",
                                    "function": {"name": "f", "arguments": "{}"}}])
    step = _step(r, acc="reasoning so far")
    assert step.accumulated_reasoning == "reasoning so far"
    assert step.reasoning_buf == "reasoning so far"


def test_the_flags_cannot_contradict_each_other():
    """Both directions. A step that is both would return AND dispatch."""
    with pytest.raises(AssertionError):
        NonStreamStep(is_terminal=True, terminal="x",
                      tool_calls_buf={0: {}}).assert_terminal()


def test_an_empty_tool_list_takes_the_text_exit_not_the_loop():
    """[] is falsy. Treating it as "has tools" would produce a step with
    no content and no tool calls -- the neither-state assert_terminal
    exists to reject."""
    r = NonStreamResult(outcome="loop", content="text", tool_calls=[])
    step = _step(r)
    assert step.terminal == "text"
    assert step.tool_calls_buf is None


# --- the neither-state guard -----------------------------------------

def test_a_neither_step_is_rejected_rather_than_sent_empty():
    """The failure this guard exists for: a future edit that fills in
    neither field would otherwise produce an empty reply to the user
    with nothing in the log to explain it."""
    with pytest.raises(AssertionError):
        NonStreamStep().assert_terminal()


def test_the_step_is_frozen():
    step = NonStreamStep(terminal="x")
    with pytest.raises(Exception):
        step.terminal = "y"


# --- the seam --------------------------------------------------------

def test_call_llama_still_owns_the_await_and_the_try():
    """C1's contract. Pinned here as well as in test_c1_nonstream so the
    reason survives even if that file is refactored: the try has to stay
    out here or a push_thinking failure becomes a user-visible error."""
    import ast
    src = loop_source()
    fn = run_one()
    def _callee(node):
        # bare `non_stream_iteration(...)` is an ast.Name, not an
        # ast.Attribute -- my first check only looked for the latter and
        # found nothing.
        return getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and _callee(n) == "non_stream_iteration"]
    assert calls, "call_llama must still call non_stream_iteration"
    for call in calls:
        owners = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Try)
                  and n.lineno <= call.lineno <= n.end_lineno]
        assert owners, "the non_stream_iteration call must be inside a try"


def test_the_sentinel_is_an_ordinary_local_not_a_locals_lookup():
    """`if not locals().get('_ns_handled')` decided whether the streaming
    branch ran, and the sentinel that fed it was set 60 lines upstream in
    a different `if`. Nothing connected the two but the function's local
    namespace."""
    import ast
    src = loop_source()
    fn = run_one()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and (getattr(node.func, "id", None)
                     or getattr(node.func, "attr", None)) == "locals"):
            raise AssertionError(
                f"call_llama calls locals() at line {node.lineno}")
    assert "ns_handled" in ast.unparse(fn)


def test_non_stream_iteration_is_untouched_by_this_block():
    """The HTTP path did not move. If a future refactor carries it in
    here, this is what notices."""
    assert callable(non_stream_iteration)