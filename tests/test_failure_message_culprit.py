"""A refusal names the culprit.

The incident this file pins (production, 2026-10-28): the operator asked
the bot "хочешь сделаем тебе edit_messages?" and the bot answered

    ❌ Something broke on the bot's side. Try again — if it keeps
    happening, tell the admin: the details are in the logs.

while the log said

    [handle_text] send_action FAILED: NetworkError: NetworkError(
    'httpx.ConnectError: ')
    [ERR text] NetworkError: httpx.ConnectError:

Nothing was wrong with the bot's side: Telegram's API was not
answering. The sentence pointed the operator at a broken build and at
the wrong question (edit_messages) instead of at a flaky connection,
and the technical detail -- the only place it belonged -- was in the
log, where the operator was not looking.

So the rule this file pins:

  * the sentence the user reads names WHICH of the three failed: the
    engine, the connection to Telegram, or the bot itself;
  * Telegram's transport errors are not engine errors: PTB wraps them
    in its own class, and they used to fall into the generic bucket;
  * the sentence carries no exception repr, no class name, no URL --
    the detail stays in the log;
  * the handlers actually use this classifier, so the wording cannot
    drift away from the tree.
"""
import sys
from pathlib import Path

import httpx
import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import user_notice as lf  # noqa: E402
from telegram import error as telegram_error  # noqa: E402


#: What must never appear in the text the user reads.
FORBIDDEN = ("ConnectError", "Traceback", "http://", "https://",
             "llama-server", "httpx", "Error(", "Exception", "telegram.error")


def _assert_is_human(message: str) -> None:
    assert message, "the user must get something"
    for needle in FORBIDDEN:
        assert needle not in message, (
            f"the user's copy carries {needle!r}: {message!r}")


# --- the three culprits ------------------------------------------------


def test_a_dead_telegram_is_named_as_telegram():
    """The case the generic sentence got wrong."""
    message = lf.failure_message(
        telegram_error.NetworkError("httpx.ConnectError: "))
    assert message == lf.TELEGRAM_UNREACHABLE_MESSAGE
    assert message != lf.GENERIC_FAILURE_MESSAGE
    _assert_is_human(message)


def test_a_telegram_timeout_is_telegram_too():
    message = lf.failure_message(telegram_error.TimedOut("timed out"))
    assert message == lf.TELEGRAM_UNREACHABLE_MESSAGE


def test_a_telegram_that_answers_with_an_error_is_not_a_transport_failure():
    """BadRequest is an answer from Telegram, not a broken connection.

    The connection worked; the request did not. That is a different
    story with a different owner.
    """
    message = lf.failure_message(
        telegram_error.BadRequest("Message to be replied not found"))
    assert message == lf.GENERIC_FAILURE_MESSAGE


def test_a_dead_engine_is_still_the_engine():
    message = lf.failure_message(
        httpx.ConnectError("All connection attempts failed"))
    assert message == lf.ENGINE_UNREACHABLE_MESSAGE


def test_a_slow_engine_is_still_the_engine():
    message = lf.failure_message(httpx.ReadTimeout("too slow"))
    assert message == lf.ENGINE_TIMEOUT_MESSAGE


def test_anything_else_is_the_bot_itself():
    message = lf.failure_message(ValueError("something odd"))
    assert message == lf.GENERIC_FAILURE_MESSAGE
    _assert_is_human(message)


# --- the tree uses it --------------------------------------------------


def test_the_handlers_ask_the_classifier():
    """The wording cannot drift away from the tree.

    Four handlers catch everything and answer the user. They must go
    through the classifier, not carry the generic sentence themselves.
    """
    src = (BOT_DIR / "handlers.py").read_text(encoding="utf-8")
    assert "GENERIC_FAILURE_MESSAGE" not in src, (
        "a handler still sends the generic sentence directly; it cannot "
        "name the culprit that way")
    assert src.count("failure_message(e)") == 4, (
        "the four catch-all handlers must each ask the classifier for "
        "the sentence; the count changed")


def test_the_telegram_message_says_what_the_user_can_do():
    """A sentence with no advice in it is a sentence with no point."""
    assert "try again" in lf.TELEGRAM_UNREACHABLE_MESSAGE.lower()
