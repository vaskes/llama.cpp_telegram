"""Every message the bot stores carries the Telegram id it came from.

The operator's rule (2026-10-27): the bot must be able to find a message
it was sent -- by its text, by who wrote it -- and then take that message
back out of the chat, while the row in the database stays. For that to
work the id has to be written WHEN the message is written, not guessed
later: the Bot API cannot list a chat's messages, so an id the bot did
not store is an id the bot will never have again.

Three things are being checked here, and each of them was red before it
was fixed:

  * the text handler stores the id of the message it answered;
  * the voice and document handlers do the same -- checked on the AST,
    because driving them means driving the Whisper call and the
    document parser, and the property under test is a keyword argument
    on one call;
  * the group-absorb path stores BOTH sides of the exchange: the
    message a human replied to, and the reply. Until now it stored only
    the reply, with the target named in a prefix, so "the message where
    he said X" was not in the record at all and the bot could not take
    it back out of the chat.

And one thing that is not a test but is the reason for the last one: the
prefix used to be "[reply to id=286293081]" -- a USER id, in the shape
of a message id. On 2026-10-26 the model read those digits out of the
record and handed them to the delete tool. A record that carries a
number in the shape of the other number is a trap with the operator's
own messages in it.
"""
import ast
import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

CHAT = -100777
THREAD = "tid"
MESSAGE_ID = 4242


# --------------------------------------------------------------- harness --

def _returns(value):
    """A synchronous factory: an `async def` here returns a coroutine."""
    async def _f(*a, **kw):
        return value
    return _f


class _Thinking:
    message_id = 777

    async def delete(self):
        return None


def _update(message_id=MESSAGE_ID):
    class Chat:
        id, type = CHAT, "group"

        async def send_action(self, *a, **kw):
            return None

    user = types.SimpleNamespace(id=1, username="tester", first_name="Test",
                                 last_name="Er")
    msg = types.SimpleNamespace(
        chat=Chat(), from_user=user, message_id=message_id,
        message_thread_id=THREAD, text="привет", caption=None,
        caption_entities=[], entities=[], date=None, reply_to_message=None,
        reply_text=None, photo=None, document=None, voice=None,
        audio=None, video_note=None, sticker=None,
    )
    return types.SimpleNamespace(message=msg, effective_user=user,
                                 effective_chat=msg.chat,
                                 callback_query=None,
                                 message_thread_id=THREAD)


def _context():
    return types.SimpleNamespace(bot=types.SimpleNamespace(data=None))


@pytest.fixture
def store(tmp_path, monkeypatch):
    import storage as storage_mod
    import turn as turn_mod

    db = str(tmp_path / "history.db")
    store = storage_mod.Storage(db)
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)
    monkeypatch.setattr(turn_mod, "storage", storage_mod)
    return store


# ---------------------------------------------------------- the handlers --

def test_the_text_handler_stores_the_telegram_id(store, monkeypatch):
    """The row the user's message produces carries the id of that message.

    The rollback is neutralised here on purpose: the handler fails after
    the persist (the history is not available), and the rollback would
    take the row with it. What is under test is what was WRITTEN, not
    what survived.
    """
    import handlers
    import turn as turn_mod

    async def _boom(*a, **kw):
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(handlers, "_route_to_thread",
                        _returns((CHAT, THREAD, False)))
    monkeypatch.setattr(handlers, "_open_thinking",
                        _returns((_Thinking(), "Test")))
    monkeypatch.setattr(handlers, "_load_history", _boom)
    monkeypatch.setattr(turn_mod.Turn, "rollback", _returns(None))

    with pytest.raises(Exception):
        asyncio.run(handlers.handle_text(_update(), _context()))

    stored = store._conn().execute(
        "SELECT telegram_message_id, content FROM messages").fetchall()
    assert len(stored) == 1, f"the handler wrote {len(stored)} row(s)"
    assert stored[0][0] == MESSAGE_ID, (
        f"the row carries the id {stored[0][0]}, not {MESSAGE_ID}: the "
        f"message cannot be found again without it")


@pytest.mark.parametrize("handler", ["handle_voice", "handle_document"])
def test_the_media_handlers_store_the_telegram_id(handler):
    """The voice and the document handlers store the id as well.

    Checked on the AST rather than by driving them: the property under
    test is a keyword argument on the persist call, and driving these
    two means driving the Whisper call and the document parser to get
    there. The check fails if the argument is dropped -- which is the
    mutation that matters.
    """
    tree = ast.parse((BOT / "handlers.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == handler)
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "persist"]
    assert calls, f"{handler} does not persist anything"

    with_id = []
    for call in calls:
        for kw in call.keywords:
            if kw.arg == "telegram_message_id":
                with_id.append(kw.value)
    assert with_id, (
        f"{handler} persists a message without the Telegram id it came "
        f"from: the id is the only way back to that message, and the Bot "
        f"API cannot list a chat's messages")
    for value in with_id:
        assert isinstance(value, ast.Attribute) and value.attr == "message_id", (
            f"{handler} passes something other than the message id as the "
            f"Telegram id: {ast.dump(value)}")


# ------------------------------------------------- the group-absorb path --

def _absorb_update(*, target_username="dimon", target_id=286293081,
                    reply_text="вот это сообщение димы удали"):
    """An update where one human replies to another human."""
    class Chat:
        id, type = CHAT, "supergroup"

        async def send_action(self, *a, **kw):
            return None

    target_user = types.SimpleNamespace(
        id=target_id, username=target_username, first_name="Dmitry")
    target = types.SimpleNamespace(
        message_id=3307, from_user=target_user,
        text="какой деплой сегодня?", caption=None)
    replier = types.SimpleNamespace(
        id=1055898374, username="vasya", first_name="Vasya")
    msg = types.SimpleNamespace(
        chat=Chat(), from_user=replier, message_id=3310,
        message_thread_id=THREAD, text=reply_text, caption=None,
        reply_to_message=target, photo=None, document=None, voice=None,
        audio=None, video_note=None, sticker=None,
    )
    return types.SimpleNamespace(message=msg, effective_user=replier,
                                 effective_chat=msg.chat,
                                 callback_query=None, update_id=1,
                                 message_thread_id=THREAD)


@pytest.fixture
def persisted(monkeypatch):
    """Capture what the absorb path writes, without a database."""
    import dispatch_group

    calls = []

    async def _capture(chat_id, thread_id, role, content, **kw):
        calls.append({"chat_id": chat_id, "thread_id": thread_id,
                      "role": role, "content": content, **kw})

    monkeypatch.setattr(dispatch_group, "_persist_message", _capture)
    monkeypatch.setattr(dispatch_group, "_load_history", _returns([]))
    # The silent reaction is not what is under test, and the real one
    # reaches for the engine: a test that talks to localhost:8080 is a
    # test that waits for a timeout.
    monkeypatch.setattr(dispatch_group, "call_llama",
                        types.SimpleNamespace(call_llama=_returns(None)))
    return calls


def test_the_absorb_path_stores_both_sides_of_the_exchange(persisted):
    """The message that was replied to is a message of the chat too.

    Until now the record carried only the reply, with the target named
    in a prefix. That is enough for the model to read and not enough to
    find: "the message where he said X" was not in the record, so the
    bot could not take it back out of the chat.
    """
    import dispatch_group

    asyncio.run(dispatch_group._handle_reply_to_other_user(
        _absorb_update(), _context()))

    ids = [c.get("telegram_message_id") for c in persisted]
    assert 3307 in ids, (
        f"the message that was replied to was not stored: {persisted}")
    assert 3310 in ids, (
        f"the reply itself was not stored: {persisted}")

    target = next(c for c in persisted
                  if c.get("telegram_message_id") == 3307)
    assert "какой деплой сегодня?" in target["content"], (
        f"the target was stored without its text: {target['content']!r}")
    assert target.get("sender_name"), (
        "the target was stored without saying who wrote it")


def test_the_absorb_path_stores_the_target_before_the_reply(persisted):
    """The target is the older of the two, and the record is in order."""
    import dispatch_group

    asyncio.run(dispatch_group._handle_reply_to_other_user(
        _absorb_update(), _context()))

    ids = [c.get("telegram_message_id") for c in persisted]
    assert ids.index(3307) < ids.index(3310), (
        f"the reply was stored before the message it answers: {ids}")


def test_the_recorded_reply_carries_no_bare_user_id(persisted):
    """No "id=<digits>" in the stored text.

    A user id in the shape of a message id is a trap: on 2026-10-26 the
    model read the digits out of the record and handed them to the
    delete tool as a message id.
    """
    import dispatch_group

    asyncio.run(dispatch_group._handle_reply_to_other_user(
        _absorb_update(target_username=None), _context()))

    for call in persisted:
        assert "id=" not in call["content"], (
            f"the stored text carries a bare id: {call['content']!r}")
        assert "286293081" not in call["content"], (
            f"the stored text carries the user id: {call['content']!r}")
