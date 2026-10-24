"""How the files of an answer reach Telegram.

The content of a file is taken out of the message the user already
received, so the delivery path is where the content can be lost. Three
things are pinned here:

  * the document carries the topic id, so a file in a forum topic lands
    in that topic and not in General -- PTB's reply_* methods do not
    carry it through, which is why the text reply has its own helper;
  * a file that cannot be uploaded goes out as text instead, because
    the alternative is a question with no answer in it;
  * a file whose acknowledgement timed out is not sent twice.

The rate limit is the chat's own: three files in one answer are three
messages, and Telegram counts messages.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

from llm_artifact import Artifact  # noqa: E402
from llm_files import send_files  # noqa: E402

CHAT_ID = -100999


def _update(thread_id=None, reply_document=None, reply_text=None):
    """An update whose message records what was sent.

    `message_thread_id` is set EXPLICITLY: on a bare MagicMock every
    attribute is a truthy mock, so an unset one would look like a forum
    topic and the topic-id rule would be tested against a fiction.
    """
    update = MagicMock()
    update.effective_chat.id = CHAT_ID
    message = MagicMock()
    message.message_thread_id = thread_id
    message.reply_document = reply_document or AsyncMock(return_value=MagicMock())
    message.reply_text = reply_text or AsyncMock(return_value=MagicMock())
    update.message = message
    return update


FILE = Artifact("index.html", "<html>hi</html>", "html")


@pytest.mark.asyncio
async def test_a_file_is_sent_as_a_document_with_its_name():
    update = _update()
    await send_files(update, [FILE])
    update.message.reply_document.assert_awaited_once()
    kwargs = update.message.reply_document.await_args.kwargs
    document = kwargs["document"]
    assert document.filename == "index.html"
    assert document.input_file_content == b"<html>hi</html>"
    assert kwargs["caption"] == "📄 index.html"


@pytest.mark.asyncio
async def test_the_topic_id_goes_with_the_document():
    update = _update(thread_id=42)
    await send_files(update, [FILE])
    kwargs = update.message.reply_document.await_args.kwargs
    assert kwargs["message_thread_id"] == 42


@pytest.mark.asyncio
async def test_a_message_without_a_topic_carries_no_topic_id():
    """In the General topic the id is absent, and General is where the
    message was."""
    update = _update(thread_id=None)
    await send_files(update, [FILE])
    kwargs = update.message.reply_document.await_args.kwargs
    assert "message_thread_id" not in kwargs


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_uploaded_goes_out_as_text():
    """The content was taken out of the message the user has already
    received, so a failed upload must not lose it."""
    update = _update(reply_document=AsyncMock(side_effect=RuntimeError("boom")))
    await send_files(update, [FILE])
    update.message.reply_text.assert_awaited_once()
    sent = update.message.reply_text.await_args.args[0]
    assert "<html>hi</html>" in sent
    assert "index.html" in sent


@pytest.mark.asyncio
async def test_a_flood_wait_is_retried():
    from telegram.error import RetryAfter

    calls = {"n": 0}

    async def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryAfter(0)
        return MagicMock()

    update = _update(reply_document=AsyncMock(side_effect=_flaky))
    await send_files(update, [FILE])
    assert calls["n"] == 2
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_timeout_is_not_sent_twice():
    """Telegram usually delivered it; a second copy is worse than a
    missing one."""
    from telegram.error import TimedOut

    update = _update(reply_document=AsyncMock(side_effect=TimedOut()))
    await send_files(update, [FILE])
    assert update.message.reply_document.await_count == 1
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_failing_file_does_not_stop_the_others():
    update = _update(reply_document=AsyncMock(side_effect=RuntimeError("boom")))
    await send_files(update, [FILE, Artifact("style.css", "body {}", "css")])
    assert update.message.reply_text.await_count == 2


@pytest.mark.asyncio
async def test_an_empty_file_is_not_sent():
    update = _update()
    await send_files(update, [Artifact("empty.txt", "", "text")])
    update.message.reply_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_files_means_no_messages():
    update = _update()
    await send_files(update, [])
    update.message.reply_document.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_file_over_the_cap_goes_out_as_text():
    """An answer that turned into a 50 MB file was not a file anybody
    asked for, and the text fallback is kinder than a stalled upload."""
    update = _update()
    huge = Artifact("big.txt", "x" * (10 * 1024 * 1024 + 1), "text")
    await send_files(update, [huge])
    update.message.reply_document.assert_not_awaited()
    update.message.reply_text.assert_awaited()
