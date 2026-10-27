"""One person, one name in the record.

The record is the source of truth -- the operator keeps this history for
a LoRA dataset -- and it is written by two paths: the text handler, and
the group-absorb path ("a human replied to another human in a group").
Until 2026-10-27 the two paths each had their own resolver, and they
disagreed: the text handler wrote "Vasisualy Lohankin" and the absorb
path wrote "Vasisualy", for the same person, in the same chat. A record
that calls one person two names cannot be searched by name, and a model
reading both rows sees two people where there is one.

The operator's decision: the full Telegram nickname is canonical, and
anything shorter is an artefact of a second resolver.

These tests drive the absorb path -- the one that was wrong -- and
check what it actually WRITES, because a test of the resolver alone
cannot see which resolver a writer uses.
"""
import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))


def _user(uid, first, last=None, username=None):
    return SimpleNamespace(id=uid, is_bot=False, first_name=first,
                           last_name=last, username=username)


#: The operator's Telegram identity, in the shape Telegram sends it.
OPERATOR = _user(286293081, "Vasisualy", "Lohankin", "V_bot")


def _group_reply(target=OPERATOR, author=OPERATOR):
    return SimpleNamespace(
        update_id=4242,
        message=SimpleNamespace(
            message_id=7,
            text="ok, deal",
            caption=None,
            from_user=author,
            message_thread_id=None,
            reply_to_message=SimpleNamespace(
                message_id=6, text="the original", caption=None,
                from_user=target),
        ),
        effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
    )


@pytest.fixture
def written(monkeypatch):
    """Run the group-absorb path and return the rows it wrote."""
    import call_llama
    import dispatch_group

    rows = []

    async def _persist(chat_id, thread_id, role, content, sender_name=None,
                       telegram_message_id=None):
        rows.append({"content": content, "sender_name": sender_name,
                     "telegram_message_id": telegram_message_id})

    async def _already_recorded(chat_id, message_id):
        return False

    async def _load_history(chat_id, thread_id):
        return []

    async def _call_llama(*args, **kwargs):
        return None

    monkeypatch.setattr(dispatch_group, "_persist_message", _persist)
    monkeypatch.setattr(dispatch_group, "_already_recorded", _already_recorded)
    monkeypatch.setattr(dispatch_group, "_load_history", _load_history)
    monkeypatch.setattr(call_llama, "call_llama", _call_llama)

    def _run(upd):
        asyncio.run(dispatch_group._handle_reply_to_other_user(upd, None))
        return rows

    return _run


def test_the_absorb_path_writes_the_full_name(written):
    """The row the absorb path wrote on 2026-10-27 said "Vasisualy"."""
    rows = written(_group_reply())
    assert rows, "the absorb path wrote nothing -- the check below would pass"
    for row in rows:
        assert row["sender_name"] == "Vasisualy Lohankin", (
            f"the record calls the operator {row['sender_name']!r}; the "
            "canonical name is the full Telegram nickname")


def test_the_absorb_path_names_the_target_the_same_way(written):
    """The prefix that points at the replied-to message carries a name
    too, and it used to prefer the handle ("@V_bot")."""
    rows = written(_group_reply())
    reply = [r for r in rows if r["telegram_message_id"] == 7]
    assert reply, "the reply itself was not written"
    assert reply[0]["content"].startswith("[reply to Vasisualy Lohankin]"), (
        f"the prefix names somebody else: {reply[0]['content']!r}")


def test_both_writers_use_the_same_resolver(written):
    """The text handler and the absorb path must produce the same string
    for the same person -- not merely a similar one."""
    import handlers

    rows = written(_group_reply())
    for row in rows:
        assert row["sender_name"] == handlers._sender_display_name(OPERATOR)


def test_there_is_one_resolver_and_not_two():
    """The fix is a single implementation, not two resolvers that happen
    to agree today."""
    import handlers
    import sender_name

    assert handlers._sender_display_name is sender_name.display_name, (
        "handlers has its own copy of the naming policy; the record calls "
        "one person by two names the moment the two disagree")


def test_the_absorb_module_does_not_resolve_names_itself():
    """Structural: the module that writes the rows must not read the
    name off the user itself. It asks the policy."""
    tree = ast.parse((BOT / "dispatch_group.py").read_text(encoding="utf-8"))
    asked_for = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)):
            asked_for.add(node.args[1].value)
    own = asked_for & {"first_name", "last_name", "username"}
    assert not own, (
        f"dispatch_group asks the user for {sorted(own)} itself; the naming "
        "policy lives in sender_name.py, in one place")


def test_the_comparison_can_see_a_wrong_value():
    """Control. A check that cannot fail is decoration."""
    import sender_name

    assert sender_name.display_name(OPERATOR) == "Vasisualy Lohankin"
    assert sender_name.display_name(OPERATOR) != "@V_bot", (
        "the handle is not the canonical name -- if this ever fails, the "
        "comparison above is not measuring what it claims")
    assert sender_name.display_name(_user(1, "Dima")) == "Dima"
    assert sender_name.display_name(None) is None
