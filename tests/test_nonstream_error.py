import ast  # noqa: E402
from loop_owner import (call_source, loop_source,  # noqa: F401
                       loop_tree, run_one)
"""llm_nonstream.classify_error -- a failed POST, decided purely.

TECH-DEBT #8 block 4 (T3.3). The branch this replaces was

    if (getattr(_err_response, "status_code", None) == 500
            and req_body.get('tools')):
        return await tools_less_fallback(...)

inline in a handler that had to hold three things at once: which
exceptions carry `.response`, which status code means "the tool-call
output was malformed, try again", and whether this request offered tools
at all. None of it was reachable from a test that did not build a whole
`call_llama`.

`ErrorStep.retry_without_tools` is a flag, not a message, because the
two branches return different KINDS of thing. One hands back an await,
the other a string destined for Telegram. Collapsing both into one
field is the shape `NonStreamResult` was built to avoid.
"""
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

from llm_nonstream import classify_error  # noqa: E402

pytestmark = pytest.mark.tech_debt(
    item=8,
    reason="Pins TECH-DEBT #8 block 4: the failed non-streaming POST's "
           "three-way decision, as a pure function.")


class _Resp:
    def __init__(self, status, text="upstream said no"):
        self.status_code = status
        self.text = text


class _WithResponse(Exception):
    def __init__(self, response):
        super().__init__("boom")
        self.response = response


TOOLS = {"model": "m", "messages": [], "tools": [{"type": "function"}]}


def test_a_500_that_offered_tools_retries_once_without_them():
    """v0.8.1: the PEG grammar parser rejects the model's tool-call
    output, and the same prompt without tools usually parses."""
    step = classify_error(_WithResponse(_Resp(500)), TOOLS)
    assert step.retry_without_tools is True
    assert step.message == "", (
        "the retry path must not also carry a message; returning one "
        "would be the fallback never running")
    assert step.status_code == 500


def test_a_500_on_a_request_without_tools_does_not_retry():
    """Nothing to strip. Retrying would send the identical request."""
    step = classify_error(_WithResponse(_Resp(500)), {"model": "m"})
    assert step.retry_without_tools is False
    assert step.message == "[llama-server request failed: boom]"


@pytest.mark.parametrize("status", [400, 401, 404, 429, 502, 503])
def test_no_other_status_retries(status):
    """Only 500 has the grammar-parser story behind it.

    A 429 is a rate limit: retrying immediately makes it worse and the
    user waits longer for the same failure.
    """
    step = classify_error(_WithResponse(_Resp(status)), TOOLS)
    assert step.retry_without_tools is False
    assert "llama-server request failed" in step.message


def test_an_exception_without_a_response_is_still_classified():
    """ConnectionError and JSONDecodeError arrive with no `.response`.

    Reading `exc.response.status_code` unguarded raises AttributeError
    from inside the handler that is meant to be reporting the failure,
    so the user gets a traceback instead of an apology.
    """
    step = classify_error(ValueError("no response attached"), TOOLS)
    assert step.retry_without_tools is False
    assert step.status_code is None
    assert step.body == ""
    assert "no response attached" in step.message


def test_a_response_with_none_status_does_not_retry():
    step = classify_error(_WithResponse(_Resp(None)), TOOLS)
    assert step.retry_without_tools is False
    assert step.status_code is None


def test_the_body_is_truncated_before_it_reaches_the_log():
    """A whole HTML error page in one log line is a log nobody reads."""
    step = classify_error(_WithResponse(_Resp(500, "x" * 5000)), TOOLS)
    assert len(step.body) == 500


def test_an_empty_tools_list_is_not_an_offer():
    """`tools: []` is falsy and the model had nothing offered, so a 500
    is not the grammar parser."""
    step = classify_error(_WithResponse(_Resp(500)), {"tools": []})
    assert step.retry_without_tools is False


def test_the_call_site_awaits_the_retry_rather_than_returning_a_value():
    """C1: the await stays in call_llama.

    If it moves into the module, a failure in `tools_less_fallback`
    surfaces to a function that has no idea which iteration it was in.
    """
    import ast
    fn = run_one()
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "tools_less_fallback" in called
    assert "classify_error" in called
    for node in ast.walk(fn):
        if (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None)
                == "tools_less_fallback"):
            break
    else:
        pytest.fail("call_llama no longer awaits tools_less_fallback")


def test_the_non_stream_terminal_is_read_from_the_flag_not_the_text():
    """The last inline `terminal is not None` in the loop.

    On the abort path the turn ends with a legitimate None, and reading
    the value falls through into the dispatch loop.
    """
    import ast
    fn = run_one()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Compare) and node.ops
                and isinstance(node.ops[0], ast.IsNot)
                and isinstance(node.comparators[0], ast.Constant)
                and node.comparators[0].value is None):
            assert not (isinstance(node.left, ast.Attribute)
                        and node.left.attr == "terminal"), (
                f"call_llama:{node.lineno} still infers terminality from "
                f"the value; use is_terminal")