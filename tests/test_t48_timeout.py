"""T48 tests: _reply handles TimedOut without raising."""
import asyncio
import sys
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot"))

# Skip these tests if telegram package is not installed in
# the test environment (it's installed in /app but not
# necessarily in the test runner).
try:
    from telegram.error import TimedOut
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


def test_timedout_all_attempts_return_none():
    """When all 3 TimedOut attempts fail, _reply returns None
    instead of raising. This prevents the duplicate
    "❌ Error: Timed out" message."""
    update = _make_update()
    update.message.reply_text = AsyncMock(side_effect=TimedOut("simulated"))

    with patch("handlers.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(_reply(update, "hello"))

    assert result is None, "expected None on TimedOut exhaustion"
    assert update.message.reply_text.await_count == 3, \
        f"expected 3 attempts, got {update.message.reply_text.await_count}"


def test_timedout_then_success():
    """If one of the retries succeeds, _reply returns the
    successful message (no None)."""
    update = _make_update()
    fake_msg = MagicMock()
    fake_msg.message_id = 999

    call_count = 0
    async def reply_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise TimedOut("simulated")
        return fake_msg

    update.message.reply_text = AsyncMock(side_effect=reply_side_effect)

    with patch("handlers.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(_reply(update, "hello"))

    assert result is fake_msg, f"expected successful message, got {result}"
    assert update.message.reply_text.await_count == 2

