"""Where an update enters the bot, and what happens to an edited one.

TECH-DEBT #10's first tripwire. `dispatch.py` is 1672 lines and this file
touches 4 of them, because the feature they guard has already cost
production twice.

**The dead-block incident.** The edit-replace block lived inside
`for upd_dict in updates_raw:` in the polling loop. Production runs the
webhook, `webhook.py` hands updates straight to `_dispatch_update`, and
the polling loop is never entered. So the feature was in the source, in
the comments, and in nobody's production. Four `AttributeError: 'NoneType'
object has no attribute 'message_thread_id'` before that was noticed.

Two comments described the block as living in `_dispatch_update`. It did
not. The block was in the other file, in the branch that production does
not take, and the comments were checked by nobody -- the same class as a
stale docstring claim, except the claim pointed at a file instead of a
property.

**The double-handling incident.** After the rewrite lands, an update can
hold both `message` and `edited_message`. PTB 21 routes each field to
whichever handler matches it, so the same text is handled twice. The
original bot replies, then replies again to the edit. That is why
`_apply_edited_message` pops `edited_message` rather than merely reading
it.

So the properties worth pinning are not "the function exists":

- every delivery mode reaches `_dispatch_update`;
- the edit rewrite runs BEFORE the update is parsed, not after;
- `edited_message` is gone from the dict afterwards, on both rewrite
  paths;
- an update that already carries `message` is left alone.

Counting matters more than presence here. `de_json` called twice inside
`_dispatch_update` is the same defect wearing a different hat.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=10)

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"
sys.path.insert(0, str(BOT))

DISPATCH = BOT / "dispatch.py"
WEBHOOK = BOT / "webhook.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _fn(tree: ast.Module, name: str):
    """Any top-level function, sync or async.

    `_spawn_dispatch` is a plain `def` that returns a Task -- matching
    only AsyncFunctionDef reported "no such function" on code that has
    had it since the webhook moved.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"no top-level function {name} in this module")


def _called_names(fn):
    """Names invoked inside `fn`, in source order.

    Walks recursively on purpose: the dispatch body lives inside an
    `async with`, and a body-level scan reports "not found" on correct
    code. That mistake was made twice in one session and both times the
    tripwire went green on a tree it was supposed to guard.
    """
    return [n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]


# === the entry point, and what runs before the parse ==================

def test_the_edit_rewrite_runs_before_the_update_is_parsed():
    """Order, not existence.

    Parsing first would hand PTB an update with both `message` and
    `edited_message` in it, and the edit would be routed a second time.
    """
    fn = _fn(_tree(DISPATCH), "_dispatch_update")
    # every call, not just bare names: `Update.de_json` is an attribute
    edit = next((n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_apply_edited_message"), None)
    parse = next((n for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "de_json"), None)
    assert edit is not None, "_dispatch_update no longer calls _apply_edited_message"
    assert parse is not None, "_dispatch_update no longer parses the update"
    assert edit.lineno < parse.lineno, (
        "the edit rewrite has to run before Update.de_json; after it, the "
        "dict already carries both keys and PTB handles the text twice")


def test_the_update_is_parsed_exactly_once():
    """`de_json` twice is `message_thread_id` twice."""
    fn = _fn(_tree(DISPATCH), "_dispatch_update")
    n = sum(1 for x in ast.walk(fn)
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
            and x.func.attr == "de_json")
    assert n == 1, (
        f"_dispatch_update calls Update.de_json {n} times; each one is a "
        f"second chance to route an update that was already routed")


# === both delivery modes reach it ====================================

def test_the_webhook_delivery_path_reaches_dispatch_update():
    src = WEBHOOK.read_text(encoding="utf-8")
    assert "from dispatch import _dispatch_update" in src, (
        "webhook.py no longer imports _dispatch_update; the webhook must "
        "reach the same entry point as the polling loop, or whatever "
        "_dispatch_update does applies to one mode only")
    assert re.search(r"await\s+_dispatch_update\s*\(", src), (
        "webhook.py imports _dispatch_update but never awaits it")


def test_the_polling_delivery_path_reaches_dispatch_update():
    """Via `_spawn_dispatch`, which is a task factory and nothing else."""
    spawn = _fn(_tree(DISPATCH), "_spawn_dispatch")
    assert "_dispatch_update" in _called_names(spawn), (
        "_spawn_dispatch no longer creates a _dispatch_update task; the "
        "polling loop would parse updates and drop them")


def test_no_module_parses_an_update_just_to_route_it():
    """`Update.de_json` outside the entry point is a second entry point.

    webhook.py does call it, to validate the body before handing the
    dict on -- parsing for a decision and parsing for a route are
    different, and only one of them hands the update to a handler. This
    test is here to make that distinction a deliberate one: if a new
    `de_json` appears somewhere else, somebody has to say out loud that
    it is the new front door.
    """
    allowed = {
        DISPATCH.resolve(): "_dispatch_update -- the entry point",
        WEBHOOK.resolve(): "validation before handing the dict to _dispatch_update",
    }
    offenders = []
    for path in sorted(BOT.glob("*.py")):
        if path.name.startswith("selftest") or path.name.startswith("test_"):
            continue
        if not re.search(r"\bde_json\b", path.read_text(encoding="utf-8")):
            continue
        if path.resolve() not in allowed:
            offenders.append(path.name)
    assert not offenders, (
        "modules that call Update.de_json outside the known entry points: "
        f"{offenders}")

# === behaviour: call it, do not read it =============================

import dispatch  # noqa: E402  -- needs the sys.path insert above


class _FakeBot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _FakeDispatcher:
    def __init__(self, bot=None):
        self.bot = bot or _FakeBot()


def _edited(text="hi", chat_id=-100, msg_id=7, thread_id=None, **extra):
    msg = {"message_id": msg_id, "text": text,
           "chat": {"id": chat_id, "type": "supergroup"},
           "from": {"id": 42, "username": "tester"}}
    if thread_id is not None:
        msg["message_thread_id"] = thread_id
    msg.update(extra)
    return {"update_id": 900, "edited_message": msg}


@pytest.fixture
def clean(monkeypatch):
    """Isolated module globals, restored even if a test fails.

    `_bot_replies` is module state that real traffic writes to; a test
    that leaks a key into it makes the next test's delete call fire for
    the wrong reason and pass.
    """
    bot = _FakeBot()
    monkeypatch.setattr(dispatch, "_bot_replies", {})
    monkeypatch.setattr(dispatch, "_dispatcher", _FakeDispatcher(bot))
    return bot


@pytest.mark.asyncio
async def test_a_command_edit_becomes_a_message_and_loses_the_edit_key(clean):
    upd = _edited("/help")
    assert await dispatch._apply_edited_message(upd) is True
    assert "edited_message" not in upd, (
        "the rewritten update still carries edited_message, so PTB routes "
        "the same text to a matching MessageHandler twice")
    assert upd["message"]["text"] == "/help"


@pytest.mark.asyncio
async def test_a_plain_text_edit_is_reprocessed_as_a_message(clean):
    upd = _edited("исправленный текст")
    assert await dispatch._apply_edited_message(upd) is True
    assert "edited_message" not in upd
    assert upd["message"]["text"] == "исправленный текст"


@pytest.mark.asyncio
async def test_the_rewrite_keeps_the_forum_thread_id(clean):
    """The AttributeError that started all this was on this key.

    `message_thread_id` is what `handle_text` reads first. If a rewrite
    ever rebuilds the message from parts instead of moving the original
    dict, that attribute disappears and the error returns -- as an
    AttributeError inside the handler, which reads like Telegram being
    flaky rather than like a routing bug.
    """
    upd = _edited("правка", thread_id=88)
    await dispatch._apply_edited_message(upd)
    assert upd["message"]["message_thread_id"] == 88, (
        "the rewrite preserved the message but lost its forum thread; "
        "handle_text reads message_thread_id on the next line")


@pytest.mark.asyncio
async def test_the_stale_reply_is_deleted_before_the_reprocess(clean):
    dispatch._bot_replies[(-100, 7)] = 555
    await dispatch._apply_edited_message(_edited("правка"))
    assert clean.deleted == [(-100, 555)], (
        "the bot's previous answer is still on screen after the user "
        "fixed their message")


@pytest.mark.asyncio
async def test_no_recorded_reply_means_no_delete_call(clean):
    """And the rewrite still happens -- that is the actual feature."""
    await dispatch._apply_edited_message(_edited("правка"))
    assert clean.deleted == []


@pytest.mark.asyncio
async def test_a_failed_delete_does_not_lose_the_reprocess(clean):
    """Missing delete permission is the common case, not an abort."""
    async def boom(chat_id, message_id):
        raise Exception("not enough rights")

    clean.delete_message = boom
    dispatch._bot_replies[(-100, 7)] = 555
    upd = _edited("правка")
    assert await dispatch._apply_edited_message(upd) is True
    assert "edited_message" not in upd


@pytest.mark.asyncio
async def test_an_update_that_already_has_a_message_is_untouched(clean):
    upd = {"update_id": 1, "message": {"message_id": 1, "text": "original"},
           "edited_message": {"message_id": 1, "text": "edited"}}
    assert await dispatch._apply_edited_message(upd) is False, (
        "an update that already carries `message` must be left alone")
    assert upd["message"]["text"] == "original", (
        "an update carrying both keys had its message overwritten")


@pytest.mark.asyncio
@pytest.mark.parametrize("upd", [
    {"update_id": 2},
    {"update_id": 3, "callback_query": {"id": "x"}},
    {"update_id": 4, "edited_message": "not a dict"},
])
async def test_updates_without_a_rewritable_edit_are_no_ops(clean, upd):
    before = dict(upd)
    await dispatch._apply_edited_message(upd)
    assert upd == before, f"a no-op update was modified: {upd}"
