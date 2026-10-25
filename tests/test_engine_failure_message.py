"""What the user reads when the engine could not be reached.

The bug this file pins: the bot answered a dead engine with a Python
repr.

    [both streaming and non-streaming failed: stream_err=ConnectError(
    'All connection attempts failed'), fallback_err=ConnectError(...)]

That is what the user saw in the chat on 2026-10-25, while the engine
was down. It says nothing in a language a user can act on, and it can
carry the engine's own address -- the internal endpoint is not the
user's business.

So the rule this file pins:

  * the answer the user reads is a sentence, chosen by the kind of
    failure;
  * it carries no exception repr, no class name and no URL;
  * the technical detail is still written to the log, because that is
    where the operator reads it.

The end-to-end tests drive the real streaming branch and the real
error classifier. Feeding a hand-made failure string to a formatter
would prove the formatter and nothing else -- the defect lived in the
branch that built the string, not in the wording.
"""
import sys
from pathlib import Path

import httpx
import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import user_notice as lf  # noqa: E402
import llm_nonstream as lns  # noqa: E402
import llm_stream as ls  # noqa: E402

#: Things that must never appear in the text the user reads.
FORBIDDEN = ("ConnectError", "Traceback", "http://", "https://",
             "llama-server", "httpx", "Error(", "Exception")


def _assert_is_human(message: str) -> None:
    assert message, "the user must get something"
    for needle in FORBIDDEN:
        assert needle not in message, (
            f"the user's copy carries {needle!r}: {message!r}")


# --- the classification ----------------------------------------------


def test_a_dead_engine_says_the_engine_is_unreachable():
    message = lf.engine_failure_message(
        httpx.ConnectError("All connection attempts failed"))
    assert message == lf.ENGINE_UNREACHABLE_MESSAGE
    _assert_is_human(message)


def test_a_connect_timeout_is_unreachable_too():
    """A connect timeout is the engine not being there, not the engine
    being slow -- the advice is different."""
    message = lf.engine_failure_message(
        httpx.ConnectTimeout("timed out"))
    assert message == lf.ENGINE_UNREACHABLE_MESSAGE


def test_a_read_timeout_asks_for_a_shorter_question():
    """The engine was there and did not answer in time. That is a
    different problem with a different advice."""
    message = lf.engine_failure_message(
        httpx.ReadTimeout("timed out"))
    assert message == lf.ENGINE_TIMEOUT_MESSAGE
    _assert_is_human(message)


def test_an_error_from_the_engine_says_so():
    message = lf.engine_failure_message(
        httpx.HTTPError("500 Server Error"), status=500)
    assert message == lf.ENGINE_ERROR_MESSAGE
    _assert_is_human(message)


def test_an_unknown_failure_still_says_something_a_user_can_read():
    message = lf.engine_failure_message(ValueError("something odd"))
    assert message == lf.ENGINE_ERROR_MESSAGE
    _assert_is_human(message)


# --- the real paths ---------------------------------------------------


class _DeadStream:
    """A client whose every attempt to reach the engine fails."""

    def stream(self, *_a, **_kw):
        raise httpx.ConnectError("All connection attempts failed")

    async def post(self, *_a, **_kw):
        raise httpx.ConnectError("All connection attempts failed")


@pytest.mark.asyncio
async def test_the_streaming_branch_answers_with_a_sentence():
    """The real branch, with a real dead client.

    This is the path the user hit: the streaming attempt failed, the
    non-streaming fallback failed, and the terminal became the answer.
    """
    outcome = await ls.stream_iteration(
        _DeadStream(), {"model": "m", "messages": []},
        iteration=0, abort_event=None, accumulated_reasoning="",
        thinking_msg=None, silent=False, throttle=object())

    assert outcome.is_terminal is True
    assert outcome.terminal, "the turn must end with something to say"
    _assert_is_human(outcome.terminal)
    assert outcome.terminal == lf.ENGINE_UNREACHABLE_MESSAGE


def test_the_error_classifier_answers_with_a_sentence():
    """The non-streaming path: the exception becomes the message."""
    step = lns.classify_error(
        httpx.ConnectError("All connection attempts failed"),
        {"model": "m"})

    assert step.retry_without_tools is False
    _assert_is_human(step.message)
    assert step.message == lf.ENGINE_UNREACHABLE_MESSAGE


def test_the_classifier_still_reports_the_status_and_the_body():
    """The user-facing change must not cost the operator the facts.

    The status and the body are what the log line and the diagnostics
    read; they are not the user's copy, but they are not nothing.
    """
    class _Resp:
        status_code = 500
        text = "internal error"

    class _WithResponse:
        response = _Resp()

    step = lns.classify_error(_WithResponse(), {"model": "m"})

    assert step.status_code == 500
    assert step.body == "internal error"
    assert step.message == lf.ENGINE_ERROR_MESSAGE


def test_a_500_with_tools_still_retries_without_them():
    """The retry path is unchanged: no message, so the fallback runs."""
    class _Resp:
        status_code = 500
        text = "grammar error"

    class _WithResponse:
        response = _Resp()

    step = lns.classify_error(
        _WithResponse(), {"model": "m", "tools": [{"type": "function"}]})

    assert step.retry_without_tools is True
    assert step.message == ""
