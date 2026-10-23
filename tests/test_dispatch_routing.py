"""Where an update enters the bot, and what happens to an edited one.

TECH-DEBT #10's first tripwire. `dispatch.py` was 1672 lines and this file
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
# Blocks D/E/F split dispatch.py; these hold what the edit-path and startup
# tests need now. Second constants, not replacements -- the `_dispatch_update`
# tests below still read `dispatch.py` and must keep doing so.
EDIT = BOT / "dispatch_edit.py"
POLLING = BOT / "dispatch_polling.py"
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
    # After block D the callee is a parameter: the polling module imports
    # nothing from `dispatch`, and `main` passes `_dispatch_update` in. The
    # property is unchanged -- spawn still wraps the routing call in a task
    # -- so pin the property and check the parameter is wired, rather than
    # grepping for a name that moved.
    spawn = _fn(_tree(POLLING), "_spawn_dispatch")
    assert "dispatch_update" in _called_names(spawn) or "_dispatch_update" in _called_names(spawn), (
        f"_spawn_dispatch no longer creates a dispatch task; the polling loop "
        f"would parse updates and drop them. Called: {sorted(_called_names(spawn))}")
    assert any(isinstance(n, ast.Name) and n.id.startswith("dispatch_update")
               for n in ast.walk(spawn)), (
        "the dispatch callable is accepted but never invoked -- spawn would "
        "return a task running None")
    assert "dispatch_update" in [a.arg for a in spawn.args.args + spawn.args.kwonlyargs], (
        "spawn must take the dispatch callable as a parameter; a module-level "
        "import here would re-grow the cycle block D removed")


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
import dispatch_edit  # noqa: E402
import dispatch_polling  # noqa: E402


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
    # Patch `dispatch_edit`, NOT `dispatch`. After block F the function has
    # its own module: `_bot_replies` is read from `dispatch_edit`'s globals
    # and the Application comes from `dispatch_polling`'s registry. Setting
    # either on `dispatch` rebinds a name nobody reads there -- the test
    # stays green and stops testing anything.
    bot = _FakeBot()
    monkeypatch.setattr(dispatch_edit, "_bot_replies", {})
    fake = _FakeDispatcher(bot)
    monkeypatch.setattr(dispatch_polling, "_dispatcher", fake)
    monkeypatch.setattr(dispatch_polling, "get_dispatcher", lambda: fake)
    return bot


@pytest.mark.asyncio
async def test_a_command_edit_becomes_a_message_and_loses_the_edit_key(clean):
    upd = _edited("/help")
    assert await dispatch_edit._apply_edited_message(upd) is True
    assert "edited_message" not in upd, (
        "the rewritten update still carries edited_message, so PTB routes "
        "the same text to a matching MessageHandler twice")
    assert upd["message"]["text"] == "/help"


@pytest.mark.asyncio
async def test_a_plain_text_edit_is_reprocessed_as_a_message(clean):
    upd = _edited("исправленный текст")
    assert await dispatch_edit._apply_edited_message(upd) is True
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
    await dispatch_edit._apply_edited_message(upd)
    assert upd["message"]["message_thread_id"] == 88, (
        "the rewrite preserved the message but lost its forum thread; "
        "handle_text reads message_thread_id on the next line")


@pytest.mark.asyncio
async def test_the_stale_reply_is_deleted_before_the_reprocess(clean):
    dispatch_edit._bot_replies[(-100, 7)] = 555
    await dispatch_edit._apply_edited_message(_edited("правка"))
    assert clean.deleted == [(-100, 555)], (
        "the bot's previous answer is still on screen after the user "
        "fixed their message")


@pytest.mark.asyncio
async def test_no_recorded_reply_means_no_delete_call(clean):
    """And the rewrite still happens -- that is the actual feature."""
    await dispatch_edit._apply_edited_message(_edited("правка"))
    assert clean.deleted == []


@pytest.mark.asyncio
async def test_a_failed_delete_does_not_lose_the_reprocess(clean):
    """Missing delete permission is the common case, not an abort."""
    async def boom(chat_id, message_id):
        raise Exception("not enough rights")

    clean.delete_message = boom
    dispatch_edit._bot_replies[(-100, 7)] = 555
    upd = _edited("правка")
    assert await dispatch_edit._apply_edited_message(upd) is True
    assert "edited_message" not in upd


@pytest.mark.asyncio
async def test_an_update_that_already_has_a_message_is_untouched(clean):
    upd = {"update_id": 1, "message": {"message_id": 1, "text": "original"},
           "edited_message": {"message_id": 1, "text": "edited"}}
    assert await dispatch_edit._apply_edited_message(upd) is False, (
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
    await dispatch_edit._apply_edited_message(upd)
    assert upd == before, f"a no-op update was modified: {upd}"


def test_the_application_is_published_for_everyone_else_to_find():
    """The registry, and the two readers that break silently without it.

    `_dispatch_update` uses a local `app` for everything it does itself,
    so a `set_dispatcher` that does nothing changes no local behaviour:
    mutation D4 -- turning the setter's body into `pass` -- passed the
    whole 2047-test suite.

    The two readers are what makes the registry load-bearing:

    - `dispatch_webhook._register_bot_menu` finds the already-built
      Application instead of building a second one at startup;
    - `dispatch_edit._apply_edited_message` needs it to delete the stale
      reply. With no dispatcher it skips the delete and returns True, so
      edit-replace quietly stops replacing anything.

    Assert the write, then assert a reader sees it. Both halves: the
    first catches a setter that does nothing, the second catches a
    reader that reads a different name.
    """
    import dispatch_polling

    class _App:
        pass

    app = _App()
    saved = dispatch_polling.get_dispatcher()
    try:
        dispatch_polling.set_dispatcher(app)
        assert dispatch_polling.get_dispatcher() is app, (
            "set_dispatcher stored something get_dispatcher does not return"
        )
        import dispatch_edit
        src = Path(dispatch_edit.__file__).read_text(encoding="utf-8")
        assert "dispatch_polling.get_dispatcher()" in src, (
            "the edit path no longer reads the registry, so a null "
            "dispatcher silently turns edit-replace into a no-op"
        )
        assert "dispatch_polling.set_dispatcher(" in \
            (BOT / "dispatch.py").read_text(encoding="utf-8"), (
            "_dispatch_update no longer publishes the Application; the "
            "command menu and the edit path would each build their own"
        )
    finally:
        dispatch_polling.set_dispatcher(saved)
