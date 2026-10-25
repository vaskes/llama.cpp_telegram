"""Regression tests for the two NameErrors the extraction left behind.

Both were live in production and both were invisible to the test suite,
for the same reason: they only fire on a path the tests do not walk.

1. selftest.py raised NameError on its first await, gated behind
   LLAMABOT_SELFTEST=1, which production never sets.
2. access.py called _reply on the semaphore-queue-timeout path. The call sat
   inside `except Exception: pass`, so the NameError was swallowed and the
   user got silence -- which is precisely the failure v0.10.2 added that line
   to prevent.

The graph tripwires in test_refactor_graph.py now catch this class statically.
These two are here because the static check is the reason they were found, and
that is worth pinning in a way that says what it replaced.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


@pytest.mark.asyncio
async def test_overload_timeout_reaches_the_reply_helper():
    """The whole point of the overload message is that the user sees it.

    Before the fix this path raised NameError and the bare
    `except Exception: pass` turned it back into the silent drop it was
    written to remove.
    """
    import access

    class _Sem:
        async def acquire(self):
            raise asyncio.TimeoutError

        def locked(self):
            return True

        def release(self):
            pass

    class _Msg:
        message_id = 1

    class _Chat:
        id = 42

    class _User:
        id = 42
        username = "someone"

    class _Update:
        message = _Msg()
        effective_user = _User()
        effective_chat = _Chat()

    calls = []

    async def _fake_reply(update, text, **kw):
        calls.append(text)

    # No timeout patch needed: _Sem.acquire raises TimeoutError immediately,
    # so wait_for returns before the real timeout is ever consulted.
    with patch.object(access, "_get_user_semaphore", lambda *a: _Sem()), \
         patch("handlers._reply", _fake_reply):
        result = await access._check_user_slot(42, 42, _Update())

    assert result is None, "a timed-out slot must not hand back a semaphore"
    assert calls, (
        "the overload message never reached _reply -- the call is broken "
        "again, and the silent `except` is hiding it"
    )
    assert "overloaded" in calls[0]