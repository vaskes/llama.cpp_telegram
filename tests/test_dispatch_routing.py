"""Where an update enters the bot, and that there is exactly one door.

TECH-DEBT #10's first tripwire. `dispatch.py` was 1672 lines and this file
touches 4 of them, because the feature they guarded has already cost
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

**The double-handling incident.** While the rewrite was in place, an
update could hold both `message` and `edited_message`. PTB 21 routes each
field to whichever handler matches it, so the same text was handled
twice: the original bot replies, then replies again to the edit.

**The feature is gone (2026-10-24).** The operator removed edit-replace
outright: it had no live coverage and no way to get one (that needs two
people, a deliberately slow model and a human watching the message
change), the cost of keeping it was the testing, and the benefit was a
wrong-answer window that closes when the user asks again. `edited_message`
is no longer in the subscription list, so Telegram does not even send
edits.

What is left here is the property that outlived the feature: **one door**.
Every delivery mode must reach `_dispatch_update`, the update is parsed
exactly once, and nothing else parses one just to look at it. Those are
the properties whose absence cost production two crashes, and they are
worth keeping even with the feature that motivated them gone.

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
# Blocks D/E split dispatch.py; these hold what the startup tests need
# now. Second constants, not replacements -- the `_dispatch_update`
# tests below still read `dispatch.py` and must keep doing so.
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


# === the entry point =====================================================

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


# === the registry =======================================================

def test_the_application_is_published_for_everyone_else_to_find():
    """The registry, and the reader that breaks silently without it.

    `_dispatch_update` uses a local `app` for everything it does itself,
    so a `set_dispatcher` that does nothing changes no local behaviour:
    mutation D4 -- turning the setter's body into `pass` -- passed the
    whole 2047-test suite.

    The reader is what makes the registry load-bearing:
    `dispatch_webhook._register_bot_menu` finds the already-built
    Application instead of building a second one at startup.

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
        assert "dispatch_polling.get_dispatcher()" in \
            (BOT / "dispatch_webhook.py").read_text(encoding="utf-8"), (
            "the command menu no longer reads the registry, so a null "
            "dispatcher makes it build a second Application"
        )
        assert "dispatch_polling.set_dispatcher(" in \
            (BOT / "dispatch.py").read_text(encoding="utf-8"), (
            "_dispatch_update no longer publishes the Application; the "
            "command menu would build its own"
        )
    finally:
        dispatch_polling.set_dispatcher(saved)

