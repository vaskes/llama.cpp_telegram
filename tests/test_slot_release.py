"""B1: the per-user slot must come back when a handler fails early.

The reviewer filed this as an unguarded `_abort_events.pop(... thinking.message_id)`
in three handlers' finally blocks. Fixing that was correct and necessary, but
it was the smaller half. Measuring the handlers showed the real shape:

    sem = await _check_user_slot(...)          # slot acquired
    if sem is None: return
    await update.message.chat.send_action(...) # <-- not covered by any finally
    ...up to three more awaits...
    try:
        ...
    finally:
        sem.release()

Everything between the acquisition and the try was outside the release. An
exception there -- Telegram rejecting the typing indicator, a malformed
update -- discarded the slot permanently. _PER_USER_SEMAPHORE_LIMIT is 2, so
two such failures lock a user out of the bot until the process restarts.

This test injects the failure at the typing indicator and counts the slot.
The AST version of this check was wrong twice: it first reported four leaking
handlers when only three leak, then reported handle_text's window as empty
when it contains _reply() and turn.persist(). Both errors came from reading
the code instead of running it, so the assertion here is a semaphore counter.
"""
import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

HANDLERS = ("handle_photo", "handle_voice", "handle_document", "handle_text")


class _InjectedFailure(RuntimeError):
    """Stands in for Telegram's 'message thread not found'."""


def _fake_update():
    class Chat:
        id = 777
        type = "private"

        async def send_action(self, *a, **kw):
            # The first await after the slot is acquired, in every handler.
            raise _InjectedFailure("telegram: message thread not found")

    msg = types.SimpleNamespace(
        chat=Chat(),
        from_user=types.SimpleNamespace(id=1, username="u", first_name="A",
                                        last_name="B"),
        message_id=555,
        message_thread_id=None,
        text="hi",
        caption=None,
        caption_entities=[],
        entities=[],
        date=None,
        reply_to_message=None,
        reply_text=None,
        photo=None,
        document=None,
        voice=None,
        audio=None,
        video_note=None,
        sticker=None,
    )
    return types.SimpleNamespace(
        message=msg, effective_user=msg.from_user, effective_chat=msg.chat,
        callback_query=None, message_thread_id=None,
    )


async def _run_handler(monkeypatch, handlers, name, limit):
    fn = getattr(handlers, name)

    sem = asyncio.Semaphore(limit)
    monkeypatch.setattr(handlers, "_route_to_thread",
                        _returns((777, None, False)), raising=False)
    monkeypatch.setattr(handlers, "_check_user_slot", _acquires(sem),
                        raising=False)
    # handle_text deliberately swallows a send_action failure (it logs and
    # carries on), so arming only the typing indicator would never fail that
    # handler inside the window. Arm the thinking reply too -- whichever
    # fires first, the slot must come back.
    monkeypatch.setattr(handlers, "_reply", _raises(), raising=False)

    try:
        await fn(_fake_update(), types.SimpleNamespace(
            bot=types.SimpleNamespace(data=None)))
    except Exception:
        pass

    return sem


def _raises():
    async def _f(*a, **kw):
        raise _InjectedFailure("telegram: thinking message rejected")
    return _f


def _returns(value):
    async def _f(*a, **kw):
        return value
    return _f


def _acquires(sem):
    async def _f(chat_id, user_id, update):
        await sem.acquire()
        return sem
    return _f


@pytest.mark.parametrize("name", HANDLERS)
def test_slot_is_returned_when_the_handler_fails_in_the_window(
        name, monkeypatch):
    import handlers

    # state.py owns the value; config re-exports it. Reading it from handlers
    # with a getattr default of 2 is what made the AST checker confidently
    # quote "=2" for a constant it had never actually read.
    from state import _PER_USER_SEMAPHORE_LIMIT as limit
    assert limit >= 1, f"limit={limit} would make the test vacuous"

    sem = asyncio.run(_run_handler(monkeypatch, handlers, name, limit))

    assert sem._value == limit, (
        f"{name} leaked a per-user slot: {sem._value}/{limit} left after an "
        f"injected failure at the typing indicator. The user cannot get "
        f"another slot until the process restarts."
    )


def test_the_limit_is_two_so_the_lockout_is_two_failures():
    """Pins the severity. If this ever goes up, the bug above is quieter,
    not smaller."""
    from state import _PER_USER_SEMAPHORE_LIMIT as limit

    assert limit == 2