"""The `delete_messages` tool: what it may delete, and what it may not.

The operator asked for it on 2026-10-26: the bot is an administrator
in the admin channel, and the channel could not be cleaned. The same
message carried a rule that is not negotiable: "sqlite наша священная
корова -- источник истины и всего того, что происходило в канале. Базу
не трогаем."

So the tests here cover two things: the deletion itself (whose reach,
whose messages, whose refusals) and the database, which must not be
touched at all.
"""
import inspect
import os
import sys

import pytest

_REPO = os.path.join(os.path.dirname(__file__), "..", "telegram-bot")
sys.path.insert(0, _REPO)


class _FakeUser:
    def __init__(self, user_id=42):
        self.id = user_id
        self.username = "testbot"


class _FakeMember:
    def __init__(self, status="administrator", can_delete=True):
        self.status = status
        self.can_delete_messages = can_delete


class _FakeBot:
    """The Telegram API, as much of it as the tool touches."""

    def __init__(self, *, status="administrator", can_delete=True,
                 fail_ids=(), member_error=None):
        self.bot = _FakeUser()
        self.status = status
        self.can_delete = can_delete
        self.fail_ids = set(fail_ids)
        self.member_error = member_error
        self.deleted = []
        self.member_queries = []

    async def get_chat_member(self, chat_id, user_id):
        self.member_queries.append((chat_id, user_id))
        if self.member_error is not None:
            raise self.member_error
        return _FakeMember(self.status, self.can_delete)

    async def delete_message(self, chat_id, message_id):
        if message_id in self.fail_ids:
            raise RuntimeError("message can't be deleted")
        self.deleted.append((chat_id, message_id))
        return True


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is module state; every test starts from nothing."""
    import llm_delete
    llm_delete._SENT.clear()
    llm_delete._admin_cache.clear()
    yield
    llm_delete._SENT.clear()
    llm_delete._admin_cache.clear()


# === the definition the model is shown ===


def test_the_tool_def_has_the_openai_shape():
    from llm_delete import delete_messages_tool_def
    d = delete_messages_tool_def()
    assert d["type"] == "function"
    fn = d["function"]
    assert fn["name"] == "delete_messages"
    props = fn["parameters"]["properties"]
    assert "count" in props and "message_ids" in props
    # Neither is required: the default is "my last answer".
    assert "required" not in fn["parameters"]


def test_the_tool_def_carries_no_chat_id():
    """The model's reach is the conversation it is in.

    A chat_id parameter would let a prompt injection hidden in a
    document aim the tool at another chat. The same reasoning the
    sandbox's session_id follows (T57 #S16).
    """
    from llm_delete import delete_messages_tool_def
    props = delete_messages_tool_def()["function"]["parameters"]["properties"]
    assert "chat_id" not in props


def test_the_tool_name_is_registered_for_the_isolation_layer():
    from tool_isolation import KNOWN_TOOL_NAMES
    assert "delete_messages" in KNOWN_TOOL_NAMES, (
        "a tool that is not in KNOWN_TOOL_NAMES is soft-rejected by the "
        "isolation layer, and the model loops on it")


def test_the_prompt_mentions_the_tool():
    """The model has to know the tool exists, or it answers in prose."""
    from prompt_text.group_context import GROUP_CONTEXT
    assert "delete_messages" in GROUP_CONTEXT


# === the registry ===


def test_the_registry_remembers_answers_as_groups():
    from llm_delete import new_answer_group, remember_answer
    group_a = new_answer_group()
    group_b = new_answer_group()
    remember_answer(100, 11, group_a)
    remember_answer(100, 12, group_a)   # the same answer, second part
    remember_answer(100, 13, group_b)
    import llm_delete
    groups = llm_delete._answer_groups(100, 1)
    assert groups == [[13]], (
        f"the last answer is one group, newest first: {groups!r}")
    groups = llm_delete._answer_groups(100, 2)
    assert groups == [[13], [12, 11]], (
        f"two answers, each whole: {groups!r}")


def test_the_registry_is_per_chat():
    from llm_delete import new_answer_group, remember_answer
    import llm_delete
    group = new_answer_group()
    remember_answer(100, 11, group)
    remember_answer(200, 21, group)
    assert llm_delete._answer_groups(200, 1) == [[21]]
    assert llm_delete._answer_groups(300, 1) == []


def test_the_registry_forgets_what_was_deleted():
    from llm_delete import forget_messages, new_answer_group, remember_answer
    import llm_delete
    group = new_answer_group()
    remember_answer(100, 11, group)
    forget_messages(100, [11])
    assert llm_delete._answer_groups(100, 1) == []


# === the rights ===


@pytest.mark.asyncio
async def test_an_administrator_with_the_right_may_delete():
    from llm_delete import bot_may_delete
    bot = _FakeBot(status="administrator", can_delete=True)
    allowed, why = await bot_may_delete(bot, 100, "supergroup")
    assert allowed is True, why


@pytest.mark.asyncio
async def test_a_plain_member_may_not_delete():
    from llm_delete import bot_may_delete
    bot = _FakeBot(status="member")
    allowed, why = await bot_may_delete(bot, 100, "supergroup")
    assert allowed is False
    assert "administrator" in why


@pytest.mark.asyncio
async def test_an_administrator_without_the_right_may_not_delete():
    from llm_delete import bot_may_delete
    bot = _FakeBot(status="administrator", can_delete=False)
    allowed, why = await bot_may_delete(bot, 100, "supergroup")
    assert allowed is False
    assert "delete messages" in why


@pytest.mark.asyncio
async def test_the_rights_are_checked_once_and_cached():
    """The check costs an API call; it is not asked twice in a row."""
    from llm_delete import bot_may_delete
    bot = _FakeBot(status="administrator", can_delete=True)
    await bot_may_delete(bot, 100, "supergroup")
    await bot_may_delete(bot, 100, "supergroup")
    assert len(bot.member_queries) == 1


@pytest.mark.asyncio
async def test_a_private_chat_needs_no_administrator():
    from llm_delete import bot_may_delete
    bot = _FakeBot(status="member")
    allowed, why = await bot_may_delete(bot, 100, "private")
    assert allowed is True, why


# === the executor ===


@pytest.mark.asyncio
async def test_it_deletes_the_bots_last_answer():
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    group = new_answer_group()
    remember_answer(100, 11, group)
    remember_answer(100, 12, group)
    bot = _FakeBot()
    text, extras = await execute_delete_messages(
        {}, bot=bot, chat_id=100, chat_type="supergroup")
    assert sorted(bot.deleted) == [(100, 11), (100, 12)], (
        f"the whole answer goes, both parts: {bot.deleted!r}")
    assert extras["deleted"] == [12, 11]
    assert "deleted 2" in text


@pytest.mark.asyncio
async def test_count_removes_that_many_answers():
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    for i, mid in enumerate((11, 12, 13)):
        remember_answer(100, mid, new_answer_group())
    bot = _FakeBot()
    await execute_delete_messages(
        {"count": 2}, bot=bot, chat_id=100, chat_type="supergroup")
    assert sorted(m for _, m in bot.deleted) == [12, 13], (
        f"the two newest answers: {bot.deleted!r}")


@pytest.mark.asyncio
async def test_explicit_ids_win_over_count():
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    remember_answer(100, 11, new_answer_group())
    bot = _FakeBot()
    await execute_delete_messages(
        {"count": 1, "message_ids": [77]}, bot=bot, chat_id=100,
        chat_type="supergroup")
    assert bot.deleted == [(100, 77)]


@pytest.mark.asyncio
async def test_it_refuses_more_than_the_cap():
    from llm_delete import execute_delete_messages
    bot = _FakeBot()
    text, extras = await execute_delete_messages(
        {"count": 21}, bot=bot, chat_id=100, chat_type="supergroup")
    assert bot.deleted == []
    assert "limit" in text
    assert extras is None


@pytest.mark.asyncio
async def test_it_refuses_a_count_that_is_not_a_number():
    from llm_delete import execute_delete_messages
    bot = _FakeBot()
    text, _ = await execute_delete_messages(
        {"count": "many"}, bot=bot, chat_id=100, chat_type="supergroup")
    assert bot.deleted == []
    assert "not a number" in text


@pytest.mark.asyncio
async def test_it_says_it_does_not_know_when_there_is_nothing_to_delete():
    from llm_delete import execute_delete_messages
    bot = _FakeBot()
    text, extras = await execute_delete_messages(
        {}, bot=bot, chat_id=100, chat_type="supergroup")
    assert bot.deleted == []
    assert "do not know" in text
    assert extras is None


@pytest.mark.asyncio
async def test_it_refuses_where_the_bot_may_not_delete():
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    remember_answer(100, 11, new_answer_group())
    bot = _FakeBot(status="member")
    text, extras = await execute_delete_messages(
        {}, bot=bot, chat_id=100, chat_type="supergroup")
    assert bot.deleted == [], "nothing is deleted without the right"
    assert "refused" in text
    assert extras is None


@pytest.mark.asyncio
async def test_telegrams_own_refusal_is_reported_not_hidden():
    """A message Telegram keeps is not a message that was deleted."""
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    remember_answer(100, 11, new_answer_group())
    remember_answer(100, 12, new_answer_group())
    bot = _FakeBot(fail_ids={11})
    text, extras = await execute_delete_messages(
        {"count": 2}, bot=bot, chat_id=100, chat_type="supergroup")
    assert [m for _, m in bot.deleted] == [12]
    assert "deleted 1 of 2" in text
    assert "can't be deleted" in text
    assert extras["failed"] == [11]


@pytest.mark.asyncio
async def test_a_deleted_message_is_forgotten():
    """The second attempt must not try to delete it again."""
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    import llm_delete
    remember_answer(100, 11, new_answer_group())
    bot = _FakeBot()
    await execute_delete_messages(
        {}, bot=bot, chat_id=100, chat_type="supergroup")
    assert bot.deleted == [(100, 11)]
    assert llm_delete._answer_groups(100, 1) == [], (
        "the message is gone from the chat, so it is gone from the "
        "registry too")


# === the database is not touched ===


def test_the_module_does_not_import_the_database():
    """The operator's rule, as a tripwire.

    "sqlite наша священная корова -- источник истины ... Базу не
    трогаем." The cleanest way to keep a module away from the database
    is to make it unable to reach it: no import, no reference.
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
        f"the delete tool imports {sorted(imported)} -- the bot's "
        "history is not the tool's to change")
    assert "sqlite3" not in imported
    assert "storage" not in llm_delete.__dict__, (
        "the module holds a reference to the storage module")


@pytest.mark.asyncio
async def test_deleting_a_message_does_not_write_to_the_database(monkeypatch):
    """The store is made to shout if anything asks it for anything."""
    import storage

    def _boom(*a, **kw):
        raise AssertionError(
            "the delete tool touched the bot's database -- the history "
            "is the record of what happened, and it is not the tool's "
            "to change")

    monkeypatch.setattr(storage, "get_store", _boom)

    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    remember_answer(100, 11, new_answer_group())
    bot = _FakeBot()
    text, _ = await execute_delete_messages(
        {}, bot=bot, chat_id=100, chat_type="supergroup")
    assert "deleted 1" in text
    assert bot.deleted == [(100, 11)]


@pytest.mark.asyncio
async def test_the_dispatcher_routes_the_tool():
    """The name is dispatched, not soft-rejected."""
    from llm_dispatch import DispatchDeps, execute_tool
    from llm_delete import new_answer_group, remember_answer
    remember_answer(100, 11, new_answer_group())
    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=100, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    result = await execute_tool("delete_messages", {}, deps)
    assert bot.deleted == [(100, 11)]
    assert "deleted 1" in result.text
    assert result.ledger is not None
    assert result.ledger["name"] == "delete_messages"
