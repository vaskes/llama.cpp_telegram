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

import pytest
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

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
    # Presence of each call, then order. Checking only `order[0]` cannot
    # tell "rewrite happens late" from "rewrite does not happen at all",
    # and it reported the second as the first: deleting the rewrite call
    # produced "routes before rewriting: [('route', 300)]", which sends
    # the next reader looking for an ordering bug that is not there.
    rewrites = [ln for kind, ln in order if kind == "rewrite"]
    routes = [ln for kind, ln in order if kind == "route"]
    assert rewrites, (
        "_dispatch_update does not call _apply_edited_message at all. The "
        f"calls it does have are {order}. This is the first death of this "
        "feature: the function is perfect and called from nowhere.")
    assert routes, (
        f"the rewrite is in place ({rewrites}) but no dispatcher step is "
        "visible in the same function; this assertion no longer knows "
        "what it is comparing against")
    assert min(rewrites) < min(routes), (
        f"_dispatch_update routes before rewriting: {order}. The rewrite "
        "has to happen first, or the dispatcher answers the original text "
        "the user already replaced.")


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

def test_the_server_refuses_to_start_without_a_secret(monkeypatch):
    """N7, with a check.

    `_check_secret` is fail-closed, so a missing
    `WEBHOOK_SECRET_TOKEN` never opens the endpoint -- it makes the bot
    reject 100% of traffic while `/healthz` keeps answering
    `webhook-ok`. A health check that cannot fail on the one
    misconfiguration that stops the bot working is worse than none: it
    tells the operator green while every message goes nowhere.

    `run()` now raises before binding the socket. Note the distinction
    from the BOT_TOKEN check, which fires at import of `config` and is
    a different failure entirely -- seeing that message does not mean
    this one was tested.
    """
    import config
    import webhook

    monkeypatch.setattr(config, "WEBHOOK_SECRET_TOKEN", "")

    async def go():
        await webhook.run()

    loop = asyncio.new_event_loop()
    try:
        # The timeout matters. With the startup check removed, `run()`
        # binds the socket and waits on the shutdown event forever --
        # the mutation HANGS the suite instead of failing it, and a hang
        # reads as infrastructure trouble rather than as a caught
        # regression.
        with pytest.raises(RuntimeError) as exc:
            loop.run_until_complete(asyncio.wait_for(go(), 5))
    except asyncio.TimeoutError:
        pytest.fail(
            "webhook.run() did not refuse to start: it bound the socket "
            "and waited. The container now serves a healthy /healthz while "
            "rejecting every update.")
    finally:
        loop.close()

    assert "WEBHOOK_SECRET_TOKEN" in str(exc.value), (
        f"the failure does not name the variable that is missing: {exc}")


def test_the_health_endpoint_answers_even_while_traffic_is_refused(monkeypatch):
    """Why N7 needed a check: this is the state it used to hide."""
    import config
    import webhook

    class _Request:
        headers = {}

    assert webhook._check_secret(_Request()) is False, (
        "precondition: an unset secret is fail-closed at the door")

    monkeypatch.setattr(config, "WEBHOOK_SECRET_TOKEN", "")
    try:
        response = asyncio.new_event_loop().run_until_complete(
            webhook._healthz(_Request()))
        body = response.text if hasattr(response, "text") else b""
        assert b"ok" in (body if isinstance(body, bytes) else body.encode()), (
            "healthz no longer answers ok -- if this changed, N7's "
            "motivation changed too and the startup check should be "
            "re-examined rather than kept")
    finally:
        config.WEBHOOK_SECRET_TOKEN = ""


def test_the_health_endpoint_is_actually_registered():
    """M13. `_healthz` answering is not the endpoint existing.

    The test above calls `webhook._healthz` directly, which means it
    proves the handler works and says nothing about whether anything
    routes to it. Deleting `app.router.add_get("/healthz", _healthz)`
    from `make_app()` leaves the whole suite green -- verified by
    mutation, 25 passed.

    That matters because of what N7 is for. The whole reason the server
    now refuses to start without a secret is that a health check which
    cannot fail on the one misconfiguration that stops the bot working
    is worse than none: green, while every message goes nowhere. Keep
    the handler AND the registration, or the reasoning behind the
    startup check stops applying to the thing an operator watches.
    """
    import webhook

    app = webhook.make_app()
    paths = {getattr(r.resource, "canonical", None)
             for r in app.router.routes()}
    assert "/healthz" in paths, (
        f"/healthz is not registered; make_app() serves {sorted(p for p in paths if p)}. "
        "webhook.run() refuses to start without a secret on the grounds "
        "that a health endpoint which cannot fail is worse than none -- "
        "that reasoning no longer describes the deployed service.")
