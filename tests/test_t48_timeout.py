"""T48 tests: _reply handles TimedOut without raising -- and without
sending a second copy.

The contract, 2026-10-28.  A `sendMessage` that times out was most
likely delivered: the timeout is in our read of the response, not in
Telegram's handling of the request.  The Bot API's sendMessage has no
idempotency key, so resending it delivers the SAME message a second
time under a new id.  Measured on production the same day: an answer
that went out as 22 messages was delivered as 30 -- 8 of them exact
copies -- because 8 sends timed out while Telegram was busy with the
burst.

So: a timeout is logged and the message is treated as delivered.  One
user turn, one message.  RetryAfter is different and still retries: a
429 is Telegram saying "not yet", and the message was NOT accepted.
"""
import asyncio
import sys
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot"))

# Skip these tests if telegram package is not installed in
# the test environment (it's installed in /app but not
# necessarily in the test runner).
try:
    from telegram.error import RetryAfter, TimedOut
except ImportError:
    import pytest
    pytest.skip("telegram package not importable in this env", allow_module_level=True)


from handlers import _reply


def _make_update():
    update = MagicMock()
    update.effective_chat.id = 123
    update.message.message_thread_id = None
    update.message.reply_text = AsyncMock()
    return update


def test_timedout_returns_none_instead_of_raising():
    """A TimedOut send does not raise: the caller must not turn it
    into a second "❌ Error: Timed out" message."""
    update = _make_update()
    update.message.reply_text = AsyncMock(side_effect=TimedOut("simulated"))

    with patch("handlers.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(_reply(update, "hello"))

    assert result is None, "expected None on TimedOut, not a raise"


def test_timedout_is_not_resent():
    """The one that matters: a timeout is NOT a second chance.

    Resending is a duplicate -- the Bot API has no idempotency key, so
    the message Telegram already accepted arrives again under a new id.
    Measured: 8 copies in one turn (2026-10-28, production).
    """
    update = _make_update()
    update.message.reply_text = AsyncMock(side_effect=TimedOut("simulated"))

    with patch("handlers.asyncio.sleep", new=AsyncMock()):
        asyncio.run(_reply(update, "hello"))

    assert update.message.reply_text.await_count == 1, \
        (f"a timed-out send must not be repeated; got "
         f"{update.message.reply_text.await_count} attempts")


def test_retry_after_is_still_retried():
    """A 429 is Telegram saying "not yet" -- the message was NOT
    accepted, so retrying it is correct and is not a duplicate."""
    update = _make_update()
    fake_msg = MagicMock()
    fake_msg.message_id = 999

    call_count = 0

    async def reply_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RetryAfter(5)
        return fake_msg

    update.message.reply_text = AsyncMock(side_effect=reply_side_effect)

    with patch("handlers.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(_reply(update, "hello"))

    assert result is fake_msg, f"expected successful message, got {result}"
    assert update.message.reply_text.await_count == 2
