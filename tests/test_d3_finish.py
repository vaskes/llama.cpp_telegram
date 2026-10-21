"""Block D3 -- llm_finish: how an iteration ends when no tool was called.

The block this replaces was a wall of returns in the middle of the loop,
and every one of them is a message a user can receive. The strings are
the contract, not an implementation detail: they are what shows up in
Telegram when a model burns its token budget thinking, and they carry
the diagnostic detail -- finish_reason, char counts -- that makes the
next report actionable.

So these tests are mostly about the strings. The differential ones
compare against the pre-D3 code transcribed verbatim, because the risk
here is a refactor that keeps the shape and quietly drops a field from a
message.

One thing worth stating plainly: the reasoning branch is a deliberate
behaviour, not an accident. Some models spend the whole token budget
thinking and emit no content; answering with the reasoning tells the
user what happened, and the generic "empty response" string reads as a
broken bot. If someone "simplifies" that away, a test should fail.
"""

import ast
import random
from pathlib import Path

import pytest

from llm_finish import (
    REASONING_TAIL_CHARS,
    Ending,
    finish_fallback_empty,
    finish_without_tool_calls,
    refuse_hallucinated_tools,
)

pytestmark = pytest.mark.tech_debt(item=8)

pytestmark = pytest.mark.tech_debt(item=8)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"


# --------------------------------------------------------------------------
# the oracle: the inline returns as they were, before D3
# --------------------------------------------------------------------------

def oracle_no_tools(content_buf, reasoning_buf, finish_reason, executed):
    """Verbatim transcription of the pre-D3 `if not tool_calls:` body."""
    if content_buf:
        return {"text": content_buf, "executed_tools": list(executed),
                "aborted": False}
    if reasoning_buf:
        tail = reasoning_buf[-3500:]
        return (
            f'_(модель потратила все токены на размышления, не оставив на сам ответ; '
            f'finish_reason={finish_reason}; reasoning_chars={len(reasoning_buf)})_\n\n'
            f'{tail}'
        )
    return (
        f'[model returned an empty response. finish_reason={finish_reason}, '
        f'reasoning_chars={len(reasoning_buf)}, content_chars=0]'
    )


def oracle_hallucinated(content_buf, reasoning_buf, finish_reason, executed):
    """Verbatim transcription of the pre-D3 `if req_tools is None:` body."""
    if content_buf:
        return {"text": content_buf, "executed_tools": list(executed),
                "aborted": False}
    return (
        f'_(модель попыталась вызвать инструмент, хотя в этом turn\'е он не запрашивался; '
        f'finish_reason={finish_reason}, content_chars={len(content_buf)})_\n\n'
        f'{reasoning_buf[-3500:] if reasoning_buf else "(no reasoning)"}'
    )


# --------------------------------------------------------------------------
# content wins
# --------------------------------------------------------------------------

def test_content_becomes_the_answer_with_the_ledger():
    got = finish_without_tool_calls(content_buf="привет", reasoning_buf="",
                                    finish_reason="stop", executed_tools=[])
    assert got == {"text": "привет", "executed_tools": [], "aborted": False}


def test_content_wins_over_reasoning():
    """Both present: the content is the answer. The reasoning was the
    model's private working, not something to send."""
    got = finish_without_tool_calls(content_buf="ответ", reasoning_buf="размышления",
                                    finish_reason="stop", executed_tools=[])
    assert got["text"] == "ответ"


def test_the_tool_ledger_is_copied_not_aliased():
    """list(...) matters: the caller keeps appending to its own list after
    the return, and an alias would mutate a result already on its way out."""
    executed = [{"name": "bash_command", "ok": True}]
    got = finish_without_tool_calls(content_buf="x", reasoning_buf="",
                                    finish_reason="stop", executed_tools=executed)
    executed.append({"name": "late", "ok": False})
    assert len(got["executed_tools"]) == 1, "the returned ledger aliases the caller's"


# --------------------------------------------------------------------------
# reasoning second
# --------------------------------------------------------------------------

def test_reasoning_alone_produces_the_explanatory_message():
    got = finish_without_tool_calls(content_buf="", reasoning_buf="думаю",
                                    finish_reason="length", executed_tools=[])
    assert "модель потратила все токены на размышления" in got
    assert "finish_reason=length" in got
    assert got.endswith("думаю")


def test_the_message_reports_the_full_reasoning_length_not_the_tail():
    """The count is diagnostic and must describe the whole buffer. Reporting
    the truncated length would understate how much was wasted."""
    long = "x" * 5000
    got = finish_without_tool_calls(content_buf="", reasoning_buf=long,
                                    finish_reason="length", executed_tools=[])
    assert "reasoning_chars=5000" in got
    assert "reasoning_chars=3500" not in got


def test_the_tail_is_capped():
    # Not a repeating pattern. The first version used "0123456789" * 900,
    # whose head and tail are identical -- so it passed with the tail
    # sliced from the wrong end. A mutation that changed
    # reasoning_buf[-N:] to reasoning_buf[:N] survived it.
    long = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(9000))
    assert long[:3500] != long[-3500:], "the control is degenerate again"

    got = finish_without_tool_calls(content_buf="", reasoning_buf=long,
                                    finish_reason="length", executed_tools=[])
    tail = got.split("\n\n", 1)[1]
    assert len(tail) == REASONING_TAIL_CHARS
    assert tail == long[-REASONING_TAIL_CHARS:], "the wrong end was kept"


def test_the_fallback_tail_is_the_end_too():
    long = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(9000))
    got = finish_fallback_empty(content=None, reasoning=long,
                                finish_reason="stop")
    assert got.split("\n\n", 1)[1] == long[-REASONING_TAIL_CHARS:]


def test_a_short_reasoning_is_not_padded_or_truncated():
    got = finish_without_tool_calls(content_buf="", reasoning_buf="коротко",
                                    finish_reason="stop", executed_tools=[])
    assert got.endswith("коротко")


# --------------------------------------------------------------------------
# nothing at all
# --------------------------------------------------------------------------

def test_nothing_produces_the_generic_diagnostic():
    got = finish_without_tool_calls(content_buf="", reasoning_buf="",
                                    finish_reason="stop", executed_tools=[])
    assert got == ("[model returned an empty response. finish_reason=stop, "
                   "reasoning_chars=0, content_chars=0]")


def test_content_chars_is_hardcoded_zero_and_stays_that_way():
    """The old branch could only be reached when content was empty, so the
    literal was correct. If this ever becomes an f-string reading a buffer,
    a test has to notice."""
    got = finish_without_tool_calls(content_buf="", reasoning_buf="",
                                    finish_reason="stop", executed_tools=[])
    assert "content_chars=0]" in got


# --------------------------------------------------------------------------
# hallucinated tools
# --------------------------------------------------------------------------

def test_hallucination_with_content_returns_the_content():
    got = refuse_hallucinated_tools(content_buf="вот ответ", reasoning_buf="",
                                    finish_reason="stop", executed_tools=[])
    assert got == {"text": "вот ответ", "executed_tools": [], "aborted": False}


def test_hallucination_with_nothing_says_so_explicitly():
    """An empty section reads as a truncated message. Naming the absence is
    the whole point of this branch."""
    got = refuse_hallucinated_tools(content_buf="", reasoning_buf="",
                                    finish_reason="stop", executed_tools=[])
    assert "модель попыталась вызвать инструмент" in got
    assert got.endswith("(no reasoning)")
    assert "\n\n\n" not in got


def test_hallucination_with_reasoning_shows_it():
    got = refuse_hallucinated_tools(content_buf="", reasoning_buf="думал о туле",
                                    finish_reason="tool_calls", executed_tools=[])
    assert got.endswith("думал о туле")
    assert "(no reasoning)" not in got


def test_hallucination_content_chars_is_always_zero():
    """An f-string reading the content length, in a branch that content
    can never reach.

    The content case returns the answer dict before this line, so
    content_chars here is always 0. That is correct, but it is invisible
    to the differential tests for the same reason it is invisible to a
    reader: every input that reaches the message has empty content.

    Pinned rather than "fixed", because turning it into a literal would
    be a behaviour-preserving change that nobody could tell apart from a
    behaviour change -- which is the same trap the no-tools message set,
    from the other direction.
    """
    for content in ("",):        # only reachable values -- see below
        got = refuse_hallucinated_tools(content_buf=content, reasoning_buf="",
                                        finish_reason="stop", executed_tools=[])
        assert "content_chars=0)" in got, f"content={content!r}: {got}"


def test_the_two_endings_tolerate_different_inputs():
    """A real asymmetry between the two functions, and it is not accidental.

    These are called with `content_buf` -- a string accumulator, never
    None -- so refuse_hallucinated_tools measures it without a guard. The
    fallback ending is called with msg.get("content"), which is None
    whenever the engine omits it, so that one normalises.

    Asserting the difference stops someone from "fixing" one of them in
    the direction that breaks its caller.
    """
    with pytest.raises(TypeError):
        refuse_hallucinated_tools(content_buf=None, reasoning_buf="",
                                  finish_reason="stop", executed_tools=[])

    assert finish_fallback_empty(content=None, reasoning=None,
                                 finish_reason="stop") == "[fallback empty: finish_reason=stop]"


def test_no_branch_of_the_hallucination_message_is_unreachable():
    """Control for the above: a non-empty content really does take the
    other branch, so the constant is a property of reachability and not
    of a broken test."""
    got = refuse_hallucinated_tools(content_buf="непусто", reasoning_buf="",
                                    finish_reason="stop", executed_tools=[])
    assert isinstance(got, dict), "the content branch stopped taking content"


def test_hallucination_message_names_the_finish_reason():
    got = refuse_hallucinated_tools(content_buf="", reasoning_buf="",
                                    finish_reason="tool_calls", executed_tools=[])
    assert "finish_reason=tool_calls" in got


def test_the_two_messages_are_not_interchangeable():
    """They say different things about different failures. A refactor that
    shares one formatter would quietly make them identical."""
    a = finish_without_tool_calls(content_buf="", reasoning_buf="r",
                                  finish_reason="stop", executed_tools=[])
    b = refuse_hallucinated_tools(content_buf="", reasoning_buf="r",
                                  finish_reason="stop", executed_tools=[])
    assert a != b


# --------------------------------------------------------------------------
# Ending, the classifier
# --------------------------------------------------------------------------

def test_ending_classifies_content_first():
    assert Ending.of("a", "b").kind == "content"


def test_ending_classifies_reasoning_second():
    assert Ending.of("", "b").kind == "reasoning"


def test_ending_classifies_nothing_last():
    assert Ending.of("", "").kind == "nothing"


def test_ending_carries_the_capped_tail():
    e = Ending.of("", "y" * 9000)
    assert len(e.reasoning_tail) == REASONING_TAIL_CHARS


# --------------------------------------------------------------------------
# differential against the oracle
# --------------------------------------------------------------------------

_CONTENT = ["", "ответ", "a" * 50]
_REASON = ["", "размышления", "z" * 4000]
_FINISH = ["stop", "length", "tool_calls", None]


@pytest.mark.parametrize("seed", range(60))
def test_no_tools_matches_the_inline_returns(seed):
    rnd = random.Random(seed)
    content = rnd.choice(_CONTENT)
    reasoning = rnd.choice(_REASON)
    finish = rnd.choice(_FINISH)
    executed = [{"name": f"t{i}"} for i in range(rnd.randint(0, 3))]

    expected = oracle_no_tools(content, reasoning, finish, executed)
    got = finish_without_tool_calls(content_buf=content, reasoning_buf=reasoning,
                                    finish_reason=finish, executed_tools=list(executed))
    assert got == expected, f"seed {seed}"


@pytest.mark.parametrize("seed", range(60))
def test_hallucinated_matches_the_inline_returns(seed):
    rnd = random.Random(seed)
    content = rnd.choice(_CONTENT)
    reasoning = rnd.choice(_REASON)
    finish = rnd.choice(_FINISH)
    executed = [{"name": f"t{i}"} for i in range(rnd.randint(0, 3))]

    expected = oracle_hallucinated(content, reasoning, finish, executed)
    got = refuse_hallucinated_tools(content_buf=content, reasoning_buf=reasoning,
                                    finish_reason=finish, executed_tools=list(executed))
    assert got == expected, f"seed {seed}"


# --------------------------------------------------------------------------
# the call site
# --------------------------------------------------------------------------

def _call_llama():
    tree = ast.parse((BOT / "call_llama.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")


def test_call_llama_delegates_to_the_module():
    fn = _call_llama()
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert {"finish_without_tool_calls", "refuse_hallucinated_tools"} <= called


def test_the_hallucination_warning_survives_at_the_call_site():
    """The log line is the only record that the loop aborted on a phantom
    tool. Operators grep for it, so moving the branches out must not move
    the log -- and a future refactor must not quietly drop it."""
    fn = _call_llama()
    found = any(
        isinstance(n, ast.Call) and "log.warning" in ast.unparse(n)
        and "hallucinated" in ast.unparse(n)
        for n in ast.walk(fn))
    assert found, "the hallucination warning left call_llama"


def test_the_two_loop_branches_are_in_order():
    """no-tool-calls first, then the hallucination guard.

    Order matters: the second branch is only reachable when tool_calls is
    non-empty, so swapping them would send every normal turn down the
    hallucination message.

    Scoped to the two branches that delegate to this module. A first
    version matched every `if not tool_calls` in call_llama and found
    three -- the third being the fallback ending, which is also handled
    here now but is not part of the loop body.
    """
    fn = _call_llama()
    pairs = []
    delegating = {"finish_without_tool_calls", "refuse_hallucinated_tools"}
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        # any direct return, not just the first statement: the
        # hallucination branch logs before returning.
        for stmt in node.body:
            if (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id in delegating):
                pairs.append((node.lineno, stmt.value.func.id))
    pairs.sort()
    assert [name for _, name in pairs] == ["finish_without_tool_calls",
                                           "refuse_hallucinated_tools"], \
        f"branch order changed: {pairs}"


def test_all_three_endings_are_delegated():
    """No `if not tool_calls:` branch in call_llama decides on its own any
    more. Three existed inline; all three are here."""
    fn = _call_llama()
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert {"finish_fallback_empty", "finish_without_tool_calls",
            "refuse_hallucinated_tools"} <= called


# --------------------------------------------------------------------------
# the fallback ending -- the third site
# --------------------------------------------------------------------------

def test_fallback_returns_content_as_a_bare_string():
    """Unlike the other two, this path returns a string in every case.
    It is already a degraded retry and the caller hands the value straight
    to Telegram, so there is no ledger to attach."""
    got = finish_fallback_empty(content="ответ", reasoning="",
                                finish_reason="stop")
    assert got == "ответ"
    assert isinstance(got, str)


def test_fallback_handles_none_without_crashing():
    """msg.get() returns None for both keys when the engine omits them,
    which is exactly the case this branch exists for."""
    got = finish_fallback_empty(content=None, reasoning=None,
                                finish_reason="stop")
    assert got == "[fallback empty: finish_reason=stop]"


def test_fallback_explains_an_empty_content_with_reasoning():
    got = finish_fallback_empty(content="", reasoning="размышления",
                                finish_reason="stop")
    assert "fallback non-stream: content пустой" in got
    assert got.endswith("размышления")


def test_fallback_caps_its_reasoning_tail_too():
    got = finish_fallback_empty(content=None, reasoning="q" * 9000,
                                finish_reason="stop")
    tail = got.split("\n\n", 1)[1]
    assert len(tail) == REASONING_TAIL_CHARS


def test_fallback_counts_the_full_reasoning_not_the_tail():
    got = finish_fallback_empty(content=None, reasoning="w" * 5000,
                                finish_reason="stop")
    assert "reasoning_chars=5000" in got


def test_the_fallback_message_is_not_the_loop_message():
    a = finish_fallback_empty(content="", reasoning="", finish_reason="stop")
    b = finish_without_tool_calls(content_buf="", reasoning_buf="",
                                  finish_reason="stop", executed_tools=[])
    assert a != b, "three different failures now share one message"


def test_the_returned_value_goes_straight_back():
    """Both branches return what the module returns, with no wrapper. A
    wrapper here would be the first sign of someone re-flattening the
    mixed dict/str contract."""
    fn = _call_llama()
    delegating = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)
                  and isinstance(n.value.func, ast.Name)
                  and n.value.func.id in {"finish_without_tool_calls",
                                          "refuse_hallucinated_tools"}]
    assert len(delegating) == 2, f"expected two direct returns, got {len(delegating)}"