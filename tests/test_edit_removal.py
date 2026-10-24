"""The edit-replace feature is gone, and this file says it is gone.

Added 2026-10-24, with the removal. The operator's decision: a user who
edits a message waits for the answer to the wrong question and asks
again. The feature is out, the state it needed is out, and the
subscription no longer asks Telegram for edits.

Three things have to hold, and each of them is the kind of thing that
looks fine until it is not:

1. **an edited message is not answered.** Not "the old answer is not
   deleted" -- that was the old feature -- but *not answered at all*.
   The subscription is the first line of defence, and it cannot be the
   only one: `allowed_updates` lives on Telegram's side and survives
   until the webhook is re-registered, so a bot restarted against a
   stale registration keeps receiving edits.

2. **the state the feature needed is gone with it.** `_bot_replies` was
   written on every single reply and read by nobody else. A map that is
   written on every call and read by no one is not a cache, it is a leak
   with a lookup table.

3. **a normal message is still answered.** A guard that drops everything
   satisfies "an edited message is not answered" perfectly. So the
   property needs both directions, and the second one is what makes the
   first one mean something.

The behavioural test drives the real `_dispatch_update` with a stubbed
engine, the way `test_dispatch_group_seam.py` does -- a unit test of
the guard function would pass with the guard unreachable.
"""
import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.tech_debt(
    item=10,
    reason="Pins the removal of the edit-replace feature: an edited "
           "message is not answered, the write-only map is gone, and a "
           "normal message still is.")

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

BOT_ID = 777


def _user(uid=42, name="bob", username="bob"):
    return SimpleNamespace(id=uid, is_bot=False, first_name=name,
                           username=username)


def _message(text="привет"):
    return SimpleNamespace(
        message_id=8,
        text=text,
        caption=None,
        from_user=_user(),
        message_thread_id=None,
        reply_to_message=None,
    )


def _edited(text="привет, поправка"):
    return SimpleNamespace(
        update_id=9001,
        message=None,
        edited_message=_message(text),
        effective_chat=SimpleNamespace(id=42, type="private"),
    )


def _plain(text="привет"):
    return SimpleNamespace(
        update_id=9002,
        message=_message(text),
        edited_message=None,
        effective_chat=SimpleNamespace(id=42, type="private"),
    )


class _Harness:
    """Drives `_dispatch_update` and records what reached the handlers.

    The Application is a stub and the engine is never reached: the
    question is whether the update got past the guard, not what the
    model would have said.
    """

    def __init__(self):
        self.processed = []

    async def run(self, upd, monkeypatch):
        import dispatch
        import dispatch_polling

        harness = self
        registry = {}

        async def _process(u):
            harness.processed.append(u)

        async def _initialize():
            return None

        app = SimpleNamespace(
            bot=SimpleNamespace(id=BOT_ID),
            process_update=_process,
            initialize=_initialize,
            add_error_handler=lambda fn: None,
        )

        monkeypatch.setattr(dispatch, "_build_application", lambda **kw: app)
        monkeypatch.setattr(dispatch_polling, "set_dispatcher",
                            lambda a: registry.__setitem__("app", a))
        monkeypatch.setattr(dispatch_polling, "get_dispatcher",
                            lambda: registry.get("app"))
        monkeypatch.setattr(dispatch.Update, "de_json",
                            lambda d, bot: upd, raising=False)
        await dispatch._dispatch_update({"update_id": upd.update_id})
        return self


@pytest.mark.asyncio
async def test_an_edited_message_is_not_answered(monkeypatch):
    """The whole point of the removal."""
    h = await _Harness().run(_edited(), monkeypatch)
    assert h.processed == [], (
        "the bot answered an edited message. The edit-replace feature was "
        "removed on 2026-10-24: an edited message is ignored, and the "
        "answer to the original stands.")


@pytest.mark.asyncio
async def test_a_normal_message_is_still_answered(monkeypatch):
    """The other direction, and the one that makes the first mean something.

    A guard that dropped every update would pass the test above.
    """
    h = await _Harness().run(_plain(), monkeypatch)
    assert len(h.processed) == 1, (
        "a normal message did not reach the handlers -- the guard is "
        "dropping more than the edits")


def test_the_write_only_map_is_gone():
    """`_bot_replies` and its two accessors.

    It was written on every reply and read by nobody else, and the
    feature that read it is gone. What is left is a write-only map: a
    leak with a lookup table.
    """
    import state

    for name in ("_bot_replies", "_register_bot_reply", "_pop_bot_reply",
                 "_BOT_REPLIES_MAXSIZE"):
        assert not hasattr(state, name), (
            f"state.{name} is back. It was written on every reply and read "
            "by nobody else -- the map and the feature that read it went "
            "together.")


def _identifiers(tree):
    """Every name the code actually uses.

    AST, not a substring scan. A comment that explains what was removed
    is exactly what should survive the removal -- the history is the
    point -- and a substring scan cannot tell a comment from a call.
    The scan that could not tell them apart would have had to delete
    the explanation, and then the next reader would have no idea why
    the feature is gone.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
    return names


def test_no_production_module_uses_the_removed_names():
    """A name that comes back is a route back.

    The names are checked across the whole package, not in one file:
    the feature was spread over `dispatch_edit.py`, `state.py`,
    `handlers.py` and `dispatch.py`, and a partial resurrection would
    live in whichever of them was reopened last.
    """
    removed = ("_bot_replies", "_register_bot_reply", "_pop_bot_reply",
               "_apply_edited_message", "dispatch_edit")
    offenders = []
    for path in sorted(BOT.rglob("*.py")):
        if path.name.startswith("selftest"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = _identifiers(tree)
        for name in removed:
            if name in used:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        "the removed feature is used in production code again:\n"
        + "\n".join(offenders))


def test_the_explanation_of_the_removal_is_still_there():
    """The other side of the same coin.

    The comments that say *why* the feature is gone have to survive it:
    a removal without a reason is a removal somebody will undo, because
    the next reader cannot tell a decision from an accident.
    """
    src = (BOT / "state.py").read_text(encoding="utf-8")
    assert "_bot_replies" in src, (
        "state.py no longer explains what the map was and why it went. "
        "The names in a comment are not code -- the AST check above is "
        "what proves that.")


def test_the_subscription_does_not_ask_telegram_for_edits():
    """`allowed_updates` is where the bot promises what it will handle.

    A type in that list with no handler behind it is a promise the bot
    cannot keep -- and it is exactly how the edit-replace feature stayed
    dead in production for years while the source said it was alive.
    """
    import config

    assert "edited_message" not in config.ALLOWED_UPDATES, (
        "edited_message is subscribed again, but nothing handles an edit: "
        "the bot would receive updates it has no path for")
    assert "message" in config.ALLOWED_UPDATES
    assert "callback_query" in config.ALLOWED_UPDATES, (
        "the Stop button is a callback_query; without it the button "
        "appears in the UI and its clicks are lost")
    assert "chat_member" in config.ALLOWED_UPDATES, (
        "the new-member welcome flow is a chat_member update")
