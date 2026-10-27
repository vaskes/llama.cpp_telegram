"""The conversation shows the model the Telegram id of every message.

The live test of 2026-10-27 (01:21 MSK), measured, not guessed:

  * the operator wrote "Сообщение 1", "Сообщение 2", ... and then "Удали
    сообщение 1";
  * the bot answered "Сообщение 1 — 3369" (right, by luck: 3369 was the
    id of the message being answered, which the tool description
    happened to carry), then "Сообщение 1 (3354), Сообщение 2 (3357)"
    -- both wrong;
  * 3354 and 3357 were ids of the BOT's own messages, taken from the bag
    of ids in the delete tool's description;
  * the final turn called no tool at all (reasoning_chars=9851,
    tool_calls=0) and asked the operator to clarify.

The cause is one sentence: the id of a message the USER wrote was
nowhere in anything the model was shown. The conversation carried the
text and nothing else, so a message named by its text ("Сообщение 1")
had no id to resolve to -- and a model without an id takes a number out
of whatever bag of numbers it can see.

So the id now rides along with the text, as a marker line of its own
above it. What this file checks:

  * the id reaches the model next to the text it belongs to;
  * a row with no id gets no marker (the bot's own answers are stored
    before they are sent, so their id does not exist yet);
  * the marker is its own line, above the text -- a display name is
    user-controlled text, and it must not be mistakable for an id;
  * the id does not travel to the engine as a message field;
  * the queries that feed the context carry the id, including the
    summarising one (T70.5), whose raw tail is the recent past;
  * the photo turn, the one turn that is NOT persisted, carries the id
    as well;
  * and the chain that matters to the operator: an id the model read
    out of the conversation is an id the bot vouches for, so
    `delete_messages` may act on it.

The last one is the point of the whole change. Without it the marker is
decoration: the delete tool refuses ids it cannot vouch for, and the
record is what vouches.
"""
import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

CHAT = -100777
THREAD = "main"
MESSAGE_ID = 3369

#: The marker the model reads the id from.
MARKER = re.compile(r"^\[telegram id (\d+)\]$", re.MULTILINE)


class _FakeMember:
    def __init__(self, status="administrator", can_delete=True):
        self.status = status
        self.can_delete_messages = can_delete


class _FakeBot:
    """The Telegram API, as much of the delete tool touches."""

    def __init__(self, *, status="administrator", can_delete=True):
        self.status = status
        self.can_delete = can_delete
        self.deleted = []

    async def get_chat_member(self, chat_id, user_id):
        return _FakeMember(self.status, self.can_delete)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path):
    """The registry is module state; every test starts from nothing."""
    import llm_delete
    llm_delete._SENT.clear()
    llm_delete._admin_cache.clear()
    llm_delete._LOG_LOADED = False
    llm_delete._GROUP_SEQ = 0
    llm_delete.SENT_LOG_PATH = str(tmp_path / "sent_messages.jsonl")
    yield
    llm_delete._SENT.clear()
    llm_delete._admin_cache.clear()
    llm_delete._LOG_LOADED = False
    llm_delete._GROUP_SEQ = 0


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store with three messages: one with an id, one without, and an
    answer of the bot's own."""
    import storage as storage_mod

    store = storage_mod.Storage(str(tmp_path / "history.db"))
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)

    def _row(text, *, sender="Vasya", tg_id=None, role="user"):
        payload = json.dumps({"role": role, "content": text},
                             ensure_ascii=False)
        return store.add_message(CHAT, THREAD, role, payload,
                                 sender_name=sender,
                                 telegram_message_id=tg_id)

    _row("Сообщение 1", tg_id=MESSAGE_ID)
    _row("Сообщение 2", tg_id=MESSAGE_ID + 2)
    _row("[llm] Вижу.", role="assistant")
    return store


def _shaped(history):
    """The messages as they go to the engine."""
    import llm_messages
    return [llm_messages._tag_sender(m) for m in history]


def _ids_in(history):
    """The ids the model can read out of these messages."""
    out = []
    for m in _shaped(history):
        content = m.get("content")
        if isinstance(content, str):
            out.extend(int(x) for x in MARKER.findall(content))
    return out


# --- the id reaches the model ---------------------------------------------

async def test_the_model_sees_the_id_next_to_the_text_it_wrote(store):
    """The whole point: a message named by its text becomes an id."""
    import persistence

    history = await persistence.load_history(CHAT, THREAD)
    shaped = _shaped(history)
    user = [m for m in shaped if m.get("role") == "user"]
    assert user, "the history carries no user message"

    content = user[0]["content"]
    assert f"[telegram id {MESSAGE_ID}]" in content, (
        f"the id is not shown to the model: {content!r}")
    assert "Сообщение 1" in content, (
        f"the text the id belongs to is gone: {content!r}")


async def test_every_message_with_an_id_is_marked(store):
    import persistence

    history = await persistence.load_history(CHAT, THREAD)
    assert _ids_in(history) == [MESSAGE_ID, MESSAGE_ID + 2], (
        f"not every message carries its id: {_ids_in(history)!r}")


async def test_a_row_without_an_id_is_left_alone(store):
    """The bot's own answers are stored BEFORE they are sent, so their
    id does not exist yet. There is nothing to mark."""
    import persistence

    history = await persistence.load_history(CHAT, THREAD)
    answers = [m for m in _shaped(history) if m.get("role") == "assistant"]
    assert answers, "the history carries no answer of the bot's own"
    for m in answers:
        assert "telegram id" not in m["content"], (
            f"an answer of the bot's own was marked with an id: "
            f"{m['content']!r}")


async def test_the_marker_is_a_line_of_its_own_above_the_text(store):
    """A display name is user-controlled text. It must not be
    mistakable for an id, and the id must not be mistakable for part of
    the name."""
    import persistence

    history = await persistence.load_history(CHAT, THREAD)
    content = _shaped(history)[0]["content"]
    lines = content.splitlines()
    marker_lines = [i for i, ln in enumerate(lines)
                    if ln.startswith("[telegram id ")]
    assert marker_lines, f"no marker line: {content!r}"
    for i in marker_lines:
        assert i + 1 < len(lines), f"the marker is the last line: {content!r}"
        assert lines[i + 1].startswith("From: "), (
            f"the marker does not sit directly above the message it "
            f"belongs to: {content!r}")
        assert MARKER.match(lines[i]), (
            f"the marker is not a line of its own: {lines[i]!r}")


async def test_the_id_does_not_travel_as_a_message_field(store):
    """It is shown in the text, not smuggled in as a field."""
    import persistence

    history = await persistence.load_history(CHAT, THREAD)
    for m in _shaped(history):
        assert "_telegram_message_id" not in m, (
            f"the id leaked into the request as a field: {m!r}")


# --- the queries that feed the context carry it ---------------------------

def test_the_context_query_returns_the_id(store):
    rows = store.get_messages(CHAT, THREAD, 10)
    ids = [r.get("telegram_message_id") for r in rows]
    assert MESSAGE_ID in ids, (
        f"the context query dropped the Telegram id: {ids!r}")


def test_the_summarising_queries_return_the_id(store):
    """T70.5's raw tail is the recent past -- the part the model most
    often needs an id for."""
    rows = store.get_messages_since(CHAT, THREAD, 0.0, 10)
    assert MESSAGE_ID in [r.get("telegram_message_id") for r in rows], (
        "get_messages_since dropped the Telegram id")

    old = store.get_pending_old(CHAT, THREAD, float("inf"), 10)
    assert old, "the pending-old query returned nothing"
    assert "telegram_message_id" in old[0], (
        f"get_pending_old dropped the Telegram id: {old[0].keys()!r}")


def test_the_photo_turn_carries_the_id():
    """The photo is the one turn that is NOT persisted, so its current
    turn is built by hand -- and it would otherwise be the one message
    whose id the model cannot see."""
    tree = ast.parse((BOT / "handlers.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_photo")
    built = [n for n in ast.walk(fn)
             if isinstance(n, ast.Assign)
             and any(getattr(t, "id", None) == "current_turn" for t in n.targets)]
    assert built, "handle_photo builds no current_turn"

    keys = set()
    for node in built:
        if isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant):
                    keys.add(key.value)
    assert "_telegram_message_id" in keys, (
        f"the photo turn carries no Telegram id: {sorted(keys)!r}")


# --- the chain: an id read out of the conversation is vouched for ----------

async def test_the_id_the_model_read_is_one_the_bot_vouches_for(store):
    """The marker is not decoration.

    The delete tool refuses any id it cannot vouch for, and the record
    is what vouches. So the id the model reads out of the conversation
    has to be an id the record holds -- otherwise the model reads a
    number, passes it, and is refused for a reason that looks like the
    tool being broken.
    """
    import persistence
    from llm_dispatch import DispatchDeps, execute_tool

    history = await persistence.load_history(CHAT, THREAD)
    ids = _ids_in(history)
    assert ids, "the model cannot read a single id out of the conversation"

    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=CHAT, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    result = await execute_tool("delete_messages",
                                 {"message_ids": [ids[0]]}, deps)
    assert bot.deleted == [(CHAT, ids[0])], (
        f"the message was not deleted: {bot.deleted!r} / {result.text!r}")


async def test_an_id_that_is_not_in_the_record_is_still_refused(store):
    """The other side of the same door: the marker is a source of ids,
    not a way past the check."""
    from llm_dispatch import DispatchDeps, execute_tool

    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=CHAT, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    result = await execute_tool("delete_messages",
                                 {"message_ids": [111222]}, deps)
    assert bot.deleted == [], (
        f"an id nobody vouched for was deleted: {bot.deleted!r}")
    assert "111222" not in result.text or "refus" in result.text.lower() or \
        "not" in result.text.lower(), (
        f"the refusal does not say what went wrong: {result.text!r}")


# --- the description the model reads ---------------------------------------

def test_the_tool_description_names_the_marker():
    """The description is the policy. A marker the description does not
    mention is decoration: the model reads the id and refuses to pass
    it, because the description says the only ids are the listed ones."""
    import llm_delete

    desc = llm_delete.delete_messages_tool_def(
        chat_id=CHAT, current_message_id=7)["function"]["description"]
    assert "[telegram id N]" in desc, (
        f"the description does not mention the marker: {desc!r}")
    assert "search_messages" in desc, (
        f"the description does not mention the other source of ids: {desc!r}")


def test_the_tool_description_says_the_own_ids_are_not_the_users():
    """The measured failure: the model took 3354 and 3357 out of the
    list of the bot's own messages and offered them as the ids of the
    user's messages."""
    import llm_delete

    # The fence lives in the branch that lists the bot's own ids, so
    # the registry has to hold one -- otherwise the description has
    # nothing to fence off and the test would pass on an empty case.
    llm_delete._SENT[CHAT].append((4242, 1))

    desc = llm_delete.delete_messages_tool_def(
        chat_id=CHAT, current_message_id=7)["function"]["description"]
    assert "not the user's" in desc, (
        f"the description does not fence off the bot's own ids: {desc!r}")


def test_the_prompt_says_where_an_id_may_come_from():
    """The prompt is where the model is told what an id IS.

    The measured failure: the rules said "Use those and nothing else"
    and listed three sources -- the message just written, the message
    replied to, the bot's own messages. None of them was the message
    the user wrote five minutes ago, so the model either guessed one or
    asked the operator to say it again.
    """
    from prompt_text import GROUP_CONTEXT

    assert "[telegram id N]" in GROUP_CONTEXT, (
        "the prompt does not tell the model where an id may come from")
    assert "NOT evidence" in GROUP_CONTEXT, (
        "the prompt does not tell the model that an id it stated in an "
        "earlier answer is not evidence -- on 2026-10-27 the answer "
        "'Сообщение 1 — 3354' was repeated back as a fact in the next "
        "turn")
