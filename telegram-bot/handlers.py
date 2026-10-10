# handlers.py
# The 4 Telegram message handlers (text/photo/voice/document)
# plus their helpers.
#
# Cross-module state (_is_group_chat, _should_mute_in_group,
# _user_semaphore, _check_user_slot, _stop_button_markup,
# _abort_events, _bot_replies, _persist_message, _load_history)
# is imported from bot.py INSIDE each function (late import)
# to avoid circular dependencies at module load time. This is
# the standard Python pattern for cross-module function refs
# in a project still in mid-refactor.

import asyncio
import base64
import json
import os
import tempfile

from telegram import Update
from telegram.ext import ContextTypes

from call_llama import call_llama, transcribe_voice, fetch_tools_from_llama, get_weather, _discover_default_model, _donsetch_init, donsetch_call, _tag_sender
import state  # cross-module state (see state.py)
from storage import get_store
from state import _abort_events, _bot_replies, _get_global_llm_sem
from rating import _is_rating_active, _apply_rating_and_persist
# Late imports for cross-module state (see state.py). The state
# lives in state.py to break the circular dep between handlers.py
# and dispatch.py (both need _abort_events, _bot_replies,
# _get_global_llm_sem). Late import: done at handler-call time so
# bot.py is fully loaded by the time we read these.
import persistence
import prompts
import rating
from config import (
    BOT_TOKEN, MAX_DOC_BYTES, MAX_PHOTO_BYTES, MAX_VOICE_BYTES,
    MODEL, TELEGRAM_API,
)
from prompts import GROUP_CONTEXT, RATING_RULES

# === send_reply ===
async def send_reply(update: Update, text: str):
    """Reply to a Telegram message, falling back to a helpful message if the model returned nothing.

    Telegram rejects empty messages with `Message text is empty`. Some models
    (Ornith-Uncensored in particular) spend the whole token budget on reasoning
    and return content='' for short prompts. We don't want the user to see a
    cryptic API error in that case.

    Also records the (chat_id, user_message_id) → bot_message_id mapping
    so a later user edit can trigger delete+reprocess.
    """
    from dispatch import _is_group_chat, _check_user_slot, _stop_button_markup, reject_if_unauthorized, is_authorized, _parse_subtalk_arg, _parse_topic_arg, _should_mute_in_group, _is_reply_to_other_user, _user_semaphore
    text = (text or '').strip()
    if not text:
        text = EMPTY_RESPONSE_FALLBACK
    sent = await _reply(update, text)
    # Record the bot reply so an edit on the user message can replace it.
    # update.message.message_id is the user's message; sent.message_id is
    # the bot's reply. The mapping lets us delete the bot reply and
    # process the edited user message as a fresh request.
    try:
        user_msg = update.message
        if user_msg is not None and sent is not None:
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id is not None:
                _bot_replies[(chat_id, user_msg.message_id)] = sent.message_id
    except Exception as e:
        # Never let tracking break a reply.
        print(f"[send_reply] tracking failed: {type(e).__name__}: {e!r}", flush=True)

# === _reply_active ===
async def _reply_active(update: Update, user_id: int) -> str:
    """Returns the user's active sub-talk name (resolving to 'main' default)."""
    return await asyncio.to_thread(get_store().get_active_thread, user_id) or 'main'

# === _general_thread_id ===
def _general_thread_id() -> str:
    """Sentinel thread id for the General topic of a forum-enabled
    supergroup. The General topic is real (it has messages, history,
    notifications) but the Bot API does not assign it a numeric
    message_thread_id; instead, messages in it have message_thread_id
    == None. We use a stable string sentinel so that conversation
    history persists per-(chat_id, "general") key.
    """
    return "general"

# === _route_to_thread ===
async def _route_to_thread(update, context) -> tuple[int, str, bool] | None:
    """Resolve the (chat_id, thread_id) for the current message.

    Returns a tuple (chat_id, thread_id, is_group) on success, or
    None if the user is not authorized (only in private mode).

    Private mode (update.message.message_thread_id is None):
      - Enforces ALLOWED_USER_IDS whitelist via reject_if_unauthorized.
      - chat_id = effective_user.id (== chat_id for 1:1 chats).
      - thread_id = active sub-talk name from DB, or 'main' default
        auto-created on first message.

    Group mode (update.message.message_thread_id is not None):
      - No whitelist check. Any member of the group can use the bot.
      - chat_id = effective_chat.id (the group id, negative).
      - thread_id = str(message_thread_id) -- the Telegram topic id.
    """
    from dispatch import _is_group_chat, _check_user_slot, _stop_button_markup, reject_if_unauthorized, is_authorized, _parse_subtalk_arg, _parse_topic_arg, _should_mute_in_group, _is_reply_to_other_user, _user_semaphore
    msg = update.message
    if _is_group_chat(update):
        # Group mode. Telegram is the source of truth for thread
        # existence; we do not need a DB row to know it exists.
        # For messages in the General topic, message_thread_id is None
        # by Bot API design — use the "general" sentinel so the DB key
        # is stable and conversation history persists.
        if msg.message_thread_id is not None:
            thread_id = str(msg.message_thread_id)
        else:
            thread_id = _general_thread_id()
        return update.effective_chat.id, thread_id, True
    # Private mode
    if await reject_if_unauthorized(update, context):
        return None
    user_id = update.effective_user.id
    active = await asyncio.to_thread(get_store().get_active_thread, user_id)
    if active is not None:
        return user_id, active, False
    # No sub-talks at all yet — auto-create 'main' so the user's first
    # message lands somewhere sensible without them needing to /newsub.
    await asyncio.to_thread(get_store().create_thread, user_id, 'main')
    await asyncio.to_thread(get_store().set_active_thread, user_id, 'main')
    return user_id, 'main', False

# === _reply ===
async def _reply(update, text, **kwargs):
    """Reply to update.message, preserving message_thread_id in group mode.

    PTB's Message.reply_text() does NOT pass message_thread_id
    through to sendMessage, so replies in group mode would land
    in the General topic instead of staying in the user's topic.
    This helper fixes that.
    """
    from dispatch import _is_group_chat, _check_user_slot, _stop_button_markup, reject_if_unauthorized, is_authorized, _parse_subtalk_arg, _parse_topic_arg, _should_mute_in_group, _is_reply_to_other_user, _user_semaphore
    if _is_group_chat(update):
        kwargs.setdefault('message_thread_id', update.message.message_thread_id)
    return await update.message.reply_text(text, **kwargs)

# === _reject_in_group ===
async def _reject_in_group(update) -> bool:
    """Sub-talk commands (/newsub, /sub, /subs, /delsub, /here, /reset, /stats)
    only make sense in private chat mode — they manage the user's own
    sub-talks in the bot's DB. In group mode, the equivalent is to use
    Telegram's native forum-topic controls (createForumTopic, etc.),
    which are wired in commit 3 of the group-mode migration.

    Returns True if the message is in a group and we sent a notice,
    False otherwise (i.e. the caller should continue processing).
    """
    from dispatch import _is_group_chat, _check_user_slot, _stop_button_markup, reject_if_unauthorized, is_authorized, _parse_subtalk_arg, _parse_topic_arg, _should_mute_in_group, _is_reply_to_other_user, _user_semaphore
    if _is_group_chat(update):
        await _reply(
            update,
            'ℹ In group mode, sub-talk commands are replaced by Telegram\n'
            'forum topics. Use the chat\'s "Create topic" button or\n'
            'the /forumtopic /topics /deltopic commands instead.\n'
            'See docs/SETUP.md for the group deployment pattern.'
        )
        return True
    return False

# === _resolve_active ===
async def _resolve_active(user_id: int) -> str:
    """Deprecated. Use _route_to_thread() in handlers.

    Kept for backward compatibility with any external code that
    imports it. New code should call _route_to_thread() instead.
    """
    active = await asyncio.to_thread(get_store().get_active_thread, user_id)
    if active is not None:
        return active
    # No sub-talks at all yet — auto-create 'main' so the user's first
    # message lands somewhere sensible without them needing to /newsub.
    await asyncio.to_thread(get_store().create_thread, user_id, 'main')
    await asyncio.to_thread(get_store().set_active_thread, user_id, 'main')
    return 'main'

# === _sender_display_name ===
def _sender_display_name(user) -> str | None:
    """Build a short display name for a Telegram User so the LLM can
    distinguish speakers in a group. Resolution order:
      1. first_name + " " + last_name  (e.g. "Vasisualy Lohankin")
      2. first_name only
      3. "@" + username
      4. "user_" + str(id)              (last-resort numeric)

    Returns None for non-User inputs (system messages, channel posts)
    so the caller can decide to skip persisting the name.
    """
    if user is None:
        return None
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    full = (first + " " + last).strip()
    if full:
        return full
    if getattr(user, "username", None):
        return "@" + user.username
    if getattr(user, "id", None) is not None:
        return f"user_{user.id}"
    return None

# === handle_photo ===
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from dispatch import _is_group_chat, _check_user_slot, _stop_button_markup, reject_if_unauthorized, is_authorized, _parse_subtalk_arg, _parse_topic_arg, _should_mute_in_group, _is_reply_to_other_user, _user_semaphore
    print(f"[handle_photo] ENTRY chat_id={update.effective_chat.id} thread_id={update.message.message_thread_id} caption={update.message.caption!r}", flush=True)
    result = await _route_to_thread(update, context)
    if result is None:
        print("[handle_photo] REJECTED", flush=True)
        return
    chat_id, thread_id, is_group = result
    print(f"[handle_photo] chat_id={chat_id} thread_id={thread_id!r} is_group={is_group}", flush=True)
    sem = await _check_user_slot(chat_id, update.effective_user.id, update)
    if sem is None:
        return
    await update.message.chat.send_action(action='typing')
    thinking = None
    try:
        photo = update.message.photo[-1]
        # Telegram sends multiple PhotoSize entries; we want the
        # largest (index -1). Cap it: a 50-MP photo at Q8_0 base64
        # inflates to >30 MB and would OOM the bot.
        # Note: photo.file_size is not always populated by Telegram.
        # The pre-check below fast-fails when file_size is set; the
        # helper _download_with_limit re-checks (and falls back to a
        # post-check on the actual byte count) when it isn't.
        if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
            await _reply(update,
                f'❌ Photo too large ({photo.file_size/1e6:.1f} MB > '
                f'{MAX_PHOTO_BYTES/1e6:.0f} MB).'
            )
            return
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await _download_with_limit(file, MAX_PHOTO_BYTES, 'Photo', update)
        if photo_bytes is None:
            return
        # Telegram photos are often WebP (especially from Android), not JPEG.
        # Detect actual MIME from magic bytes — we were hardcoding image/jpeg
        # which caused llama-server to reject the image with 400.
        mime = 'image/jpeg'
        if photo_bytes[:8].startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif photo_bytes[:4] == b'RIFF' and photo_bytes[8:12] == b'WEBP':
            mime = 'image/webp'
        elif photo_bytes[:2] == b'\xff\xd8':
            mime = 'image/jpeg'
        elif photo_bytes[:6] in (b'GIF87a', b'GIF89a'):
            mime = 'image/gif'
        photo_b64 = base64.b64encode(photo_bytes).decode('ascii')
        photo_data_url = f"data:{mime};base64,{photo_b64}"
        caption = update.message.caption or 'Describe the image in detail.'
        print(f"[handle_photo] downloaded {len(photo_bytes)} bytes, caption={caption!r}", flush=True)
        photo_content = [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": photo_data_url}}
        ]
        thread_id = thread_id  # already resolved by _route_to_thread
        sender_name = _sender_display_name(update.message.from_user)
        await _persist_message(chat_id, thread_id, 'user', photo_content, sender_name=sender_name)
        history = await _load_history(chat_id, thread_id)
        # When recalling image turns in later text-only messages, the
        # images themselves cannot be re-sent from history (no id
        # retained), so the model falls back to its own description.
        thinking = await _reply(update, '💭 думаю…', reply_markup=_stop_button_markup())
        print(f"[handle_photo] chat_id={chat_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
        # Per-task abort event for the Stop button.
        abort_event = asyncio.Event()
        _abort_events[(chat_id, thinking.message_id)] = abort_event
        # Compute rating_active here (before call_llama) so we
        # can pass it to the model AND use it in the dispatcher.
        rating_active = _is_rating_active(update)
        # T1 (P0-1): global LLM concurrency cap.
        async with _get_global_llm_sem():
            bot_response = await call_llama(history, max_tokens=16384, user_text=caption, thinking_msg=thinking, use_stream=False, rating_active=rating_active, abort_event=abort_event, bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id)
        print(f"[handle_photo] call_llama returned: {len(bot_response)} chars, head={bot_response[:200]!r}", flush=True)
        if bot_response is None:
            print(f"[handle_photo] ABORTED by user via Stop button, moving to next", flush=True)
            try:
                await thinking.edit_text('⏹ Остановлено')
            except Exception:
                pass
            return
        # === Empty-response retry ===
        # The LLM sometimes returns no content, no reasoning,
        # and no finish_reason — usually on hard questions
        # (future predictions, niche facts). The call_llama
        # fallback string starts with "[model returned an
        # empty response." We retry once. If still empty,
        # replace with a user-friendly message that suggests
        # rephrasing or trying again.
        if bot_response.startswith('[model returned an empty response.'):
            print(
                f"[handle_photo] empty response, retrying once",
                flush=True,
            )
            try:
                await thinking.edit_text('🔄 переспрашиваю…')
            except Exception:
                pass
            retry_response = await call_llama(
                history, max_tokens=16384, user_text=caption,
                thinking_msg=thinking, use_stream=False,
                rating_active=rating_active, abort_event=abort_event,
                bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id,
            )
            if retry_response is None:
                print(f"[handle_photo] ABORTED on retry, moving to next", flush=True)
                try:
                    await thinking.edit_text('⏹ Остановлено')
                except Exception:
                    pass
                return
            if not retry_response.startswith('[model returned an empty response.'):
                bot_response = retry_response
                print(f"[handle_photo] retry succeeded", flush=True)
            else:
                print(f"[handle_photo] retry also empty, sending fallback", flush=True)
                bot_response = (
                    '🤔 Не удалось получить ответ от модели. '
                    'Возможные причины: перегруженный длинный контекст, '
                    'сложный вопрос (предсказания, нишевые факты) или '
                    'временный сбой llama-server. Попробуй:\n'
                    '• /reset — очистить историю и начать с нуля\n'
                    '• Переформулировать вопрос\n'
                    '• Добавить больше деталей в вопрос'
                )
        # === Edit-during-LLM detection: removed (was v0.4) ===
        # Earlier versions compared the user's current message
        # text against a snapshot taken at handler entry, to
        # detect "user edited during LLM call". That required a
        # concurrent getUpdates consumer, which Telegram rejects
        # (HTTP 409) — the check was a no-op in practice. The
        # _user_msg_text dict and the helper function were
        # deleted in v0.5.1 (T4). The polling loop's
        # edit-replace block (in _dispatch_update) now deletes
        # the bot's stale reply and re-processes the edit as a
        # new user message — there's a brief wrong-answer
        # window. The Stop button (per-handler abort_event) is
        # the recommended escape for long-running LLM calls.
        # === RATING_MODE dispatch ===
        # In a group with RATING_MODE=1, the LLM prefixes its
        # response with [[TYPE:...]] [[RATE:N]]. We parse the
        # type and take one of three actions:
        #   question / request / confirmation → text reply
        #   info / statement with rating        → emoji reaction, persist rating
        #   bloat                               → emoji reaction, no text
        # Private mode and [llm]-marked messages always get a
        # plain text reply (rating path is group-only).
        # rating_active was computed above (before call_llama).
        # T3 (P1-1): the 30-line RATING_MODE dispatch is now a
        # single helper call. The helper handles all 4 paths
        # (rating_active off, info/statement with rating, bloat,
        # question/request/confirmation) and returns the text
        # to reply with (possibly empty for rating-only turns).
        bot_response = await _apply_rating_and_persist(
            context, update, chat_id, thread_id, bot_response, rating_active,
        )
        if bot_response:
            await send_reply(update, bot_response)
    except Exception as e:
        print(f"[ERR photo] {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            await _reply(update, f'❌ Error: {e}')
        except Exception:
            pass
    finally:
        if thinking is not None:
            try:
                await thinking.delete()
            except Exception:
                pass
        _abort_events.pop((chat_id, thinking.message_id), None)
        sem.release()

# === _download_with_limit ===
async def _download_with_limit(file, max_bytes: int, kind: str, update: Update):
    """Download a Telegram file with a size guard.

    Returns the downloaded bytes on success, or None if the file is
    too large or the download failed (the user-facing error message
    has already been sent to `update` in both cases). Used by
    handle_photo, handle_voice, and handle_document so the
    pre-check + error path live in one place.

    Note: PTB 21.0 removed File.download_as_chunks (the old
    chunked iterator we used to abort mid-download). We now use
    File.download_as_bytearray(), which buffers the whole file
    in memory. The pre-check on file.file_size rejects oversized
    files before the request goes out (Telegram usually populates
    file_size, but not always); the post-check on len(buf) catches
    the rare case where file_size is missing and the file is huge.
    Worst-case memory: max_bytes (10 MB photo, 20 MB voice, 5 MB doc,
    50 MB video_note) - well within the bot's memory budget.
    """
    if file.file_size and file.file_size > max_bytes:
        await _reply(update,
            f'❌ {kind} too large ({file.file_size/1e6:.1f} MB > '
            f'{max_bytes/1e6:.0f} MB).'
        )
        return None
    try:
        buf = await file.download_as_bytearray()
    except Exception as e:
        await _reply(update, f'❌ Failed to download {kind.lower()}: {e}')
        return None
    if len(buf) > max_bytes:
        await _reply(update,
            f'❌ {kind} too large ({len(buf)/1e6:.1f} MB > '
            f'{max_bytes/1e6:.0f} MB).'
        )
        return None
    return bytes(buf)

# === handle_voice ===
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from dispatch import _is_group_chat, _check_user_slot, _stop_button_markup, reject_if_unauthorized, is_authorized, _parse_subtalk_arg, _parse_topic_arg, _should_mute_in_group, _is_reply_to_other_user, _user_semaphore
    result = await _route_to_thread(update, context)
    if result is None:
        return
    chat_id, thread_id, is_group = result
    # The actual user id, not the chat id. In private mode these
    # are equal (Telegram uses the same value for both), but in
    # group mode chat_id is the group's id and user_id must be
    # the user's id. The previous `user_id = chat_id` alias
    # caused the abort_event key to mismatch what the callback
    # handler looks up: (group_id, group_id) vs (group_id, real
    # user_id) — the Stop button failed silently.
    user_id = update.effective_user.id
    sem = await _check_user_slot(chat_id, user_id, update)
    if sem is None:
        return
    await update.message.chat.send_action(action='typing')
    thinking = None
    try:
        msg = update.message
        # filters.AUDIO catches both voice messages and video notes.
        # Pick the right cap for the kind we got; tell the user which
        # one if we reject.
        if msg.voice:
            max_bytes = MAX_VOICE_BYTES
            media = msg.voice
            media_kind = "Voice"
        elif msg.video_note:
            max_bytes = MAX_VIDEO_NOTE_BYTES
            media = msg.video_note
            media_kind = "Video note"
        else:
            return  # should not happen — handler is only registered for these
        # Pre-check; media.file_size is usually populated. The real
        # guard is the chunk counter in the helper.
        if media.file_size and media.file_size > max_bytes:
            await _reply(update, 
                f'❌ {media_kind} too large ({media.file_size/1e6:.1f} MB > '
                f'{max_bytes/1e6:.0f} MB).'
            )
            return
        file = await context.bot.get_file(media.file_id)
        voice_bytes = await _download_with_limit(file, max_bytes, media_kind, update)
        if voice_bytes is None:
            return
        transcript = await transcribe_voice(voice_bytes)
        # No parse_mode — transcript is user-generated and may contain
        # '*', '_', '[', '`', which would break Markdown rendering.
        await _reply(update, f'🎤 Transcript:\n{transcript}')
        sender_name = _sender_display_name(update.message.from_user)
        await _persist_message(chat_id, thread_id, 'user', transcript, sender_name=sender_name)
        history = await _load_history(chat_id, thread_id)
        print(f"[handle_voice] user_id={user_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
        thinking = await _reply(update, '💭 думаю…')
        rating_active = _is_rating_active(update)
        # T1 (P0-1): global LLM concurrency cap.
        async with _get_global_llm_sem():
            bot_response = await call_llama(history, max_tokens=16384, user_text=transcript, thinking_msg=thinking, rating_active=rating_active, bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id)
        # === RATING_MODE dispatch (continuation) ===
        # rating_active was computed above (before call_llama).
        # T3 (P1-1): the 30-line RATING_MODE dispatch is now a
        # single helper call. The helper handles all 4 paths
        # (rating_active off, info/statement with rating, bloat,
        # question/request/confirmation) and returns the text
        # to reply with (possibly empty for rating-only turns).
        bot_response = await _apply_rating_and_persist(
            context, update, chat_id, thread_id, bot_response, rating_active,
        )
        if bot_response:
            # In rating mode the response is short (no chunking needed).
            # The original 4000-char chunking applied to voice transcripts
            # which are unlikely to exceed 4k after going through the LLM
            # in this mode.
            if len(bot_response) > 4000:
                for i in range(0, len(bot_response), 4000):
                    await _reply(update, bot_response[i:i+4000])
            else:
                await send_reply(update, bot_response)
    except Exception as e:
        print(f"[ERR voice] {type(e).__name__}: {e}", flush=True)
        await _reply(update, f'❌ Error: {e}')
    finally:
        if thinking is not None:
            try:
                await thinking.delete()
            except Exception:
                pass
        sem.release()

# === handle_document ===
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from dispatch import _is_group_chat, _check_user_slot, _stop_button_markup, reject_if_unauthorized, is_authorized, _parse_subtalk_arg, _parse_topic_arg, _should_mute_in_group, _is_reply_to_other_user, _user_semaphore
    result = await _route_to_thread(update, context)
    if result is None:
        return
    chat_id, thread_id, is_group = result
    # The actual user id, not the chat id. In private mode these
    # are equal (Telegram uses the same value for both), but in
    # group mode chat_id is the group's id and user_id must be
    # the user's id. The previous `user_id = chat_id` alias
    # caused the abort_event key to mismatch what the callback
    # handler looks up: (group_id, group_id) vs (group_id, real
    # user_id) — the Stop button failed silently.
    user_id = update.effective_user.id
    sem = await _check_user_slot(chat_id, user_id, update)
    if sem is None:
        return
    await update.message.chat.send_action(action='typing')
    doc = update.message.document
    # Reject oversized documents before downloading. We will only ever
    # read the first 16 KB of text from the file, so anything bigger is
    # almost certainly a mistake or an attempt to OOM the bot.
    if doc.file_size and doc.file_size > MAX_DOC_BYTES:
        await _reply(update, 
            f'❌ Document too large ({doc.file_size/1e6:.1f} MB > '
            f'{MAX_DOC_BYTES/1e6:.0f} MB).'
        )
        return
    file = await context.bot.get_file(doc.file_id)
    doc_bytes = await _download_with_limit(file, MAX_DOC_BYTES, 'Document', update)
    if doc_bytes is None:
        return
    tmp_path = None
    thinking = None
    try:
        ext = doc.file_name.split('.')[-1] if '.' in doc.file_name else 'txt'
        with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as tmp:
            tmp.write(doc_bytes)
            tmp_path = tmp.name
        with open(tmp_path, 'r', encoding='utf-8', errors='ignore') as f:
            doc_text = f.read()[:16000]
        caption = update.message.caption or 'Read the document and answer questions.'
        user_content = f"{caption}\n\n--- Document ---\n{doc_text}"
        sender_name = _sender_display_name(update.message.from_user)
        await _persist_message(chat_id, thread_id, 'user', user_content, sender_name=sender_name)
        history = await _load_history(chat_id, thread_id)
        print(f"[handle_document] user_id={user_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
        thinking = await _reply(update, '💭 думаю…', reply_markup=_stop_button_markup())
        rating_active = _is_rating_active(update)
        abort_event = asyncio.Event()
        _abort_events[(chat_id, thinking.message_id)] = abort_event
        # T1 (P0-1): global LLM concurrency cap.
        async with _get_global_llm_sem():
            bot_response = await call_llama(history, max_tokens=16384, user_text=caption, thinking_msg=thinking, rating_active=rating_active, abort_event=abort_event, bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id)
        if bot_response is None:
            print(f"[handle_document] ABORTED by user via Stop button, moving to next", flush=True)
            try:
                await thinking.edit_text('⏹ Остановлено')
            except Exception:
                pass
            return
        # === Empty-response retry (same as handle_photo) ===
        if bot_response.startswith('[model returned an empty response.'):
            print(f"[handle_document] empty response, retrying once", flush=True)
            try:
                await thinking.edit_text('🔄 переспрашиваю…')
            except Exception:
                pass
            retry_response = await call_llama(
                history, max_tokens=16384, user_text=caption,
                thinking_msg=thinking, rating_active=rating_active,
                abort_event=abort_event,
                bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id,
            )
            if retry_response is None:
                print(f"[handle_document] ABORTED on retry, moving to next", flush=True)
                try:
                    await thinking.edit_text('⏹ Остановлено')
                except Exception:
                    pass
                return
            if not retry_response.startswith('[model returned an empty response.'):
                bot_response = retry_response
                print(f"[handle_document] retry succeeded", flush=True)
            else:
                print(f"[handle_document] retry also empty, sending fallback", flush=True)
                bot_response = (
                    '🤔 Не удалось получить ответ от модели. '
                    'Возможные причины: перегруженный длинный контекст, '
                    'сложный вопрос (предсказания, нишевые факты) или '
                    'временный сбой llama-server. Попробуй:\n'
                    '• /reset — очистить историю и начать с нуля\n'
                    '• Переформулировать вопрос\n'
                    '• Добавить больше деталей в вопрос'
                )
        # Edit-during-LLM detection removed: the check is a
        # no-op without a concurrent getUpdates consumer.
        # See handle_photo for the full rationale.
        # === RATING_MODE dispatch ===
        # In a group with RATING_MODE=1, the LLM prefixes its
        # response with [[TYPE:...]] [[RATE:N]]. We parse the
        # type and take one of three actions:
        #   question / request / confirmation → text reply
        #   info / statement with rating        → emoji reaction, persist rating
        #   bloat                               → emoji reaction, no text
        # Private mode and [llm]-marked messages always get a
        # plain text reply (rating path is group-only).
        # rating_active was computed above (before call_llama).
        # T3 (P1-1): the 30-line RATING_MODE dispatch is now a
        # single helper call. The helper handles all 4 paths
        # (rating_active off, info/statement with rating, bloat,
        # question/request/confirmation) and returns the text
        # to reply with (possibly empty for rating-only turns).
        bot_response = await _apply_rating_and_persist(
            context, update, chat_id, thread_id, bot_response, rating_active,
        )
        if bot_response:
            await send_reply(update, bot_response)
    except Exception as e:
        print(f"[ERR doc] {type(e).__name__}: {e}", flush=True)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if thinking is not None:
            try:
                await thinking.delete()
            except Exception:
                pass
        _abort_events.pop((chat_id, thinking.message_id), None)
        sem.release()

# === handle_text ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from dispatch import _is_group_chat, _check_user_slot, _stop_button_markup, reject_if_unauthorized, is_authorized, _parse_subtalk_arg, _parse_topic_arg, _should_mute_in_group, _is_reply_to_other_user, _user_semaphore
    print(f"[handle_text] ENTRY user_id={update.effective_user.id} chat_type={update.effective_chat.type} is_forum={getattr(update.effective_chat, 'is_forum', None)} thread_id={update.message.message_thread_id} text={update.message.text!r}", flush=True)
    result = await _route_to_thread(update, context)
    if result is None:
        print(f"[handle_text] REJECTED {update.effective_user.id}", flush=True)
        return
    chat_id, thread_id, is_group = result
    # The actual user id, not the chat id. In private mode these
    # are equal (Telegram uses the same value for both), but in
    # group mode chat_id is the group's id and user_id must be
    # the user's id. The previous `user_id = chat_id` alias
    # caused the abort_event key to mismatch what the callback
    # handler looks up: (group_id, group_id) vs (group_id, real
    # user_id) — the Stop button failed silently.
    user_id = update.effective_user.id
    sem = await _check_user_slot(chat_id, user_id, update)
    if sem is None:
        return
    user_message = update.message.text
    try:
        await update.message.chat.send_action(action='typing')
    except Exception as e:
        print(f"[handle_text] send_action FAILED: {type(e).__name__}: {e!r}", flush=True)
    sender_name = _sender_display_name(update.message.from_user)
    await _persist_message(chat_id, thread_id, 'user', user_message, sender_name=sender_name)
    history = await _load_history(chat_id, thread_id)
    print(f"[handle_text] user_id={user_id} thread_id={thread_id!r} history_len={len(history)}", flush=True)
    thinking = await _reply(update, '💭 думаю…', reply_markup=_stop_button_markup())
    # Register per-task abort event so the Stop button on the
    # thinking message can interrupt the LLM call. Cleaned up
    # in the finally block to avoid leaking the event into the
    # next handler call (the semaphore allows the same user to
    # have at most 2 in-flight requests, so the dict shouldn't
    # grow without bound).
    abort_event = asyncio.Event()
    _abort_events[(chat_id, thinking.message_id)] = abort_event
    try:
        rating_active = _is_rating_active(update)
        # T1 (P0-1): wrap call_llama in the global semaphore so
        # 50 different users in a group can only cause 4
        # concurrent LLM forwards on the same GPU.
        async with _get_global_llm_sem():
            bot_response = await call_llama(history, max_tokens=32768, user_text=user_message, thinking_msg=thinking, rating_active=rating_active, abort_event=abort_event, bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id)
        if bot_response is None:
            print(f"[handle_text] ABORTED by user via Stop button, moving to next", flush=True)
            try:
                await thinking.edit_text('⏹ Остановлено')
            except Exception:
                pass
            return
        # === Empty-response retry (same as handle_photo) ===
        if bot_response.startswith('[model returned an empty response.'):
            print(f"[handle_text] empty response, retrying once", flush=True)
            try:
                await thinking.edit_text('🔄 переспрашиваю…')
            except Exception:
                pass
            retry_response = await call_llama(
                history, max_tokens=32768, user_text=user_message,
                thinking_msg=thinking, rating_active=rating_active,
                abort_event=abort_event,
                bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id,
            )
            if retry_response is None:
                print(f"[handle_text] ABORTED on retry, moving to next", flush=True)
                try:
                    await thinking.edit_text('⏹ Остановлено')
                except Exception:
                    pass
                return
            if not retry_response.startswith('[model returned an empty response.'):
                bot_response = retry_response
                print(f"[handle_text] retry succeeded", flush=True)
            else:
                print(f"[handle_text] retry also empty, sending fallback", flush=True)
                bot_response = (
                    '🤔 Не удалось получить ответ от модели. '
                    'Возможные причины: перегруженный длинный контекст, '
                    'сложный вопрос (предсказания, нишевые факты) или '
                    'временный сбой llama-server. Попробуй:\n'
                    '• /reset — очистить историю и начать с нуля\n'
                    '• Переформулировать вопрос\n'
                    '• Добавить больше деталей в вопрос'
                )
        # Edit-during-LLM detection removed: the check is a
        # no-op without a concurrent getUpdates consumer.
        # See handle_photo for the full rationale.
        # === RATING_MODE dispatch ===
        # T3 (P1-1): the 30-line RATING_MODE dispatch is now a
        # single helper call. The helper handles all 4 paths
        # (rating_active off, info/statement with rating, bloat,
        # question/request/confirmation) and returns the text
        # to reply with (possibly empty for rating-only turns).
        bot_response = await _apply_rating_and_persist(
            context, update, chat_id, thread_id, bot_response, rating_active,
        )
        if bot_response:
            if len(bot_response) > 4000:
                for i in range(0, len(bot_response), 4000):
                    await _reply(update, bot_response[i:i+4000])
            else:
                await send_reply(update, bot_response)
    except Exception as e:
        print(f"[ERR text] {type(e).__name__}: {e}", flush=True)
        await _reply(update, f'❌ Error: {e}')
    finally:
        try:
            await thinking.delete()
        except Exception:
            pass
        # Deregister the per-task abort event so a subsequent
        # handler call for the same user doesn't accidentally
        # see a leftover event. We pop the specific key rather
        # than clear, in case the dict has been overwritten by
        # a concurrent handler (defensive — the semaphore
        # prevents same-user concurrency, but be safe).
        _abort_events.pop((chat_id, thinking.message_id), None)
        sem.release()



# === Re-exported helpers for tests ===
async def _persist_message(*args, **kwargs):
    return await persistence.persist(*args, **kwargs)


async def _load_history(*args, **kwargs):
    return await persistence.load_history(*args, **kwargs)
