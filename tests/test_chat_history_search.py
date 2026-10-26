"""The bot's own record of the chat, and what it buys.

The operator's rule (2026-10-27): the bot must be able to find a message
it was sent -- by its text, by who wrote it -- and take that message back
out of the chat, **while the row in the database stays**. The database is
the bot's own record of what happened and it is on its way to becoming a
training set; a row that vanishes because a message was deleted in the
chat is a hole in that record.

The chain this file walks, end to end:

    a message in the record  ->  search_messages finds it  ->  the
    dispatcher vouches for its id  ->  delete_messages takes the message
    out of the chat  ->  THE ROW IS STILL THERE.

The last part is the one that is easy to lose: a deletion that cleans up
after itself looks tidier and destroys the record. The rule is the
other way round.
"""
import inspect
import json
import os
import sys

import pytest

_REPO = os.path.join(os.path.dirname(__file__), "..", "telegram-bot")
sys.path.insert(0, _REPO)

CHAT = 100


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
    """A store with a few messages of this chat, and a second chat."""
    import storage as storage_mod

    store = storage_mod.Storage(str(tmp_path / "history.db"))
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)

    def _row(chat_id, text, *, sender="Vasya", tg_id=None, role="user"):
        payload = json.dumps({"role": role, "content": text},
                             ensure_ascii=False)
        return store.add_message(chat_id, "main", role, payload,
                                 sender_name=sender,
                                 telegram_message_id=tg_id)

    _row(CHAT, "какой деплой сегодня?", sender="Dmitry", tg_id=3307)
    _row(CHAT, "вот это сообщение димы удали", sender="Vasya", tg_id=3310)
    _row(CHAT + 1, "чужой чат", sender="Someone", tg_id=9999)
    return store


# --- the tool --------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_search_returns_the_telegram_id_of_a_match(store):
    """The id is the point of the search: without it there is nothing to
    act on."""
    from llm_history import run_search

    text, extras = await run_search({"text": "деплой"}, chat_id=CHAT)
    assert "3307" in text, f"the match carries no id: {text!r}"
    assert "какой деплой" in text
    assert extras["matches"] == 1


@pytest.mark.asyncio
async def test_the_search_finds_a_message_by_who_wrote_it(store):
    from llm_history import run_search

    text, _ = await run_search({"sender": "dmitr"}, chat_id=CHAT)
    assert "3307" in text, f"the sender filter found nothing: {text!r}"


@pytest.mark.asyncio
async def test_the_search_does_not_reach_into_another_chat(store):
    """One chat's record is not another's."""
    from llm_history import run_search

    text, _ = await run_search({"text": "чужой"}, chat_id=CHAT)
    assert "9999" not in text, f"the search reached into another chat: {text!r}"
    assert "0 results" in text, (
        f"the search found a message of another chat: {text!r}")


@pytest.mark.asyncio
async def test_the_search_says_so_when_it_finds_nothing(store):
    """Zero results has to read as zero results.

    The tool loop treats a result that says "0 results" as no progress,
    and a search that found nothing IS no progress. A search that says
    "the record is empty" reads like an answer, and the turn goes on
    with nothing.
    """
    from llm_history import run_search

    text, extras = await run_search({"text": "не существует"}, chat_id=CHAT)
    assert "0 results" in text, f"an empty search did not say so: {text!r}"
    assert extras["matches"] == 0


@pytest.mark.asyncio
async def test_the_search_does_not_change_the_record(store):
    """A read is a read."""
    from llm_history import run_search

    before = len(store.get_messages(CHAT, "main", limit=100))
    await run_search({"text": "деплой"}, chat_id=CHAT)
    after = len(store.get_messages(CHAT, "main", limit=100))
    assert before == after, f"the search wrote to the record: {before} -> {after}"


@pytest.mark.asyncio
async def test_the_dispatcher_routes_the_search_tool(store):
    """The name is dispatched, not soft-rejected."""
    from llm_dispatch import DispatchDeps, execute_tool

    deps = DispatchDeps(bot=_FakeBot(), chat_id=CHAT, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    result = await execute_tool("search_messages", {"text": "деплой"}, deps)
    assert "3307" in result.text, f"the tool did not run: {result.text!r}"
    assert result.ledger["name"] == "search_messages"


# --- the chain: a message in the record can be taken back out --------------

@pytest.mark.asyncio
async def test_a_message_the_record_holds_can_be_deleted(store):
    """The id the search found is an id the delete tool may act on.

    Before this, the only ids in reach were the ones the live update
    carried and the bot's own messages. The dispatcher looks the id up
    in the record and hands it to the tool, which cannot read the
    database itself.
    """
    from llm_dispatch import DispatchDeps, execute_tool

    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=CHAT, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    result = await execute_tool("delete_messages",
                                 {"message_ids": [3307]}, deps)
    assert bot.deleted == [(CHAT, 3307)], (
        f"the message was not deleted: {bot.deleted!r} / {result.text!r}")


@pytest.mark.asyncio
async def test_the_record_survives_the_deletion(store):
    """The rule the operator wrote down.

    The message leaves the chat. The row stays: the record of what
    happened is not the chat, and a deletion must not clean up after
    itself.
    """
    from llm_dispatch import DispatchDeps, execute_tool

    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=CHAT, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    await execute_tool("delete_messages", {"message_ids": [3307]}, deps)

    rows = store._conn().execute(
        "SELECT telegram_message_id FROM messages WHERE chat_id = ?",
        (CHAT,)).fetchall()
    assert [r[0] for r in rows] == [3307, 3310], (
        f"the deletion took the record with it: {[r[0] for r in rows]}")


@pytest.mark.asyncio
async def test_an_id_the_record_does_not_hold_is_still_refused(store):
    """The guard is still a guard.

    The record decides what the bot can vouch for. An id that is not in
    it is a number the model produced on its own, and the bot is an
    administrator: a made-up id is a real attempt to delete somebody
    else's message.
    """
    from llm_dispatch import DispatchDeps, execute_tool

    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=CHAT, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    result = await execute_tool("delete_messages",
                                 {"message_ids": [4242]}, deps)
    assert bot.deleted == [], (
        f"a made-up id was deleted: {bot.deleted!r}")
    assert "refused" in result.text, f"the refusal was not reported: {result.text!r}"


@pytest.mark.asyncio
async def test_an_id_of_another_chat_is_refused(store):
    """The record of one chat does not vouch for another one's message."""
    from llm_dispatch import DispatchDeps, execute_tool

    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=CHAT, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    await execute_tool("delete_messages", {"message_ids": [9999]}, deps)
    assert bot.deleted == [], (
        f"a message of another chat was deleted: {bot.deleted!r}")


# --- the delete tool cannot reach the record -------------------------------

def test_the_delete_tool_cannot_reach_the_record():
    """The tool is handed the ids it may use; it never looks them up.

    Two doors, and both are shut: the module does not import the store,
    and it does not import the module that reads it. A deletion that
    could reach the record could reach it for writing, and the record is
    not the tool's to change.
    """
    import ast
    import llm_delete

    tree = ast.parse(inspect.getsource(llm_delete))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "storage" not in imported, (
        f"the delete tool imports {sorted(imported)}")
    assert "llm_history" not in imported, (
        f"the delete tool imports {sorted(imported)} -- the module that "
        f"reads the record is the one door the tool must not have")
    assert "storage" not in llm_delete.__dict__
    assert "llm_history" not in llm_delete.__dict__


@pytest.mark.asyncio
async def test_the_delete_tool_alone_never_reads_the_record(monkeypatch):
    """The store is made to shout if anything asks it for anything."""
    import storage

    def _boom(*a, **kw):
        raise AssertionError(
            "the delete tool touched the bot's database -- the history "
            "is the record of what happened, and it is not the tool's "
            "to change")

    monkeypatch.setattr(storage, "get_store", _boom)

    from llm_delete import execute_delete_messages

    bot = _FakeBot()
    text, _ = await execute_delete_messages(
        {"message_ids": [3307]}, bot=bot, chat_id=CHAT,
        chat_type="supergroup", known_ids={3307})
    assert bot.deleted == [(CHAT, 3307)]
    assert "deleted 1" in text
