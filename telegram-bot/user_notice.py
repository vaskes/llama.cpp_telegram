"""What the user reads when something breaks.

One module, one rule: the chat gets a sentence, the log gets the
detail. Every place where a failure becomes the user's answer asks
this module for the wording.

The rule was written on 2026-10-25, after the operator wrote "выведи
hmtl" into the chat while the engine was down and the bot answered:

    [both streaming and non-streaming failed: stream_err=ConnectError(
    'All connection attempts failed'), fallback_err=ConnectError(...)]

That is a Python repr, not an answer. It says nothing in a language a
user can act on, and it can carry the engine's own address -- the
internal endpoint is not the user's business.

The same class of leak existed on five more paths, all of them visible
to the user:

  * the four handlers' catch-all replies (`❌ Error: {e}`);
  * the file download (`❌ Failed to download photo: {e}`);
  * the forum-topic create and delete (`❌ createForumTopic failed: {e}`).

Every one of them logged the exception properly -- `log.exception` with
the traceback -- and then ALSO pushed the raw text into the chat. The
log was never the problem. The chat was.

The engine failures are classified by the kind of failure, because that
is what the user can act on:

  * the engine is not listening -- the commonest cause by far, and the
    one an operator recognises instantly ("the engine is down");
  * the engine listened and did not answer in time -- a different
    problem with a different advice (shorten the question);
  * the engine answered with an error -- anything else.

Wording is English because the bot's own notices are English. That was
not always true: on 2026-10-25 the bot's user-facing copy was half
Russian and half English, sometimes inside one message (`/stats` had a
Russian heading over an English body). A bot that switches languages
depending on how it failed reads as two different products, so the
operator's call was one language for everything the user reads.
"""
from __future__ import annotations

import httpx

__all__ = [
    "ENGINE_UNREACHABLE_MESSAGE",
    "ENGINE_TIMEOUT_MESSAGE",
    "ENGINE_ERROR_MESSAGE",
    "GENERIC_FAILURE_MESSAGE",
    "TELEGRAM_UNREACHABLE_MESSAGE",
    "DOWNLOAD_FAILURE_MESSAGE",
    "TOPIC_CREATE_FAILURE_MESSAGE",
    "TOPIC_DELETE_FAILURE_MESSAGE",
    "engine_failure_message",
    "failure_message",
]

#: The engine did not answer the connection. It is down, restarting, or
#: not where the bot thinks it is.
ENGINE_UNREACHABLE_MESSAGE = (
    "🛠 The bot could not reach the language engine — it is unavailable "
    "right now.\n\n"
    "Usually that means the engine is down or restarting. Try again in a "
    "minute — if the bot answers but your question is long, try "
    "shortening it."
)

#: The engine was there, but did not answer in time.
ENGINE_TIMEOUT_MESSAGE = (
    "⏳ The language engine did not answer in time.\n\n"
    "Try again — and if the question is long or the context is huge, "
    "shorten it or start a new conversation with /reset."
)

#: The engine answered, and the answer was an error.
ENGINE_ERROR_MESSAGE = (
    "⚠️ The language engine returned an error — no answer was produced.\n\n"
    "Try again in a minute. If it keeps happening, tell the admin: the "
    "details are in the logs."
)

#: Something broke in the bot itself, not in the engine. The handlers'
#: catch-all replies used to carry the exception's own text.
GENERIC_FAILURE_MESSAGE = (
    "❌ Something broke on the bot's side. Try again — if it keeps "
    "happening, tell the admin: the details are in the logs."
)

#: The bot could not reach Telegram. Not the bot's own logic, and not the
#: engine: the transport to Telegram's API is down or flaky.
#:
#: Measured on 2026-10-28: the bot answered a question in the group with
#: "Something broke on the bot's side" while the log said
#: `telegram.error.NetworkError: httpx.ConnectError`. The generic wording
#: pointed the user at the wrong culprit -- the bot was fine, Telegram was
#: not answering -- and the operator read it as a broken build.
TELEGRAM_UNREACHABLE_MESSAGE = (
    "📡 The bot could not reach Telegram — the connection to Telegram's "
    "API is down or flaky right now. Nothing is wrong with your question: "
    "try again in a minute."
)

#: The file could not be pulled from Telegram.
DOWNLOAD_FAILURE_MESSAGE = (
    "❌ Could not download the file. Try again — and if the file is "
    "large, send a smaller one."
)

#: The sub-talk topic could not be created.
TOPIC_CREATE_FAILURE_MESSAGE = (
    "❌ Could not create the sub-talk topic. Try again."
)

#: The sub-talk topic could not be deleted.
TOPIC_DELETE_FAILURE_MESSAGE = (
    "❌ Could not delete the sub-talk topic. Try again."
)


def engine_failure_message(
    exc: BaseException, *, status: int | None = None
) -> str:
    """The sentence the user reads, chosen by the kind of failure.

    `status` is the HTTP status, when the failure carried one. It is
    not used to pick a different wording -- the three cases above are
    the ones a user can act on -- but it is part of the signature
    because the caller has it and the log line does not, and a future
    case ("the engine is up but said 429") will need it.
    """
    if isinstance(exc, httpx.ConnectError):
        return ENGINE_UNREACHABLE_MESSAGE
    if isinstance(exc, httpx.ConnectTimeout):
        # The connection never completed, so this is the same story as
        # a refused one: the engine is not taking connections. It is
        # checked before the general timeout because a connect timeout
        # says nothing about the length of the question.
        return ENGINE_UNREACHABLE_MESSAGE
    if isinstance(exc, httpx.TimeoutException):
        return ENGINE_TIMEOUT_MESSAGE
    return ENGINE_ERROR_MESSAGE


def failure_message(exc: BaseException) -> str:
    """The sentence the user reads when the bot caught a failure.

    It names the culprit, because there are three of them and the user
    can act on each differently: the engine is down, Telegram is not
    answering, or the bot itself broke. The generic sentence ("something
    broke on the bot's side") was true for the last one only, and it was
    the one the operator read when Telegram was the problem.

    Telegram's own transport errors come first: they are the case the
    generic wording got wrong, and they are not httpx errors -- PTB
    wraps the httpx exception in its own class.
    """
    if _is_telegram_transport_error(exc):
        return TELEGRAM_UNREACHABLE_MESSAGE
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                        httpx.TimeoutException)):
        return engine_failure_message(exc)
    return GENERIC_FAILURE_MESSAGE


def _is_telegram_transport_error(exc: BaseException) -> bool:
    """True when the failure was the transport to Telegram, not the bot.

    PTB's `NetworkError` (and `TimedOut`, which derives from it) means
    the request to Telegram's API did not complete: connection refused,
    reset, timed out.

    `BadRequest` derives from `NetworkError` in PTB -- a historical
    grouping, not a statement about the connection. It means the
    connection worked and the REQUEST was wrong ("Message to be replied
    not found"), so it is not a transport failure and must not be
    reported as "Telegram is unreachable".
    """
    try:
        from telegram import error as telegram_error
    except Exception:  # pragma: no cover - PTB is a hard dependency
        return False
    if isinstance(exc, telegram_error.BadRequest):
        return False
    return isinstance(exc, telegram_error.NetworkError)
