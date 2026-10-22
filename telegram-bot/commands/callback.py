"""Callback-query routing: the Stop button, rating buttons, sub-talk switches.

TECH-DEBT #10: command handlers moved out of dispatch.py.
Everything that decides what the bot *says* in response to a command
now lives here; dispatch.py keeps the routing that finds it.

Nothing in this package imports `dispatch`. The command handlers
reached nothing from it -- `_dispatcher` was only used by
`_register_bot_menu`, which now takes a bot it is handed.
"""

import re

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from access import reject_if_unauthorized
import storage
from state import _abort_events
from chat_utils import _is_group_chat
from logconf import get_logger

log = get_logger(__name__)



# === cmd_callback ===
async def cmd_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 2026-10-22: slash commands are for trusted ids only -- in a group
    # as well as in private. The group branch below returned before the
    # authorization check, so anyone who could write could clear history,
    # create topics, or delete them outright (cmd_delsub calls
    # deleteForumTopic, which is not a local record).
    if await reject_if_unauthorized(update, context):
        return
    """Handle inline-keyboard button presses.

    callback_data format:
      sub:<name>     — switch to sub-talk <name>  (private mode)
      newsub:prompt  — show a one-time reply keyboard asking for the new name
      delsub:<name>  — confirm-then-delete (we delete immediately; no confirm step)
      stop:thinking  — abort the in-flight LLM call for the user

    The Stop button is on every "💭 думаю…" message in both
    private and group mode. The original user (the one whose
    message is being processed) is the only one allowed to
    stop it — identified via the thinking message's
    reply_to_message.from_user.id. We compare that against
    the callback's from_user.id.
    """
    # answer() acknowledges the callback (removes the loading
    # spinner in the Telegram client). It's best-effort: if
    # the callback is too old (Telegram's timeout is short,
    # typically 30s) the call raises BadRequest. The action
    # itself proceeds regardless. Wrapped in try/except so
    # the rest of the handler runs even if answer() fails.
    try:
        await update.callback_query.answer()
    except Exception as e:
        log.info(f"[cmd_callback] answer() failed (non-fatal): {type(e).__name__}: {e!r}")
    data = update.callback_query.data or ""
    # === Stop button (works in private AND group) ===
    if data == "stop:thinking":
        # Identify the original user from the thinking message.
        # The thinking message was sent as a reply to the user's
        # original message, so reply_to_message is set.
        thinking_msg = update.callback_query.message
        original = (
            thinking_msg.reply_to_message
            if thinking_msg and thinking_msg.reply_to_message
            else None
        )
        if original is None or original.from_user is None:
            log.info("[callback stop] no original message in reply_to_message")
            return
        original_user_id = original.from_user.id
        clicker_id = update.effective_user.id
        if clicker_id != original_user_id:
            # Don't reveal that the request was meaningful; just
            # answer with a notice and let the clicker move on.
            log.info(f"[callback stop] rejected: clicker={clicker_id} != "
                f"original={original_user_id}")
            await update.callback_query.answer(
                "Только автор сообщения может остановить обработку.",
                show_alert=True,
            )
            return
        chat_id = update.effective_chat.id
        # Key on the thinking message id, NOT (chat_id, user_id).
        # Two concurrent in-flight handlers for the same (chat, user)
        # would otherwise overwrite each other in the dict: the LATER
        # handler wins the dict slot, so the Stop click on the EARLIER
        # message sets the wrong event, leaving the earlier LLM call to
        # run to completion. The user then sees a "stopped" thinking
        # message followed by a full response from the still-running LLM.
        # Using the thinking message id makes each handler own its own
        # slot, and the Stop click on a specific thinking message always
        # targets the matching handler.
        key = (chat_id, thinking_msg.message_id)
        event = _abort_events.get(key)
        if event is None:
            # No in-flight request for this user. Either the
            # handler finished between when the user clicked
            # Stop and when the callback arrived, or this is a
            # stale button (rare — buttons are tied to a
            # specific thinking message which gets deleted on
            # completion).
            log.info(f"[callback stop] no abort_event for {key} "
                f"(handler may have already finished)")
            # v0.7.9: retry on RetryAfter for edit_message_text
            from telegram.error import RetryAfter as _RA
            _edited_ok = False
            for _att in range(3):
                try:
                    await update.callback_query.edit_message_text("⏹ Уже завершено")
                    _edited_ok = True
                    break
                except _RA as _e:
                    await asyncio.sleep(float(getattr(_e, 'retry_after', 1)) + 1.0)
                except Exception:
                    break
            if not _edited_ok:
                log.info(f"[callback stop] edit_message_text failed for key={key}; "
                    f"thinking message likely already deleted")
            return
        event.set()
        log.info(f"[callback stop] abort_event set for {key}")
        # Delete the thinking message immediately so the user sees
        # the click took effect. We DELETE (not edit) so the inline
        # Stop button disappears with the message - editing would
        # keep the button on the 'Остановлено' text, which looks
        # 'sticky' in the chat scroll. The handler's abort path
        # also tries to delete, but it's a no-op if the message is
        # already gone.
        try:
            await update.callback_query.delete_message()
        except Exception as e:
            # Fallback: if delete fails (message too old, already
            # deleted by handler, etc.), try to edit with the
            # inline keyboard removed. answer() with show_alert=False
            # to not spam the user with an error toast.
            log.info(f"[callback stop] delete_message failed: {type(e).__name__}: {e!r}; falling back to edit")
            try:
                await update.callback_query.edit_message_text(
                    "⏹ Остановлено", reply_markup=None
                )
            except Exception as e2:
                log.info(f"[callback stop] edit fallback also failed: {type(e2).__name__}: {e2!r}")
        return

    # The remaining branches (sub:, newsub:prompt, delsub:) are
    # only emitted by the private-mode /subs inline keyboard.
    # Group mode: no inline buttons exist there.
    if _is_group_chat(update):
        return
    if await reject_if_unauthorized(update, context):
        return
    user_id = update.effective_user.id
    if data.startswith("sub:"):
        thread_id = data[4:]
        existing = await asyncio.to_thread(storage.get_store().get_thread, user_id, thread_id)
        if existing is None:
            await asyncio.to_thread(storage.get_store().create_thread, user_id, thread_id)
        await asyncio.to_thread(storage.get_store().set_active_thread, user_id, thread_id)
        await update.callback_query.edit_message_text(
            f'✅ Switched to sub-talk "{thread_id}".'
        )
    elif data == "newsub:prompt":
        # The button used to fire a ForceReply that asked the user
        # to "send the new sub-talk name" — but the next text
        # message the user typed would be routed through
        # handle_text as a regular question, not a name. The
        # button was confusing (review P2-4). Drop it; the user
        # can just type /newsub <name> directly.
        await update.effective_message.reply_text(
            'Use /newsub <name> to create a new sub-talk.'
        )
    elif data.startswith("delsub:"):
        thread_id = data[7:]
        n = await asyncio.to_thread(storage.get_store().delete_thread, user_id, thread_id)
        await update.callback_query.edit_message_text(
            f'🗑 Deleted "{thread_id}" and {n} message(s).'
        )
    else:
        await update.callback_query.edit_message_text(
            f'(unknown action: {data!r})'
        )
