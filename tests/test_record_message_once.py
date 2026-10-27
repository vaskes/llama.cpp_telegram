"""The record holds a message once, not twice.

Measured on the production database on 2026-10-27: the operator's
message "Удали сообщение 1" (Telegram id 3379) was in `messages`
TWICE -- row 10690, written by the text handler under the display name
("Vasisualy Lohankin"), and row 10693, written six hours later by the
group-absorb path under the short name ("Vasisualy"). The second row
appeared when another human replied to that message: the absorb path
stores the message that was replied to -- so that "the message where
he said X" is findable at all -- and it stored it without asking
whether the record already held it.

The record is the source of truth (the operator keeps it for a LoRA
dataset), so a message in it twice is noise in the one thing that must
not be noisy. And the two rows carried different names for the same
person, which is the shape the search-by-sender has already tripped
over once ("Dmitriklim" against "Димриклим").

Two things are being checked here, and both of them were red before:

  * the absorb path asks the record before it stores the message that
    was replied to, and does not store it a second time;
  * the question is asked BY ID. A message id is unique inside its
    chat; its TEXT is not -- two messages by the same person can carry
    the same words, and a check by text would drop the second one from
    the record. The tripwire for that is the one that matters most: a
    dedup that eats real messages is worse than a duplicate row.
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
TARGET_ID = 3307          # the message that was replied to
REPLY_ID = 3310           # the reply itself
TARGET_TEXT = "какой деплой сегодня?"


# --------------------------------------------------------------- harness --

def _returns(value):
    """A synchronous factory: an `async def` here returns a coroutine."""
    async def _f(*a, **kw):
        return value
    return _f


def _once_update():
    """An update where one human replies to another human."""
    class Chat:
        id, type = CHAT, "supergroup"

        async def send_action(self, *a, **kw):
            return None

    target_user = types.SimpleNamespace(
        id=286293081, username="dimon", first_name="Dmitry")
    target = types.SimpleNamespace(
        message_id=TARGET_ID, from_user=target_user,
        text=TARGET_TEXT, caption=None)
    replier = types.SimpleNamespace(
        id=1055898374, username="vasya", first_name="Vasya")
    msg = types.SimpleNamespace(
        chat=Chat(), from_user=replier, message_id=REPLY_ID,
        message_thread_id=THREAD, text="вот это сообщение димы удали",
        caption=None, reply_to_message=target, photo=None, document=None,
        voice=None, audio=None, video_note=None, sticker=None,
    )
    return types.SimpleNamespace(message=msg, effective_user=replier,
                                 effective_chat=msg.chat,
                                 callback_query=None, update_id=1,
                                 message_thread_id=THREAD)


def _once_context():
    return types.SimpleNamespace(bot=types.SimpleNamespace(data=None))


@pytest.fixture
def record(tmp_path, monkeypatch):
    """The record itself: a real SQLite file, for this test only."""
    import storage as storage_mod

    store = storage_mod.Storage(str(tmp_path / "history.db"))
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)
    return store


@pytest.fixture
def written(monkeypatch):
    """What the absorb path TRIES to write, captured.

    The real lookup is left alone on purpose: the question "is this
    message already in the record?" has to be answered by the record,
    not by a stub in the test.
    """
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


def _stored(written) -> list:
    return [c.get("telegram_message_id") for c in written]


# ----------------------------------------------------------------- tests --

def test_the_absorb_path_does_not_store_a_message_the_record_already_holds(
        record, written):
    """The message that was replied to is already in the record.

    The text handler stored it when it was written. Storing it again
    puts the same message in the record twice, under two different
    names for the same person.
    """
    import dispatch_group
    import persistence

    asyncio.run(persistence.persist(
        CHAT, THREAD, "user", TARGET_TEXT,
        sender_name="Dmitry Lohankin", telegram_message_id=TARGET_ID))

    asyncio.run(dispatch_group._handle_reply_to_other_user(
        _once_update(), _once_context()))

    ids = _stored(written)
    assert TARGET_ID not in ids, (
        f"the message that was replied to was stored a second time: {ids}")
    assert REPLY_ID in ids, (
        f"the reply itself was not stored: {ids}")


def test_a_message_the_record_does_not_hold_is_still_stored(record, written):
    """The fix must not swallow messages that are NOT in the record.

    The record starts empty -- which is the normal case for a message
    the bot was not there to see. A dedup that eats it would lose the
    message from the one thing that must not lose anything.
    """
    import dispatch_group

    asyncio.run(dispatch_group._handle_reply_to_other_user(
        _once_update(), _once_context()))

    ids = _stored(written)
    assert TARGET_ID in ids, (
        f"the message that was replied to was not stored at all: {ids}")
    assert REPLY_ID in ids, f"the reply itself was not stored: {ids}"


def test_the_check_is_by_message_id_and_not_by_text(record, written):
    """Same words, different message: the target still gets stored.

    A message id is unique in its chat; the text of a message is not.
    Two messages by the same person can carry the same words -- and a
    check by text would drop the second one from the record.
    """
    import dispatch_group
    import persistence

    asyncio.run(persistence.persist(
        CHAT, THREAD, "user", TARGET_TEXT,
        sender_name="@dimon", telegram_message_id=9999))

    asyncio.run(dispatch_group._handle_reply_to_other_user(
        _once_update(), _once_context()))

    ids = _stored(written)
    assert TARGET_ID in ids, (
        f"the target was dropped because ANOTHER message carries the same "
        f"text: {ids}")


def test_a_failed_lookup_does_not_swallow_the_message(record, written,
                                                      monkeypatch):
    """A lookup that fails means "not recorded", not "recorded".

    A duplicate row is noise. A message that is missing from the record
    is a hole in the one thing that must not have holes -- so when the
    record cannot answer, the message is written.
    """
    import dispatch_group

    async def _boom(chat_id, telegram_message_id):
        raise RuntimeError("the record is not answering")

    monkeypatch.setattr(dispatch_group, "_already_recorded", _boom)

    asyncio.run(dispatch_group._handle_reply_to_other_user(
        _once_update(), _once_context()))

    ids = _stored(written)
    assert TARGET_ID in ids, (
        f"the message was dropped because the lookup failed: {ids}")


def test_the_absorb_path_reads_the_record_through_persistence():
    """The lookup goes through persistence; the SQL lives in storage.

    One concept -- one owner. The absorb path asks a question about
    the record; `persistence` is the layer that owns the store; `storage`
    owns the SQL. A module that reaches for the database itself is a
    module that can start writing to it.
    """
    tree = ast.parse((BOT / "dispatch_group.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "storage" not in imported, (
        "dispatch_group imports the database module itself; the record is "
        f"read through persistence. Imported: {sorted(imported)}")


def test_the_lookup_is_asked_about_the_message_that_was_replied_to(
        record, written, monkeypatch):
    """The question carries the id of the replied-to message.

    A lookup that asks about something else -- the reply, or the chat,
    or nothing at all -- would answer a question nobody asked and
    leave the duplicate in place.
    """
    import dispatch_group

    asked = []

    async def _spy(chat_id, telegram_message_id):
        asked.append((chat_id, telegram_message_id))
        return False

    monkeypatch.setattr(dispatch_group, "_already_recorded", _spy)

    asyncio.run(dispatch_group._handle_reply_to_other_user(
        _once_update(), _once_context()))

    assert asked == [(CHAT, TARGET_ID)], (
        f"the record was asked something other than 'do you hold this "
        f"message?': {asked}")
