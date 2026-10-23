"""An edit of a text message reached handle_text with no `.message` to read.

Production, 2026-10-22, four times inside twenty minutes:

    [dispatch] update_id=348110836 msg=None
    [handler error] AttributeError: AttributeError("'NoneType' object has
        no attribute 'message_thread_id'")

The bot subscribes to `edited_message` (it has since v0.9.3, f6b4a10) and
has a real feature behind it: a non-command edit deletes the bot's stale
reply and is reprocessed as a fresh message, so a corrected question gets
a corrected answer.

That feature lived INSIDE the polling loop. Production runs in webhook
mode, where `webhook.py` hands updates straight to `_dispatch_update` and
never enters the poll loop -- so the block never ran. PTB 21 turned that
into a crash rather than a no-op: measured in the shipping image,

    MessageHandler(filters.TEXT & ~filters.COMMAND).check_update(
        Update(edited_message=Message(text="hi")))   ->  True

while `update.message` on that same update is None, and handle_text's
ENTRY log reads `update.message.message_thread_id`.

The block now lives in `_dispatch_update`, the one path both delivery
modes share. Two comments in `handlers.py` and `state.py` had been
describing it as being there all along.

What the tests below pin, and why each one:

  1. the PREMISE -- PTB routes an edit into a text MessageHandler. If
     that changes, the rewrite becomes unnecessary rather than harmful,
     and a test that only watched the symptom would go quiet.
  2. the REWRITE -- an edit arrives at process_update AS a message.
  3. the STALE REPLY -- the old answer is deleted. A rewrite that
     forgets this is the half-fix: the user sees the old answer edited
     over by a new one, with no gap.
  4. the SUBSCRIPTION -- `edited_message` is still requested. The first
     draft of this fix removed it, on the reading that the bot had no
     edit handler. It has one; that was the bug.
"""
import datetime

import pytest

ROOT_IMPORT = "telegram-bot"


def _msg(text="hi", msg_id=7, chat_id=-100123, chat_type="supergroup"):
    from telegram import Chat, Message, User
    return {
        "message_id": msg_id,
        "date": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 42, "first_name": "u", "is_bot": False},
        "text": text,
    }


# --- 1. the premise --------------------------------------------------------

def test_ptb_routes_an_edited_text_message_into_a_text_handler():
    from telegram import Chat, Message, Update, User
    from telegram.ext import MessageHandler, filters

    upd = Update(
        update_id=1,
        edited_message=Message(
            message_id=7,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=-100123, type="supergroup"),
            from_user=User(id=42, first_name="u", is_bot=False),
            text="fixed typo"),
    )
    assert upd.message is None
    handler = MessageHandler(filters.TEXT & ~filters.COMMAND, lambda *_a: None)
    assert handler.check_update(upd) is True, (
        "PTB no longer routes an edited text message into a text handler; "
        "the rewrite in _apply_edited_message may now be dead code"
    )


# --- 2. the rewrite -------------------------------------------------------

class _FakeBot:
    def __init__(self):
        self.id = 42
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _FakeDispatcher:
    def __init__(self):
        self.processed = []
        self.bot = _FakeBot()

    async def process_update(self, upd):
        self.processed.append(upd)


class _patched_dispatch:
    """dispatch._dispatcher swapped for a fake, with an empty _bot_replies."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.dispatcher = _FakeDispatcher()

    def __enter__(self):
        import dispatch
        import dispatch_polling
        from state import _bot_replies
        # Since TECH-DEBT #10 block D the Application registry lives in
        # `dispatch_polling`, and `_dispatch_update` reads it through
        # `get_dispatcher()`. Assigning `dispatch._dispatcher` set a name
        # nothing read -- the fixture restored it faithfully and the tests
        # below went on exercising the real dispatcher.
        self._dispatch, self._bot_replies = dispatch, _bot_replies
        self._polling = dispatch_polling
        self._saved_dispatcher = dispatch_polling._dispatcher
        self._saved_replies = dict(_bot_replies)
        dispatch_polling._dispatcher = self.dispatcher
        _bot_replies.clear()
        _bot_replies.update(self.replies)
        return self

    def __exit__(self, *exc):
        self._polling._dispatcher = self._saved_dispatcher
        self._bot_replies.clear()
        self._bot_replies.update(self._saved_replies)
        return False


@pytest.mark.asyncio
async def test_an_edit_is_reprocessed_as_a_message():
    import dispatch

    upd_dict = {"update_id": 11, "edited_message": _msg("fixed typo")}
    with _patched_dispatch() as p:
        await dispatch._dispatch_update(upd_dict)

    assert len(p.dispatcher.processed) == 1, (
        "the edit never reached process_update"
    )
    seen = p.dispatcher.processed[0]
    assert seen.message is not None, (
        "process_update received an update with no .message -- that is the "
        "AttributeError('NoneType' object has no attribute "
        "'message_thread_id') from production, verbatim"
    )
    assert seen.message.text == "fixed typo"
    assert seen.edited_message is None


@pytest.mark.asyncio
async def test_a_plain_message_still_arrives_unchanged():
    """The control. Without it, 'the edit is delivered' would also be
    satisfied by a _dispatch_update that delivers everything the same."""
    import dispatch

    with _patched_dispatch() as p:
        await dispatch._dispatch_update({"update_id": 12, "message": _msg("hi")})
    assert len(p.dispatcher.processed) == 1
    assert p.dispatcher.processed[0].message.text == "hi"


@pytest.mark.asyncio
async def test_a_command_edit_is_reprocessed_without_deleting_anything():
    """Commands are idempotent; there is no stale prose answer to remove."""
    import dispatch

    with _patched_dispatch(replies={(-100123, 7): 555}) as p:
        await dispatch._dispatch_update(
            {"update_id": 13, "edited_message": _msg("/stats")})
    assert p.dispatcher.bot.deleted == []
    assert p.dispatcher.processed[0].message.text == "/stats"


# --- 3. the stale reply ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_non_command_edit_deletes_the_previous_reply():
    import dispatch

    with _patched_dispatch(replies={(-100123, 7): 555}) as p:
        await dispatch._dispatch_update(
            {"update_id": 14, "edited_message": _msg("corrected question")})
    assert p.dispatcher.bot.deleted == [(-100123, 555)], (
        "the bot's answer to the pre-correction text is still on screen; "
        "the rewrite half-works without this"
    )


@pytest.mark.asyncio
async def test_a_delete_failure_does_not_stop_the_rewrite():
    """Missing delete permission is the normal case in an old forum.
    Losing the rewrite would mean the edit is silently dropped."""
    import dispatch

    with _patched_dispatch(replies={(-100123, 7): 555}) as p:
        async def _boom(chat_id, message_id):
            raise RuntimeError("not enough rights")
        p.dispatcher.bot.delete_message = _boom
        await dispatch._dispatch_update(
            {"update_id": 15, "edited_message": _msg("corrected question")})
    assert len(p.dispatcher.processed) == 1
    assert p.dispatcher.processed[0].message.text == "corrected question"


# --- 4. the subscription ---------------------------------------------------

def test_edited_message_is_still_subscribed():
    """The first draft of the fix deleted this. It has to stay.

    The bot has an edit handler; that is what this whole test file is
    about. Unsubscribing turns a crash back into a silent no-op.
    """
    import ast
    from pathlib import Path
    # `allowed_updates` moved with the getUpdates loop in TECH-DEBT #10
    # block D. This test guards the subscription, so it reads wherever that
    # subscription is sent from -- pointing it back at dispatch.py would have
    # made it report "no allowed_updates list found", which is a test that
    # guards nothing, said by the test itself.
    src = (Path(__file__).resolve().parent.parent / ROOT_IMPORT
           / "dispatch_polling.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [e.value for e in node.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if "message" in values and any(
                    v in values for v in ("callback_query", "chat_member")):
                hits.append((node.lineno, values))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("[") and '"message"' in node.value:
                hits.append((node.lineno, node.value))
    assert hits, "no allowed_updates list found -- this test guards nothing"
    for lineno, value in hits:
        assert "edited_message" in value, (
            f"dispatch_polling.py:{lineno} stopped subscribing to "
            f"edited_message; "
            f"the edit-replace feature is now dead, silently"
        )