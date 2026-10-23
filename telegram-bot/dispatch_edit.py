"""The edited-message path: rewrite the bot's answer in place.

TECH-DEBT #10 block F. `dispatch.py` was 476 lines against a ticket that
asked for 400, and this was the last thing in it that was not routing.

Giving edit-replace its own module is not only about the line count. This
is the one code path in the bot with **no live coverage and no way to get
it**: verifying it needs two people, a deliberately slow model, and an
open thread -- a stop button, a message sent while the model is still
thinking, and a human watching the message change underneath them. The
docs record that as manual-only rather than pretending a unit test proves
it.

When that live test happens, the search starts in a ~110-line file whose
entire subject is this. And if the feature is ever removed, the diff is
one file instead of a hunk in the orchestration layer.
"""

import dispatch_polling
from logconf import get_logger
from state import _bot_replies

log = get_logger(__name__)


async def _apply_edited_message(upd_dict):
    """Turn an `edited_message` update into a plain `message`, in place.

    Why this exists at all: the bot answers a message, the user fixes a
    typo, and Telegram redelivers the correction as a separate
    `edited_message` update. Left alone, the bot keeps showing the answer
    to the text the user has already replaced on screen. So a non-command
    edit deletes the bot's stale reply (via the `_bot_replies` map) and is
    reprocessed as a fresh message; a command edit is idempotent and is
    simply re-run.

    Why it lives here rather than in the polling loop: it did live there,
    and the production deployment runs in WEBHOOK mode -- `webhook.py`
    hands updates straight to `_dispatch_update` and never enters the
    poll loop. So the whole feature was dead in production, and worse than
    dead: PTB 21 routes an edited text message into
    `MessageHandler(filters.TEXT & ~filters.COMMAND)` while
    `update.message` on that update is None, so `handle_text` raised

    AttributeError("'NoneType' object has no attribute 'message_thread_id'")

    on every edit in a forum. Four times in twenty minutes on
    2026-10-22. Measured in the shipping image:
    `MessageHandler(filters.TEXT & ~filters.COMMAND).check_update(
    Update(edited_message=Message(text="hi")))` -> True.

    `handlers.py` and `state.py` both carried comments saying the
    edit-replace block was "in _dispatch_update". It was not; they were
    describing where it belonged.

    Returns True if the update was rewritten. A no-op on every other
    update, including callback_query, which also carries no `.message`.
    """
    if "edited_message" not in upd_dict or "message" in upd_dict:
        return False
    em = upd_dict["edited_message"]
    if not isinstance(em, dict):
        return False
    em_text = (em.get("text") or "").lstrip()
    if em_text.startswith("/"):
        # Normalise: rewrite the update so the dispatcher's handlers see
        # a regular `message` field. This way every handler continues to
        # read `update.message.text` and friends without per-handler
        # branching.
        upd_dict["message"] = em
        upd_dict.pop("edited_message", None)
        log.info(f"[edit] treating edited_message as new command "
                 f"update_id={upd_dict.get('update_id')} cmd={em_text[:32]!r}")
        return True
    # Non-command edit: delete the bot's previous reply (if any) and
    # reprocess.
    em_chat_id = (em.get("chat") or {}).get("id")
    em_msg_id = em.get("message_id")
    if em_chat_id is not None and em_msg_id is not None:
        key = (em_chat_id, em_msg_id)
        old_bot_msg_id = _bot_replies.pop(key, None)
        _dispatcher = dispatch_polling.get_dispatcher()
        if old_bot_msg_id is not None and _dispatcher is not None:
            try:
                await _dispatcher.bot.delete_message(
                    chat_id=em_chat_id,
                    message_id=old_bot_msg_id,
                )
                log.info(f"[edit] edit-replace: deleted old bot reply "
                         f"chat_id={em_chat_id} bot_msg_id={old_bot_msg_id} "
                         f"user_msg_id={em_msg_id}")
            except Exception as e:
                # Most common failure: the bot lacks delete permission, or
                # the message is too old. Just log and continue.
                log.info(f"[edit] edit-replace: delete failed "
                         f"chat_id={em_chat_id} bot_msg_id={old_bot_msg_id}: "
                         f"{type(e).__name__}: {e!r}")
    # Now rewrite the update as a regular `message` so the dispatcher
    # processes it like a new user message. The downstream handlers
    # (handle_text etc.) will see the edited text.
    upd_dict["message"] = em
    # Drop the original key. Leaving it in place makes the update carry
    # BOTH `message` and `edited_message`, and PTB 21 hands each of those
    # to a matching MessageHandler -- so the edit is processed twice.
    upd_dict.pop("edited_message", None)
    log.info(f"[edit] edit-replace: processing edited_message "
             f"update_id={upd_dict.get('update_id')} text={em_text[:32]!r}")
    return True
