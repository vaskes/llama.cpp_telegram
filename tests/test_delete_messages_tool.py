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
def _clean_registry(tmp_path):
    """The registry is module state; every test starts from nothing."""
    import llm_delete
    llm_delete._SENT.clear()
    llm_delete._admin_cache.clear()
    llm_delete._LOG_LOADED = False
    llm_delete._GROUP_SEQ = 0
    # The log lives next to the database in the deployment. A test
    # writes into its own directory, never into the real one.
    llm_delete.SENT_LOG_PATH = str(tmp_path / "sent_messages.jsonl")
    yield
    llm_delete._SENT.clear()
    llm_delete._admin_cache.clear()
    llm_delete._LOG_LOADED = False
    llm_delete._GROUP_SEQ = 0


# === the log of what the bot sent ===


def test_the_ids_survive_a_restart(tmp_path):
    """A restart empties the memory, and the channel does not stop.

    The operator asked for the last four messages to be taken back
    right after a deploy, and the tool could not name one of them:
    the ids of the bot's own messages were only ever in memory. They
    are now in a log as well, and the log is not the database.
    """
    import llm_delete
    group = llm_delete.new_answer_group()
    llm_delete.remember_answer(100, 501, group)
    llm_delete.remember_answer(100, 502, group)

    # a restart: the memory is gone, the log is not
    llm_delete._SENT.clear()
    llm_delete._LOG_LOADED = False
    llm_delete._GROUP_SEQ = 0

    assert llm_delete.recent_ids(100) == [502, 501]
    assert llm_delete._answer_groups(100, 1) == [[502, 501]]


def test_a_restart_does_not_reuse_a_group_id(tmp_path):
    """Two answers sharing a group would be deleted as one."""
    import llm_delete
    group = llm_delete.new_answer_group()
    llm_delete.remember_answer(100, 501, group)
    llm_delete._SENT.clear()
    llm_delete._LOG_LOADED = False
    llm_delete._GROUP_SEQ = 0
    assert llm_delete.new_answer_group() != group


def test_the_log_is_the_bot_s_own_memory_and_not_the_database(tmp_path):
    """The log holds ids. The database keeps the record, untouched."""
    import llm_delete
    group = llm_delete.new_answer_group()
    llm_delete.remember_answer(100, 501, group)
    written = (tmp_path / "sent_messages.jsonl").read_text(encoding="utf-8")
    assert written.strip() == "100\t501\t%d" % group
    assert "conversation" not in written.lower()


def test_a_deleted_message_leaves_the_log(tmp_path):
    """The log says what the bot may still take back."""
    import llm_delete
    group = llm_delete.new_answer_group()
    llm_delete.remember_answer(100, 501, group)
    llm_delete.remember_answer(100, 502, group)
    llm_delete.forget_messages(100, [501])
    written = (tmp_path / "sent_messages.jsonl").read_text(encoding="utf-8")
    assert "501" not in written
    assert "502" in written


# === the ids the model is shown ===


def test_the_tool_description_carries_the_ids_the_model_needs():
    """A model with an id uses it. A model without one invents one."""
    from llm_delete import (delete_messages_tool_def, new_answer_group,
                             remember_answer)
    group = new_answer_group()
    remember_answer(100, 701, group)
    remember_answer(100, 702, group)
    desc = delete_messages_tool_def(
        chat_id=100, reply_to_message_id=699)["function"]["description"]
    assert "701" in desc and "702" in desc, desc
    assert "699" in desc, desc
    assert "NEVER invent" in desc, desc


def test_the_tool_description_says_what_the_reply_means():
    """The pointed-at id has to be unmistakable.

    On 2026-10-26 the description carried the reply-to id and the
    model still answered with numbers it had made up. So the id comes
    first, it is named as the message the user replied to, and the
    sentence says what to do with it.
    """
    from llm_delete import delete_messages_tool_def
    desc = delete_messages_tool_def(
        chat_id=100, reply_to_message_id=699)["function"]["description"]
    assert "REPLIED TO has id 699" in desc, desc
    assert "message_ids=[699]" in desc, desc


def test_the_description_names_the_message_the_user_just_wrote():
    """"Delete the message I just wrote" needs the current id.

    The live test of 2026-10-26 16:08 is the reason. The operator wrote
    "delete the message I just wrote" as a reply to an old message of
    the bot's, and the model took the reply-to id -- an ancient one --
    and the answer came back as a story about the 48-hour window. The
    bot knew the id of the message the user had just written and did
    not offer it, so the only id in reach was the wrong one.
    """
    from llm_delete import delete_messages_tool_def
    desc = delete_messages_tool_def(
        chat_id=100, reply_to_message_id=25,
        current_message_id=3264)["function"]["description"]
    assert "JUST WROTE has id 3264" in desc, desc
    assert "message_ids=[3264]" in desc, desc
    # the current message comes FIRST: it is the one "I just wrote"
    assert desc.index("3264") < desc.index("25"), desc
    assert "NEVER invent" in desc, desc


def test_the_current_message_id_is_in_reach():
    """The message the user just wrote is deletable by the tool."""
    from llm_delete import authorized_ids
    assert 3264 in authorized_ids(100, 25, 3264)
    assert 3264 in authorized_ids(100, None, 3264)
    assert 3264 not in authorized_ids(100, 25, None)


def test_the_recent_ids_are_the_bots_own_messages_newest_first():
    from llm_delete import (new_answer_group, recent_ids, remember_answer)
    group = new_answer_group()
    remember_answer(100, 701, group)
    remember_answer(100, 702, group)
    assert recent_ids(100) == [702, 701]
    assert recent_ids(999) == []
    assert recent_ids(None) == []


# === the id of the message the user replied to ===


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, message_id, chat_id, reply_to=None):
        self.message_id = message_id
        self.chat = _FakeChat(chat_id)
        self.reply_to_message = reply_to


class _FakeUpdate:
    def __init__(self, message, chat_id):
        self.message = message
        self.effective_chat = _FakeChat(chat_id)


def test_the_reply_to_id_is_read_from_the_update():
    """\"Delete this one\" is how a message is usually asked away."""
    from handlers import _reply_to_message_id
    bot_message = _FakeMessage(703, 100)
    update = _FakeUpdate(_FakeMessage(704, 100, reply_to=bot_message), 100)
    assert _reply_to_message_id(update) == 703


def test_a_reply_to_another_chat_is_dropped():
    """The id of a message in another chat means nothing here."""
    from handlers import _reply_to_message_id
    foreign = _FakeMessage(703, 200)
    update = _FakeUpdate(_FakeMessage(704, 100, reply_to=foreign), 100)
    assert _reply_to_message_id(update) is None


def test_a_message_without_a_reply_has_no_anchor():
    from handlers import _reply_to_message_id
    update = _FakeUpdate(_FakeMessage(704, 100), 100)
    assert _reply_to_message_id(update) is None


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
    rights = await bot_may_delete(bot, 100, "supergroup")
    assert rights.allowed is True, rights.reason
    assert rights.others is True, (
        "an administrator with the delete right may delete other "
        "people's messages here")


@pytest.mark.asyncio
async def test_a_plain_member_may_not_delete():
    from llm_delete import bot_may_delete
    bot = _FakeBot(status="member")
    rights = await bot_may_delete(bot, 100, "supergroup")
    assert rights.allowed is False
    assert "administrator" in rights.reason


@pytest.mark.asyncio
async def test_an_administrator_without_the_right_still_gets_the_tool():
    """Half a right is still a right, and the operator is told which half.

    An administrator without the "delete messages" right may take back
    the bot's own messages and may not touch anyone else's. The tool
    stays available -- it is how the bot cleans up after itself -- and
    the note says what it cannot do.
    """
    from llm_delete import bot_may_delete
    bot = _FakeBot(status="administrator", can_delete=False)
    rights = await bot_may_delete(bot, 100, "supergroup")
    assert rights.allowed is True, (
        "the bot may still take back its own messages, so the tool is "
        "worth advertising")
    assert "delete messages" in rights.reason
    assert "own messages" in rights.reason
    assert rights.others is False, (
        "and it may NOT delete other people's messages here -- the "
        "refusal it gets from Telegram is about that, not about the age "
        "of the message")


@pytest.mark.asyncio
async def test_unreadable_rights_do_not_hide_the_tool():
    """An inconclusive check is not a refusal.

    The bot is in the chat by construction -- it just received an
    update from it -- so a failure to read the rights is a quirk of
    the chat, not a verdict. Hiding the tool because of a quirk would
    leave the operator with a tool that does not exist and no
    explanation. Telegram has the last word on what may be deleted.
    """
    from llm_delete import bot_may_delete
    bot = _FakeBot(member_error=RuntimeError("Chat not found"))
    rights = await bot_may_delete(bot, 100, "channel")
    assert rights.allowed is True, (
        "the bot is in this chat by construction, so a failed rights "
        "check must not take the tool away")
    assert "could not read" in rights.reason
    assert rights.others is None, (
        "the check failed, it did not answer -- that is not the same as "
        "'no'")


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
    rights = await bot_may_delete(bot, 100, "private")
    assert rights.allowed is True, rights.reason


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
    remember_answer(100, 77, new_answer_group())
    remember_answer(100, 11, new_answer_group())
    bot = _FakeBot()
    await execute_delete_messages(
        {"count": 1, "message_ids": [77]}, bot=bot, chat_id=100,
        chat_type="supergroup")
    assert bot.deleted == [(100, 77)]


# === ids the bot may touch ===


@pytest.mark.asyncio
async def test_an_invented_id_is_refused_without_asking_telegram():
    """The production case, 2026-10-26 14:01 MSK.

    The operator said "delete this message of mine" in LlmChatPlace.
    The model answered with ids 706 and 707 -- numbers it had made up
    -- and the bot is an administrator there, so asking Telegram to
    delete them would be a real attempt to delete a stranger's
    message. The tool refuses them itself, and nothing is deleted.
    """
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    remember_answer(100, 3213, new_answer_group())
    bot = _FakeBot()
    text, extras = await execute_delete_messages(
        {"message_ids": [706, 707]}, bot=bot, chat_id=100,
        chat_type="supergroup", reply_to_message_id=3220)
    assert bot.deleted == [], "an invented id must not reach Telegram"
    assert "refused" in text
    assert "706" in text and "707" in text
    assert "3220" in text, (
        "the refusal names the id that IS in reach, or the model has "
        "nowhere to go")


@pytest.mark.asyncio
async def test_the_message_the_user_replied_to_can_be_deleted():
    """The way "delete this one" is meant to work."""
    from llm_delete import execute_delete_messages
    bot = _FakeBot()
    text, extras = await execute_delete_messages(
        {"message_ids": [3220]}, bot=bot, chat_id=100,
        chat_type="supergroup", reply_to_message_id=3220)
    assert bot.deleted == [(100, 3220)]
    assert "deleted 1" in text


@pytest.mark.asyncio
async def test_the_bots_own_messages_can_be_deleted_by_id():
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    remember_answer(100, 3213, new_answer_group())
    bot = _FakeBot()
    await execute_delete_messages(
        {"message_ids": [3213]}, bot=bot, chat_id=100,
        chat_type="supergroup")
    assert bot.deleted == [(100, 3213)]


@pytest.mark.asyncio
async def test_a_known_id_goes_through_and_an_invented_one_does_not():
    """Half right is still right for the half that is right."""
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    remember_answer(100, 3213, new_answer_group())
    bot = _FakeBot()
    text, extras = await execute_delete_messages(
        {"message_ids": [3213, 706]}, bot=bot, chat_id=100,
        chat_type="supergroup")
    assert bot.deleted == [(100, 3213)]
    assert extras["refused"] == [706]
    assert "706" in text


def test_the_refusal_names_the_cause_telegram_gave():
    """"Message can't be deleted" has two causes, and the bot knows which.

    The live test: the model explained Telegram's refusal as "the bot
    is not an administrator here" -- and the bot WAS an administrator,
    with the delete right. The real cause was Telegram's 48-hour
    window. The bot's own rights are known here, so the two are told
    apart instead of guessed at.
    """
    from llm_delete import telegram_refusal
    exc = RuntimeError("Message can't be deleted")
    with_right = telegram_refusal(exc, may_delete=True)
    assert "Message can't be deleted" in with_right, (
        "Telegram's own words stay in the report")
    assert "48 hours" in with_right
    without_right = telegram_refusal(exc, may_delete=False)
    assert "48 hours" not in without_right
    assert "not allowed" in without_right


@pytest.mark.asyncio
async def test_an_administrator_without_the_right_is_told_about_the_right():
    """The cause of Telegram's refusal must match the rights the bot has.

    The live failure of 2026-10-26: the model explained "Message can't
    be deleted" as "the bot is not an administrator here", and the bot
    WAS one. The other half of that story is the bot that IS an
    administrator but has no delete right -- there Telegram's refusal
    is about the right, not about the 48-hour window.
    """
    from llm_delete import execute_delete_messages, new_answer_group, \
        remember_answer
    remember_answer(100, 11, new_answer_group())
    bot = _FakeBot(status="administrator", can_delete=False, fail_ids={11})
    text, extras = await execute_delete_messages(
        {}, bot=bot, chat_id=100, chat_type="supergroup")
    assert bot.deleted == []
    assert "48 hours" not in text, (
        "the bot has no delete right here, so the refusal is about the "
        "right and not about the age of the message")
    assert "not allowed" in text


def test_a_message_that_is_gone_is_named_as_gone():
    from llm_delete import telegram_refusal
    text = telegram_refusal(RuntimeError("Message to delete not found"),
                            may_delete=True)
    assert "gone" in text
    assert "not found" in text


def test_the_description_leads_with_the_id_the_user_points_at():
    """The ids come first, and each says whose message it is.

    "YOUR last messages" was read from the user's side -- the model
    took the bot's own ids for the user's messages. The wording now
    says whose message each id is.
    """
    from llm_delete import delete_messages_tool_def, new_answer_group, \
        remember_answer
    remember_answer(100, 3213, new_answer_group())
    description = delete_messages_tool_def(
        chat_id=100, reply_to_message_id=3220)["function"]["description"]
    assert description.index("3220") < description.index("3213"), (
        "the id the user points at comes first")
    assert "BOT's messages" in description
    assert "YOUR last messages" not in description


def test_the_description_says_so_when_the_bot_knows_no_ids():
    """The deployment window of 2026-10-26: the id log was empty, so
    the description carried no ids at all, and the model made some up.
    Now it says what it does not know."""
    from llm_delete import delete_messages_tool_def
    description = delete_messages_tool_def(
        chat_id=100, reply_to_message_id=3220)["function"]["description"]
    assert "does not know their ids" in description


@pytest.mark.asyncio
async def test_the_reply_to_id_reaches_the_tool_through_the_dispatcher():
    """The id has to arrive at EXECUTION time, not only in the description.

    The description is built in `call_llama`; the tool runs in the
    dispatcher. If the id stops travelling between them, the model is
    told about a message the tool may not touch -- and the model, which
    cannot tell the difference, invents an id instead.
    """
    from llm_dispatch import DispatchDeps, execute_tool
    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=100, current_message_id=5,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={},
                         reply_to_message_id=3220)
    result = await execute_tool("delete_messages", {"message_ids": [3220]},
                                deps)
    assert bot.deleted == [(100, 3220)], (
        "the reply-to id must reach the tool, or 'delete this one' has "
        "no id to work with")


def test_the_journal_keeps_the_ids_across_a_restart(tmp_path):
    """The ids of the bot's own messages must survive a deploy.

    Measured on production, 2026-10-26: the log of ids was empty for
    a whole deployment window, the tool description carried no ids
    because of it, and the model answered "delete this message" with
    numbers it had made up. The log is the bot's memory of its own
    messages; it lives next to the database, not inside it.
    """
    import llm_delete
    group = llm_delete.new_answer_group()
    llm_delete.remember_answer(100, 3213, group)
    written = (tmp_path / "sent_messages.jsonl").read_text(encoding="utf-8")
    assert "3213" in written
    # a restart: the memory is gone, the log is not
    llm_delete._SENT.clear()
    llm_delete._LOG_LOADED = False
    llm_delete._GROUP_SEQ = 0
    assert llm_delete.recent_ids(100) == [3213]
    assert 3213 in llm_delete.authorized_ids(100)


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


async def test_the_current_message_id_reaches_the_tool():
    """The id of the message the user just wrote must get to the tool.

    Live test of 2026-10-26 16:08: the operator wrote "delete the
    message I just wrote" and the model deleted the wrong message --
    the one the operator had replied to, because the current id was
    not in the description and so was not in reach.
    """
    from llm_dispatch import DispatchDeps, execute_tool
    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=100, current_message_id=3264,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    result = await execute_tool(
        "delete_messages", {"message_ids": [3264]}, deps)
    assert bot.deleted == [(100, 3264)], bot.deleted
    assert "3264" in result.text


async def test_the_current_message_id_is_refused_when_it_is_not_the_turn():
    """Only the ids of THIS turn are in reach."""
    from llm_dispatch import DispatchDeps, execute_tool
    bot = _FakeBot()
    deps = DispatchDeps(bot=bot, chat_id=100, current_message_id=3264,
                         chat_type="supergroup", iteration=1,
                         custom_tools={}, donsetch_tools={})
    result = await execute_tool(
        "delete_messages", {"message_ids": [9999]}, deps)
    assert bot.deleted == [], bot.deleted
    assert "refused" in result.text
