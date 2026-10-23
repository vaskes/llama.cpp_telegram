"""Live-path regression for edit-replace. Wave 7.

The feature has died silently twice. First the whole block lived in the
poll loop while production ran webhook, so nothing ever called it. Then
the two transports declared different `allowed_updates` and one dropped
`edited_message`. Both times the suite stayed green, because the only
coverage was an AST check that the constant contained the string.

This is not the live test that feature needs -- that one requires two
people, a deliberately slow model and a human watching the message
change. What this does is exercise the whole path in-process: an
`edited_message` update goes in, the stale reply is deleted, and the
update is rewritten as a plain `message` so the dispatcher processes the
corrected text.

The gap it closes is the middle of the path. `_dispatch_update` calling
`_apply_edited_message` is the seam that broke twice, and it is
asserted here against the real `_bot_replies` registry rather than a
literal list in the test.
"""
import asyncio
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import dispatch  # noqa: E402
import dispatch_edit  # noqa: E402
import dispatch_polling  # noqa: E402
import state  # noqa: E402


class _FakeBot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, message_id, **kw):
        self.deleted.append((chat_id, message_id))


class _FakeApp:
    def __init__(self):
        self.bot = _FakeBot()


def _edited_update(text, chat_id=1000, message_id=7, bot_msg_id=555):
    """A Telegram `edited_message` update, plus the registry entry that
    says the bot already answered the original."""
    state._bot_replies[(chat_id, message_id)] = bot_msg_id
    return {
        "update_id": 4242,
        "edited_message": {
            "message_id": message_id,
            "date": 1,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "tester"},
            "text": text,
        },
    }


def test_a_corrected_message_deletes_the_stale_reply_and_is_reprocessed():
    """The whole middle of the feature, in one process."""
    app = _FakeApp()
    upd = _edited_update("helo ther")
    dispatch_polling.set_dispatcher(app)

    try:
        rewritten = asyncio.new_event_loop().run_until_complete(
            dispatch_edit._apply_edited_message(upd))

        assert rewritten is True, (
            "the correction was not taken as a fresh message. If this "
            "returns False the user sees a bot that answered text they "
            "have already deleted on screen, with no error anywhere.")
        assert app.bot.deleted == [(1000, 555)], (
            f"the stale reply was not deleted: {app.bot.deleted}. The bot "
            "still has its old answer sitting under the corrected one, "
            "which is the exact symptom edit-replace exists to remove.")

        assert upd.get("message", {}).get("text") == "helo ther", (
            "the update was not rewritten as a plain `message` carrying "
            "the corrected text, so the dispatcher never sees the edit")
        assert "edited_message" not in upd, (
            "the edited_message key was left in place. PTB 21 routes "
            "message and edited_message to matching handlers, so keeping "
            "both means the handler runs twice -- once on the text the "
            "user deleted, once on the correction.")

        # the registry entry is consumed exactly once
        assert (1000, 7) not in state._bot_replies, (
            "the bot-reply entry survived, so a SECOND edit of the same "
            "message would try to delete a reply that no longer exists")
    finally:
        dispatch_polling.set_dispatcher(None)
        state._bot_replies.clear()


def test_an_untouched_message_is_not_an_edit():
    """The negative half: a plain message must pass through untouched.

    Without this, a rewrite that fires on everything would look correct
    in the test above.
    """
    plain = {"update_id": 1, "message": {"message_id": 7, "text": "hi",
                                         "chat": {"id": 1000}}}
    try:
        result = asyncio.new_event_loop().run_until_complete(
            dispatch_edit._apply_edited_message(plain))
        assert result is False
        assert plain["message"]["text"] == "hi"
    finally:
        state._bot_replies.clear()


def test_the_dispatcher_calls_the_rewrite_before_it_routes():
    """The seam that broke twice.

    `_apply_edited_message` works perfectly in isolation and is called
    from nowhere: that was the first death, the block living in the poll
    loop while production ran webhook. This asserts the call site by
    source order rather than by running `main()` -- the wiring is the
    thing that has regressed, not the function.
    """
    src = (BOT / "dispatch.py").read_text(encoding="utf-8")
    import ast

    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "_dispatch_update")
    order = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            if name == "_apply_edited_message":
                order.append(("rewrite", node.lineno))
            elif name == "de_json":
                order.append(("route", node.lineno))
        if isinstance(node, ast.Attribute) and node.attr == "de_json":
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id == "Update":
                order.append(("route", node.lineno))
    assert order, "_dispatch_update no longer calls _apply_edited_message"
    assert order[0][0] == "rewrite", (
        f"_dispatch_update routes before rewriting: {order}. The rewrite "
        "has to happen first, or the dispatcher answers the original text "
        "the user already replaced.")
    assert len(order) >= 2, (
        f"only {order} found; the rewrite is in place but the dispatcher "
        "step is not visible from the same function")


def test_both_transports_would_deliver_an_edited_message():
    """Third death, cheapest to check: does the transport even ask for it?

    Once `edited_message` was missing from one of the two
    `allowed_updates`, and nothing noticed because a string was
    compared to a string.
    """
    import config

    assert "edited_message" in config.ALLOWED_UPDATES, (
        "the transports no longer subscribe to edited_message, so the "
        "rewrite below never runs no matter how correct the code is")

    from config import ALLOWED_UPDATES_JSON, ALLOWED_UPDATES_LIST
    import json

    assert "edited_message" in json.loads(ALLOWED_UPDATES_JSON)
    assert "edited_message" in ALLOWED_UPDATES_LIST