"""The Stop button must work for the person whose turn it is.

Production, 2026-10-23: the allowlist check sat at the top of
`cmd_callback`, so a Stop press from a group member who is NOT on the
allowlist was dropped before the handler ever looked at it --
`[SECURITY] rejected id=... msg=''` -- and the button did nothing,
silently, while the model kept generating the answer. The operator
pressed it 18 times. The member whose message the bot was answering
got a Stop button and a bot that ignored it.

The two gates answer different questions:

  * the allowlist: "may this id use the bot at all";
  * maker-taker:   "may this id stop THIS turn".

In group mode the first is deliberately "yes, anyone" -- `_route_to_thread`
says so: "Group mode: No whitelist check. Any member of the group can use
the bot." The bot answers that member and puts a Stop button on the
thinking message it sends them, so that button has to work for them.

These tests drive the handler, because the failure was invisible: the code
compiled, the docs promised the Stop button, and the suite was green.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

CHAT_ID = -100999
THINKING_MSG_ID = 4242
AUTHOR_ID = 777          # the member whose turn it is
STRANGER_ID = 555        # a different member of the same group
REFUSAL = "Only the author of the message can stop the processing."


def _stop_update(clicker_id=AUTHOR_ID, author_id=AUTHOR_ID, data="stop:thinking",
                 private=False):
    """A callback_query for the Stop button on the bot's thinking message.

    The thinking message is the bot's own "💭 thinking…" message, sent as a
    reply to the user's message -- so `reply_to_message.from_user.id` is
    the member whose turn it is.

    `is_forum` is set EXPLICITLY. chat_utils._is_group_chat reads it with
    getattr(chat, "is_forum", False), and on a bare MagicMock that
    attribute is a truthy mock -- so an unset mock looks like a forum
    supergroup and the group-mode early return swallows the branch under
    test. A test that passes because the code returned early is a test
    that checks nothing.
    """
    u = MagicMock()
    u.effective_user.id = clicker_id
    u.effective_user.username = "member"
    u.effective_user.first_name = "Mem"
    chat = MagicMock()
    chat.id = CHAT_ID
    chat.type = "private" if private else "supergroup"
    chat.is_forum = not private
    u.effective_chat = chat
    u.message = None

    original = MagicMock()          # the user's own message
    original.from_user.id = author_id
    thinking = MagicMock()          # the bot's "💭 thinking…" message
    thinking.message_id = THINKING_MSG_ID
    thinking.reply_to_message = original

    cq = MagicMock()
    cq.data = data
    cq.message = thinking
    cq.answer = AsyncMock()
    cq.delete_message = AsyncMock()
    cq.edit_message_text = AsyncMock()
    u.callback_query = cq
    return u


def _context():
    c = MagicMock()
    c.bot = MagicMock()
    for name in ("delete_forum_topic", "create_forum_topic", "edit_message_text",
                 "send_message", "delete_message", "get_file"):
        setattr(c.bot, name, AsyncMock())
    return c


@pytest.fixture
def allowlist(monkeypatch):
    """Set the allowlist the production container runs with.

    monkeypatch, not plain assignment: the allowlist is process-global
    state that every other test in the suite reads, and a test that
    leaves it changed breaks the tests that run after it. (That is not
    hypothetical -- the first version of this file did exactly that and
    took 15 unrelated tests down with it.)
    """
    def _set(*ids):
        import access
        import config
        monkeypatch.setattr(config, "ALLOWED_USER_IDS", set(ids))
        monkeypatch.setattr(config, "ALLOWED_USERNAMES", set())
        monkeypatch.setattr(access, "ALLOWED_USER_IDS", set(ids))
        monkeypatch.setattr(access, "ALLOWED_USERNAMES", set())
        monkeypatch.setattr(access, "LOCKDOWN", False)
    return _set


@pytest.fixture
def abort_slot():
    """The (chat, thinking-message) slot the handler looks the event up in."""
    from state import _abort_events
    ev = asyncio.Event()
    _abort_events[(CHAT_ID, THINKING_MSG_ID)] = ev
    yield ev
    _abort_events.pop((CHAT_ID, THINKING_MSG_ID), None)


@pytest.mark.asyncio
async def test_stop_from_a_non_allowlisted_author_sets_the_abort_event(allowlist, abort_slot):
    """The regression.

    The author is a group member who is NOT on the allowlist. The bot
    answers group members regardless of the allowlist, so the Stop button
    it put on their thinking message has to work for them."""
    allowlist(999999)      # somebody else is on the allowlist
    from commands.callback import cmd_callback

    upd = _stop_update(clicker_id=AUTHOR_ID, author_id=AUTHOR_ID)
    await cmd_callback(upd, _context())

    assert abort_slot.is_set(), (
        "the Stop press from the author was dropped before the handler "
        "acted on it -- the button did nothing while the model kept going"
    )
    upd.callback_query.delete_message.assert_awaited()


@pytest.mark.asyncio
async def test_stop_from_a_different_member_is_refused(allowlist, abort_slot):
    """The gate that matters. Someone else's turn is not yours to stop --
    and that must hold for a member who IS on the allowlist."""
    allowlist(STRANGER_ID, AUTHOR_ID)
    from commands.callback import cmd_callback

    upd = _stop_update(clicker_id=STRANGER_ID, author_id=AUTHOR_ID)
    await cmd_callback(upd, _context())

    assert not abort_slot.is_set(), (
        "a member who is not the author stopped someone else's turn"
    )
    said = [c.args[0] for c in upd.callback_query.answer.await_args_list
            if c.args]
    assert REFUSAL in said, (
        f"the refused clicker was not told why nothing happened: {said!r}"
    )


@pytest.mark.asyncio
async def test_subtalk_callbacks_still_need_the_allowlist(allowlist):
    """The 2026-10-22 decision stands.

    The sub-talk branches create and delete topics, and Telegram has no ACL
    for deleteForumTopic. Moving the Stop branch first must not have
    opened them."""
    allowlist()            # nobody is trusted
    from commands.callback import cmd_callback

    upd = _stop_update(data="delsub:77", private=True)
    ctx = _context()
    await cmd_callback(upd, ctx)

    assert not ctx.bot.delete_forum_topic.await_count, (
        "a stranger deleted a forum topic"
    )
    assert not upd.callback_query.edit_message_text.await_count, (
        "a stranger's delsub callback was acted on"
    )


@pytest.mark.asyncio
async def test_a_stop_press_is_always_acknowledged(allowlist):
    """answer() comes before the gate.

    A rejected callback that never gets answer() leaves the button
    spinning in the client's UI until Telegram's own timeout -- the user
    gets no feedback at all that the press was seen."""
    allowlist()
    from commands.callback import cmd_callback

    upd = _stop_update(clicker_id=STRANGER_ID, author_id=AUTHOR_ID)
    await cmd_callback(upd, _context())

    assert upd.callback_query.answer.await_count, (
        "the callback was never answered -- the button would spin"
    )
