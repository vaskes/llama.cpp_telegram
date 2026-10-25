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

Wording is Russian because the bot's other user-facing strings are
Russian (`config.EMPTY_RESPONSE_USER_FALLBACK`,
`context_budget.OVERFLOW_MESSAGE`, the thinking indicator), and a bot
that switches languages depending on how it failed reads as two
different products.
"""
from __future__ import annotations

import httpx

__all__ = [
    "ENGINE_UNREACHABLE_MESSAGE",
    "ENGINE_TIMEOUT_MESSAGE",
    "ENGINE_ERROR_MESSAGE",
    "GENERIC_FAILURE_MESSAGE",
    "DOWNLOAD_FAILURE_MESSAGE",
    "TOPIC_CREATE_FAILURE_MESSAGE",
    "TOPIC_DELETE_FAILURE_MESSAGE",
    "engine_failure_message",
]

#: The engine did not answer the connection. It is down, restarting, or
#: not where the bot thinks it is.
ENGINE_UNREACHABLE_MESSAGE = (
    "🛠 Бот не смог связаться с языковым движком — он сейчас недоступен.\n\n"
    "Обычно это значит, что движок выключен или перезапускается. "
    "Попробуй ещё раз через минуту — если бот отвечает, а вопрос "
    "длинный, попробуй сократить его."
)

#: The engine was there, but did not answer in time.
ENGINE_TIMEOUT_MESSAGE = (
    "⏳ Языковой движок не ответил вовремя.\n\n"
    "Попробуй ещё раз, а если вопрос длинный или контекст огромный — "
    "сократи его или начни новый диалог через /reset."
)

#: The engine answered, and the answer was an error.
ENGINE_ERROR_MESSAGE = (
    "⚠️ Языковой движок ответил ошибкой — ответ не получен.\n\n"
    "Попробуй ещё раз через минуту. Если повторяется — сообщи "
    "администратору: в логах есть подробности."
)

#: Something broke in the bot itself, not in the engine. The handlers'
#: catch-all replies used to carry the exception's own text.
GENERIC_FAILURE_MESSAGE = (
    "❌ Что-то сломалось на стороне бота. Попробуй ещё раз — если "
    "повторяется, сообщи администратору: в логах есть подробности."
)

#: The file could not be pulled from Telegram.
DOWNLOAD_FAILURE_MESSAGE = (
    "❌ Не удалось скачать файл. Попробуй ещё раз — если файл "
    "большой, отправь его ещё раз поменьше."
)

#: The sub-talk topic could not be created.
TOPIC_CREATE_FAILURE_MESSAGE = (
    "❌ Не удалось создать под-топик. Попробуй ещё раз."
)

#: The sub-talk topic could not be deleted.
TOPIC_DELETE_FAILURE_MESSAGE = (
    "❌ Не удалось удалить под-топик. Попробуй ещё раз."
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
