"""What a finished turn looks like on the way out of `call_llama`.

TECH-DEBT #13, first block. `call_llama` returns a string, or `None` when
the turn was aborted, or a dict when the model ran tools and the answer
needs the executed-tool trace in front of it.

That flattening existed seven times: byte-identical across the four
handlers' primary paths, and once more in each of the three
empty-response retries. A copy that forgets the preamble does not fail --
it answers without the tool trace and the user sees a reply that looks
like the model did nothing.

So this file pins the one function, and then pins the duplication being
GONE, which is the part that can come back: the last three tests fail if
a handler starts hand-rolling its own unwrap again.

`_format_executed_tools` is exercised by tests/test_t44.py and is
re-exported from here under its historical name; the two tests below
cover it only where `unwrap_llm_result` depends on it.
"""
import ast
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from llm_result import unwrap_llm_result  # noqa: E402

BASH = [{"name": "bash_command", "args": {"command": "ls -l"}}]


# --- the shape of the input ----------------------------------------------

def test_a_plain_string_passes_through():
    assert unwrap_llm_result("hello") == "hello"


def test_none_stays_none():
    """The aborted turn. Every caller branches on this immediately after.

    Returning "" instead would turn "the user clicked Stop" into "the
    model answered nothing", and the abort branch would never run.
    """
    assert unwrap_llm_result(None) is None


def test_a_dict_without_executed_tools_is_its_text():
    assert unwrap_llm_result({"text": "hi"}) == "hi"


def test_a_dict_with_a_missing_text_key_is_the_empty_string():
    """`.get("text") or ""`, not `.get("text")`. The old code did that and
    a regression here would surface as AttributeError on None downstream
    rather than as an empty answer."""
    assert unwrap_llm_result({"executed_tools": []}) == ""


def test_a_dict_with_explicit_null_text_is_the_empty_string():
    assert unwrap_llm_result({"text": None, "executed_tools": None}) == ""


# --- the preamble ---------------------------------------------------------

def test_a_tool_trace_is_prepended_to_the_answer():
    out = unwrap_llm_result({"text": "there are two files", "executed_tools": BASH})
    assert out == "```bash\n$ ls -l\n```\n\nthere are two files"


def test_tools_that_render_to_nothing_do_not_leave_a_stray_separator():
    """react_to_message is not surfaced. Prepending "" + "\\n\\n" would put
    a blank line at the top of every answer."""
    tools = [{"name": "react_to_message", "args": {"emoji": "\N{WHITE HEAVY CHECK MARK}"}}]
    assert unwrap_llm_result({"text": "ok", "executed_tools": tools}) == "ok"


def test_an_empty_executed_tools_list_adds_nothing():
    assert unwrap_llm_result({"text": "ok", "executed_tools": []}) == "ok"


def test_a_trace_lands_in_front_of_an_empty_answer_too():
    """The answer can be empty while the trace is not -- that is exactly
    the tool-only turn."""
    out = unwrap_llm_result({"text": "", "executed_tools": BASH})
    assert out == "```bash\n$ ls -l\n```\n\n"


def test_two_commands_keep_their_order():
    tools = [{"name": "bash_command", "args": {"command": "first"}},
             {"name": "bash_command", "args": {"command": "second"}}]
    out = unwrap_llm_result({"text": "done", "executed_tools": tools})
    assert out == "```bash\n$ first\n$ second\n```\n\ndone"


# --- the duplication staying gone ----------------------------------------

def _handler_tree(name):
    src = (BOT / "handlers.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in handlers.py")


@pytest.mark.parametrize("handler", [
    "handle_photo", "handle_text", "handle_document", "handle_voice"])
def test_no_handler_hand_rolls_the_unwrap(handler):
    """`isinstance(_result, dict)` in a handler is the old copy coming back.

    Parsed, not grepped: the string appears in this file's own docstring
    and in the module docstring of llm_result.py, and a substring check
    cannot tell a comment from a branch.
    """
    # Both arguments matter, and the second one is a Name: `dict` is a
    # builtin, not a literal, so `isinstance(x, ast.Constant)` never
    # matches it. That predicate shipped in the first draft and this
    # check could not have fired -- mutation "put the inline copy back in
    # one handler" passed 18/18 against it.
    #
    # The first argument alone is not enough either: it also matches
    # `isinstance(retry_response, str)` in the empty-response check.
    found = [n.lineno for n in ast.walk(_handler_tree(handler))
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "isinstance"
             and len(n.args) == 2
             and isinstance(n.args[0], ast.Name)
             and n.args[0].id in ("_result", "retry_response")
             and isinstance(n.args[1], ast.Name)
             and n.args[1].id == "dict"]
    assert not found, (
        f"{handler}: inline unwrap at {found}. It is now one function in "
        f"llm_result; a second copy is how the preamble goes missing in "
        f"one handler only"
    )


@pytest.mark.parametrize("handler", [
    "handle_photo", "handle_text", "handle_document", "handle_voice"])
def test_every_handler_unwraps_the_primary_result_exactly_once(handler):
    """Counting, not just "somewhere".

    Deleting one of the two primary calls in a handler leaves the other
    one -- so a test that only asks "is it called" is green while the
    handler now passes `call_llama`'s raw dict into the answer. That
    mutation survived the first version of this file too.

    Each handler has exactly one `use_stream`less primary POST. The
    empty-response retry is a separate `_result`-free call, and only
    three of the four have one.
    """
    calls = [n for n in ast.walk(_handler_tree(handler))
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "unwrap_llm_result"
             and n.args
             and isinstance(n.args[0], ast.Name)
             and n.args[0].id == "_result"]
    assert len(calls) == 1, (
        f"{handler}: {len(calls)} unwrap_llm_result(_result) calls, "
        f"expected 1 -- either the primary result reaches the answer "
        f"unflattened, or it is flattened twice"
    )