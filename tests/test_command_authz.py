"""Slash commands are for trusted ids, in a group as well as in private.

Operator decision 2026-10-22. Before it, eight handlers branched on the
chat type before checking the whitelist, so the group path was open to
anyone who could post: /delsub called deleteForumTopic on a real topic,
/reset cleared history, /newsub created topics.

These tests drive the handlers rather than reading them, because the
whole failure was structural and invisible: the code compiled, the docs
promised delegation to Telegram, and 1353 tests were green.

Telegram has no ACL for deleteForumTopic, so "access control is delegated
to Telegram" does not transfer from reading to destroying.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

# Every slash command, plus the inline-keyboard callback handler. help and
# start already checked first; they are here so a future edit cannot move
# the check down in them either.
COMMANDS = ["reset", "stats", "cmd_newsub", "cmd_sub", "cmd_here",
            "cmd_subs", "cmd_delsub", "cmd_callback", "cmd_help", "start"]

DESTRUCTIVE = ["reset", "cmd_newsub", "cmd_delsub", "cmd_callback"]


def _group_update(text="/delsub 77", chat_id=-100999):
    u = MagicMock()
    u.effective_user.id = 555           # a stranger
    u.effective_user.username = "nobody"
    u.effective_user.first_name = "No"
    chat = MagicMock()
    chat.id = chat_id
    chat.type = "supergroup"
    chat.send_action = AsyncMock()
    u.effective_chat = chat
    u.message = MagicMock()
    u.message.chat = chat
    u.message.text = text
    u.message.caption = None
    u.message.message_id = 1
    u.message.message_thread_id = 77
    u.message.from_user = u.effective_user
    return u


def _context():
    c = MagicMock()
    c.bot = MagicMock()
    for name in ("delete_forum_topic", "create_forum_topic", "edit_message_text",
                 "send_message", "delete_message", "get_file"):
        setattr(c.bot, name, AsyncMock())
    return c


@pytest.fixture
def bot_calls(monkeypatch):
    """Record every Telegram API call a handler attempts."""
    calls = []

    def _rec(name):
        async def f(*a, **kw):
            calls.append((name, kw))
            return True
        return f

    import commands.start_cmd as _sc
    import commands.subtalking as _st
    import dispatch as _d

    # Patch the module a name is *called from*, not the module the test
    # used to reach it through. TECH-DEBT #10 moved the command handlers
    # into `commands/`; each one does its own `from handlers import _reply`,
    # so patching `dispatch._reply` now patches a name nothing reads, and
    # the real `_reply` runs against a MagicMock -- the test would keep
    # passing while asserting nothing. Both command modules are patched
    # because the command set spans them.
    async def _fake_reply(update, text, **kw):
        calls.append(("reply", {"text": text}))
        m = MagicMock()
        m.message_id = 1
        m.edit_text = AsyncMock()
        m.delete = AsyncMock()
        return m

    for mod in (_sc, _st, _d):
        for attr in ("_reply", "send_reply"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, _fake_reply)
    for mod in (_sc, _st, _d):
        if hasattr(mod, "_is_group_chat"):
            monkeypatch.setattr(mod, "_is_group_chat", lambda u: True)
        if hasattr(mod, "_check_user_slot"):
            monkeypatch.setattr(mod, "_check_user_slot",
                                AsyncMock(return_value=asyncio.Semaphore(1)))
        if hasattr(mod, "_is_rating_active"):
            monkeypatch.setattr(mod, "_is_rating_active", lambda u: False)
        if hasattr(mod, "_stop_button_markup"):
            monkeypatch.setattr(mod, "_stop_button_markup", lambda: None)
        if hasattr(mod, "_start_reanchor_scheduler"):
            monkeypatch.setattr(mod, "_start_reanchor_scheduler", lambda: None)
    if hasattr(_d, "_route_to_thread"):
        monkeypatch.setattr(_d, "_route_to_thread",
                            AsyncMock(return_value=(1, "main", True)))
    monkeypatch.setattr("handlers._register_abort_event", lambda *a: None)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("name", COMMANDS)
async def test_stranger_in_a_group_reaches_nothing(monkeypatch, name,
                                                   bot_calls):
    """Not one Telegram call. Not a reply, not a topic, not a delete."""
    import dispatch
    import access

    # LOCKDOWN: empty allowlist, so is_authorized is False for everyone.
    monkeypatch.setattr("config.ALLOWED_USER_IDS", set())
    monkeypatch.setattr("config.ALLOWED_USERNAMES", set())
    monkeypatch.setattr(access, "ALLOWED_USER_IDS", set())
    monkeypatch.setattr(access, "ALLOWED_USERNAMES", set())

    c = _context()
    await getattr(dispatch, name)(_group_update(), c)

    destructive = [x for x in bot_calls
                   if x[0] in ("delete_forum_topic", "create_forum_topic",
                              "reply")]
    assert not destructive, (
        f"{name} acted for a stranger: {destructive}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", DESTRUCTIVE)
async def test_stranger_does_not_clear_history(monkeypatch, name, bot_calls,
                                               tmp_path):
    """/reset and friends must not delete a single stored message."""
    import dispatch
    import access
    from storage import Storage
    import config as _cfg
    import storage as _st

    db = str(tmp_path / "t.db")
    monkeypatch.setattr(_cfg, "DB_PATH", db)
    monkeypatch.setattr(_st, "_store_instance", None)
    store = Storage(db)
    monkeypatch.setattr(_st, "get_store", lambda: store)
    store.create_thread(1, "main")
    for i in range(3):
        store.add_message(1, "main", "user",
                          '{"role":"user","content":"keep me %d"}' % i)

    monkeypatch.setattr("config.ALLOWED_USER_IDS", set())
    monkeypatch.setattr("config.ALLOWED_USERNAMES", set())
    monkeypatch.setattr(access, "ALLOWED_USER_IDS", set())
    monkeypatch.setattr(access, "ALLOWED_USERNAMES", set())

    await getattr(dispatch, name)(_group_update(text="/reset"), _context())

    assert len(store.get_messages(1, "main")) == 3, (
        f"{name} deleted stored messages for an unauthorized user"
    )


@pytest.mark.asyncio
async def test_trusted_user_still_gets_through(monkeypatch, bot_calls):
    """The control. A gate that rejects everyone is not a gate."""
    import dispatch
    import access

    monkeypatch.setattr("config.ALLOWED_USER_IDS", {555})
    monkeypatch.setattr("config.ALLOWED_USERNAMES", set())
    monkeypatch.setattr(access, "ALLOWED_USER_IDS", {555})
    monkeypatch.setattr(access, "ALLOWED_USERNAMES", set())

    ctx = _context()
    await dispatch.cmd_delsub(_group_update("/delsub 77"), ctx)

    acted = [x for x in bot_calls if x[0] in ("delete_forum_topic",
                                              "create_forum_topic", "reply")]
    assert acted, (
        "a trusted id was blocked by the new gate -- the allowlist is not "
        "being consulted, it is being ignored"
    )


def test_every_command_checks_the_whitelist_before_the_chat_type():
    """The structural property, in case a handler is added later.

    A handler added without the guard is the regression this whole file
    exists to prevent, and nothing else in the suite would notice.
    """
    import ast
    tree = ast.parse((ROOT / "telegram-bot" / "dispatch.py").read_text())
    bad = []
    for fn in tree.body:
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        if not (fn.name.startswith("cmd_")
                or fn.name in ("start", "reset", "stats", "help")):
            continue
        auth = grp = None
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = getattr(n.func, "id", "") or getattr(n.func, "attr", "")
                if f == "reject_if_unauthorized" and auth is None:
                    auth = n.lineno
                if f == "_is_group_chat" and grp is None:
                    grp = n.lineno
        if auth is None:
            bad.append(f"{fn.name}: no reject_if_unauthorized at all")
        elif grp is not None and grp < auth:
            bad.append(f"{fn.name}: _is_group_chat@{grp} before "
                       f"reject_if_unauthorized@{auth}")
    assert not bad, "\n".join(bad)