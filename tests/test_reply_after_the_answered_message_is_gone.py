"""The answer must survive the deletion of the message it answers.

The delete tool can remove the very message the bot is about to reply to:
"delete the message I just wrote" is a normal request, and the bot
answers it in the same turn. Telegram then refuses the reply, because the
quoted message is not there any more:

    telegram.error.BadRequest: Message to be replied not found

On 2026-10-26 that refusal was raised out of the send helper, the
handler caught it and tried to send a failure notice the same way, and
the chat got a wall of the same traceback instead of an answer. The
operator saw a deleted message and no reply.

Telegram has the answer for this case: `allow_sending_without_reply` --
send the message even if the quoted one is not found. The quote is lost,
the answer is not.
"""
import asyncio

import pytest
from telegram.error import BadRequest

from handlers import _reply


class _FakeMessage:
    """A message whose reply_text fails the way Telegram fails."""

    def __init__(self, *, fail_first=True, error=None):
        self.message_id = 3313
        self.message_thread_id = 25
        self.calls = []
        self.fail_first = fail_first
        self.error = error

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, dict(kwargs)))
        if self.fail_first and len(self.calls) == 1:
            raise BadRequest(self.error or "Message to be replied not found")
        return f"sent:{text}"


class _FakeChat:
    id = -1004461679108
    type = "supergroup"
    is_forum = True


def _update(message):
    return type("U", (), {"message": message, "effective_chat": _FakeChat()})()


@pytest.mark.asyncio
async def test_the_answer_is_sent_without_the_quote_when_the_message_is_gone():
    msg = _FakeMessage()
    result = await _reply(_update(msg), "deleted, as asked")
    assert result == "sent:deleted, as asked"
    assert len(msg.calls) == 2
    assert msg.calls[1][1].get("allow_sending_without_reply") is True


@pytest.mark.asyncio
async def test_the_topic_is_kept_when_the_quote_is_dropped():
    """Without the thread id the answer would land in the General topic
    instead of the one the user is in."""
    msg = _FakeMessage()
    await _reply(_update(msg), "hello")
    assert msg.calls[1][1].get("message_thread_id") == 25


@pytest.mark.asyncio
async def test_another_bad_request_is_not_swallowed():
    """Only the missing-quote refusal gets the fallback. A message that
    is too long, or a chat the bot may not write to, must still be
    raised, or the bot would answer with something the user cannot see."""
    msg = _FakeMessage(error="Message text is empty")
    with pytest.raises(BadRequest):
        await _reply(_update(msg), "hello")
    assert len(msg.calls) == 1


@pytest.mark.asyncio
async def test_a_normal_reply_is_not_retried():
    """The fallback must not turn every reply into two sends."""
    msg = _FakeMessage(fail_first=False)
    result = await _reply(_update(msg), "hello")
    assert result == "sent:hello"
    assert len(msg.calls) == 1
    assert "allow_sending_without_reply" not in msg.calls[0][1]
