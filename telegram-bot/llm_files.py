"""Sending the files an answer carries.

The answer arrives as prose plus a list of files
(`llm_artifact.extract_files`).  The prose goes to the chat the way
every other answer does; each file goes out as its own Telegram
document, because a file in a chat is a file the user can open, and a
code block in a chat is a wall of text they have to scroll.

Three decisions are worth stating, because each of them is a way for
the content to be lost:

  * the file is sent as a *reply* to the message that asked for it,
    and it carries the topic id, so a file in a forum topic lands in
    that topic and not in General.  PTB's reply_* methods do not carry
    the topic id through, which is why the text reply has its own
    helper in `handlers._reply`; this is the same rule for the
    document path;
  * a file that cannot be uploaded is sent as *text* instead.  The
    content was taken out of the message the user already received, so
    a failed upload that dropped the content would leave a question
    with no answer in it.  A wall of code in the chat is the lesser
    evil;
  * a file that Telegram accepted but whose acknowledgement timed out
    is not re-sent.  The message is probably there, and a second
    copy is worse than a missing one.

The rate limit is the chat's own, the same one the text reply goes
through: three files in one answer are three messages, and Telegram
counts messages, not answers.
"""
from __future__ import annotations

import asyncio

from chat_utils import TELEGRAM_TEXT_CHUNK, _rate_limit_send
from logconf import get_logger

log = get_logger(__name__)

__all__ = ["send_files", "deliver_staged"]

#: Telegram's own document limit is 50 MB.  This cap is not about
#: Telegram: an answer that turned into a 50 MB file was not a file
#: anybody asked for, and the text fallback is kinder than a stalled
#: upload.
_MAX_FILE_BYTES = 10 * 1024 * 1024

#: How much of a file goes out per message when the file has to be
#: sent as text.  The same number the text answer uses, because it is
#: the same limit: Telegram rejects a message over 4096 characters.
_TEXT_CHUNK = TELEGRAM_TEXT_CHUNK


async def send_files(update, files) -> None:
    """Send every file of an answer as its own document.

    A failure to send one file does not stop the others: the user
    asked for the content, and one bad file is not a reason to drop
    the rest of the answer on the floor.

    The files the model staged during the turn go out with the answer,
    after it: the operator's rule of 2026-10-28 -- a file made for the
    operator leaves with the answer to the user, not in the middle of
    the turn, where it used to land between two thoughts of a reasoning
    that was still streaming.
    """
    for artifact in files or ():
        try:
            await _send_one(update, artifact)
        except Exception:
            log.exception(f"[send_files] {artifact.filename} could not be sent")
    await deliver_staged(update, _turn_key(update))


def _turn_key(update) -> tuple:
    """The turn a staged file belongs to: the chat and the user's message.

    The tool that staged the file knows the same pair, so the two meet
    here.  The message id is what keeps two turns of the same chat
    apart.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    message_id = getattr(getattr(update, "message", None), "message_id", None)
    return (chat_id, message_id)


async def deliver_staged(update, key) -> None:
    """The files the model staged during the turn, sent with the answer.

    `key` is the turn: (chat id, the id of the user's message).  These
    are the files the model asked the sandbox for -- it fetched them
    over the file door and put them on the shelf, because a file that
    goes out in the middle of a turn lands in the chat while the user
    is still reading the model's reasoning (the operator's rule of
    2026-10-28).

    They go out the way the sandbox's `send_file` always sent them: no
    caption, a reply to the message that asked for them, in the topic
    the question came from.  The channel's own id for the file is
    written back into the tool's ledger, which is where it belongs --
    the id exists only after the delivery.
    """
    import llm_staged

    for item in llm_staged.take(key):
        try:
            await _send_staged_one(update, item)
        except Exception:
            log.exception(f"[send_files] {item.name} could not be sent")


async def _send_staged_one(update, item) -> None:
    """One staged file, as a document."""
    from telegram import InputFile

    kwargs = _thread_kwargs(update)
    try:
        with open(item.path, "rb") as handle:
            sent = await update.message.reply_document(
                document=InputFile(handle, filename=item.name), **kwargs)
        file_id = getattr(getattr(sent, "document", None), "file_id", None)
        if file_id and isinstance(item.ledger, dict):
            item.ledger["file_id"] = file_id
        log.info(f"[send_files] {item.name} ({item.size} bytes) went out "
                 f"with the answer")
    finally:
        try:
            import os

            os.unlink(item.path)
        except OSError:
            pass


async def _send_one(update, artifact) -> None:
    """One file, as a document."""
    from telegram import InputFile
    from telegram.error import RetryAfter, TimedOut

    payload = artifact.content.encode("utf-8")
    if not payload:
        return
    kwargs = _thread_kwargs(update)
    if len(payload) > _MAX_FILE_BYTES:
        log.warning(f"[send_files] {artifact.filename} is {len(payload)} bytes, "
                    f"over the cap; sending it as text")
        await _send_as_text(update, artifact, kwargs)
        return
    document = InputFile(payload, filename=artifact.filename)
    for attempt in range(3):
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None:
            await _rate_limit_send(chat_id)
        try:
            await update.message.reply_document(
                document=document,
                caption=f"📄 {artifact.filename}",
                **kwargs,
            )
            return
        except RetryAfter as e:
            wait_s = float(getattr(e, "retry_after", 1)) + 1.0
            log.info(f"[send_files] RetryAfter on {artifact.filename}, "
                     f"sleeping {wait_s:.1f}s")
            await asyncio.sleep(wait_s)
        except TimedOut:
            log.info(f"[send_files] {artifact.filename} timed out; Telegram "
                     f"most likely delivered it")
            return
        except Exception as e:
            log.exception(f"[send_files] {artifact.filename} failed: "
                          f"{type(e).__name__}: {e}")
            await _send_as_text(update, artifact, kwargs)
            return
    log.warning(f"[send_files] {artifact.filename} gave up after 3 attempts")
    await _send_as_text(update, artifact, kwargs)


async def _send_as_text(update, artifact, kwargs) -> None:
    """The file's content, as text.

    The content was taken out of the message the user has already
    received, so a failed upload must not lose it.
    """
    text = artifact.content
    if not text:
        return
    head = f"📄 {artifact.filename}\n"
    for index in range(0, len(text), _TEXT_CHUNK):
        chunk = text[index:index + _TEXT_CHUNK]
        prefix = head if index == 0 else ""
        try:
            await update.message.reply_text(prefix + chunk, **kwargs)
        except Exception:
            log.exception(f"[send_files] the text fallback for "
                          f"{artifact.filename} failed too")
            return


def _thread_kwargs(update) -> dict:
    """`message_thread_id` when the message is in a forum topic.

    In a forum supergroup the General topic has no message_thread_id
    of its own, and a missing id means General -- which is where the
    message was, so passing nothing is right there.
    """
    message = getattr(update, "message", None)
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is None:
        return {}
    return {"message_thread_id": thread_id}
