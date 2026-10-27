# handlers.py
# The 4 Telegram message handlers (text/photo/voice/document)
# plus their helpers.
#
# Cross-module state comes from module-level imports, not late ones:
# `_is_group_chat` from chat_utils, `_should_mute_in_group` and
# `_check_user_slot` from access, `_stop_button_markup` from llm_markup,
# `_abort_events` from state. There is no `from bot import` here, at
# module level or inside a function -- the header used to say there was,
# and to name `_user_semaphore` and `_persist_message` as bot.py
# symbols, none of which exist in that shape: the semaphore accessor is
# `state._get_user_semaphore`, and the persist/load helpers here are
# thin re-export wrappers kept for tests.
#
# The trap this file fell into before: a comment that described a
# dependency structure the code did not have, which then read as
# justification for introducing it. The late-import tripwire below is
# scoped to `dispatch` only -- that was all wave 4 needed -- so a header
# claim about anything else is not checked by anything. `test_late_import_policy.py`
# is the one to extend when the policy grows; do not widen the header
# into a claim the tripwire cannot back.

import asyncio
import base64
import json
import os
import re
import tempfile

# TECH-DEBT #13: this block was hand-copied into all four handlers --
# four times for the primary result and three for the empty-response
# retry. llm_result owns it now. `_format_executed_tools` keeps its
# historical name because tests/test_t44.py and the call sites use it.
#
# 2026-10-24: the answer carries files as well as text. `unwrap_answer`
# is the one form; it also pulls the code blocks the model wrote out of
# the prose, so that they can arrive as documents. `llm_files` sends
# them.
from llm_result import _format_executed_tools, unwrap_answer
from user_notice import DOWNLOAD_FAILURE_MESSAGE, failure_message
from llm_files import send_files
from llm_abort import finalize_aborted_turn
# The answers the bot sends are remembered, so the delete_messages tool
# can take one back: the Bot API cannot list a chat's messages.
from llm_delete import new_answer_group, remember_answer

# TECH-DEBT #10 block C: the sender-label injection filter lives
# in `prompt_guard.py`. See the module docstring for why it is
# named for what it does rather than for a cosmetic role.
from prompt_guard import _SAFE_SENDER_MAX_LEN, _safe_sender_label

# One person, one name in the record. The policy of WHICH name a person
# is called lives in `sender_name.py`: two writers used to call the same
# person by two different names, and the record is the source of truth.
# See that module's docstring. The name here is kept because the
# handlers, the selftests and the tripwires all address it.
from sender_name import display_name as _sender_display_name

from logconf import get_logger
log = get_logger(__name__)




from telegram import Update
import time
from telegram.ext import ContextTypes

from call_llama import call_llama, transcribe_voice, _tag_sender
import state
import limits  # cross-module state (see state.py)
# TECH-DEBT #4: the persist/rollback block was hand-copied into all four
# handlers, so a fix to three of them left the fourth able to orphan a
# user message. turn.py owns the flag and the undo now.
from turn import Turn

# TECH-DEBT #2: all ten of these were imported *inside functions* to dodge an
# import cycle. The cycle was real but avoidable: none of them touch the
# dispatcher, so they live in leaf modules (chat_utils / access) and the
# import is unconditional again.
from chat_utils import (
    _is_group_chat, _parse_subtalk_arg, _parse_topic_arg, _rate_limit_send,
    TELEGRAM_TEXT_CHUNK,
)
from telegram_format import render_html, split_html
from access import (
    _check_user_slot, _is_reply_to_other_user, _should_mute_in_group,
    is_authorized, real_reply_to, reject_if_unauthorized,
)
from llm_markup import _stop_button_markup
import storage
from state import _abort_events, _get_global_llm_sem, _register_abort_event
from rating import _is_rating_active, _apply_rating_and_persist
# `handlers` does not import `dispatch`, at module level or inside a
# function. Shared state lives in `state.py` precisely so that this stays
# true, and `test_handlers_has_no_late_imports_of_dispatch` holds it there.
#
# The previous version of this comment described per-function late imports
# "see below". There were none -- the only occurrence of the phrase it
# quoted was the comment itself, which is why reading it kept being enough
# to make it look true. Do not add a route back to `dispatch` without a
# reason that survives the tripwire.
import persistence
import prompts
import rating
from typing import NamedTuple

from config import (
    BOT_TOKEN, EMPTY_RESPONSE_FALLBACK, EMPTY_RESPONSE_PREFIX,
    EMPTY_RESPONSE_USER_FALLBACK,
    MAX_DOC_BYTES, MAX_PHOTO_BYTES,
    # MAX_VIDEO_NOTE_BYTES is used by handle_voice, and that is not a typo:
    # handlers.AUDIO catches both voice messages and round video notes, so one
    # handler branches on update.message.voice vs .video_note and picks the
    # cap for whichever it got. The constant names the Telegram format, not
    # the handler, which is why it reads oddly here.
    #
    # It was also simply missing from this import while MAX_VOICE_BYTES was
    # present, so a voice note size-checked fine and a video note raised
    # NameError on the branch below -- one name short in one function.
    #
    # Not renamed: this is a documented env var (config.py, .env.example), and
    # forks that set MAX_VIDEO_NOTE_BYTES would silently fall back to the
    # default. The name is correct; the coupling was not.
    MAX_VIDEO_NOTE_BYTES,
    MAX_VOICE_BYTES, MODEL, TELEGRAM_API,
)
from prompts import GROUP_CONTEXT, RATING_RULES

# === send_reply ===


async def send_reply(update: Update, text: str, *, strip: bool = True,
                     parse_mode: str | None = None):
    """Reply to a Telegram message, falling back to a helpful message if the model returned nothing.

    Telegram rejects empty messages with `Message text is empty`. Some models
    (Ornith-Uncensored in particular) spend the whole token budget on reasoning
    and return content='' for short prompts. We don't want the user to see a
    cryptic API error in that case.

    v0.10.2 (Qwen wave-3): applies per-chat rate limiting BEFORE sending.
    Without this, a burst (queue-drain after restart, or a rating-mode
    storm) can exceed Telegram's 20 msg/min/chat limit and trigger
    RetryAfter. _rate_limit_send sleeps if the chat is over budget.

    `strip=False` sends the text as it is. The chunking helper needs that:
    a chunk trimmed at the boundary loses the space that separated the
    last word of one chunk from the first word of the next, and the user
    reads "строка 372строка 373" instead of two sentences.

    `parse_mode` is Telegram's parse mode. It is None for the plain
    messages -- the notices, the errors: their text is written for a
    person and has no markup to render -- and "HTML" for the answer,
    whose console-output block is a code block Telegram can render
    natively. See telegram_format.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        await _rate_limit_send(chat_id)
    if strip:
        text = (text or '').strip()
    if not text or not text.strip():
        text = EMPTY_RESPONSE_FALLBACK
    return await _reply(update, text, parse_mode=parse_mode)


# === _reply_to_message_id ===


def _reply_to_message_id(update: Update):
    """The id of the message the user replied to, if it is in this chat.

    "Delete this one" is how a message usually gets asked away, and
    the reply is where the id lives: the Bot API cannot list a chat's
    messages, so the id of the message under the user's finger is the
    only one the bot can learn. A reply to a message in ANOTHER chat
    (a forwarded one) carries an id that means nothing here, so it is
    dropped.

    The implicit topic reply (real_reply_to) is dropped too: it is the
    topic's creation message, its id is the topic id, and no user ever
    pointed at it. On 2026-10-26 it was handed to the model as "the
    message the user replied to" (id 25), and the model tried to delete
    a service message that Telegram will not delete.
    """
    message = getattr(update, "message", None)
    replied = real_reply_to(update)
    if replied is None:
        return None
    chat = getattr(update, "effective_chat", None)
    replied_chat = getattr(replied, "chat", None)
    if chat is not None and replied_chat is not None:
        if getattr(replied_chat, "id", None) != getattr(chat, "id", None):
            return None
    return getattr(replied, "message_id", None)


# === _send_answer_text ===


async def _send_answer_text(update: Update, text: str):
    """Send the prose of an answer, split so that Telegram accepts it.

    Telegram's limit for a text message is 4096 characters. An answer
    over that comes back as `Message_too_long`, and the user sees the
    API error instead of the answer.

    The text and voice handlers have split their answer since v0.10.1.
    The photo and document paths did not, so a long answer to a photo
    died at the API boundary (T-FREEZE-CHUNK-1, 2026-10-24).

    All four handlers send their answer through here, so the next path
    cannot forget the split. `send_reply` does the rate limiting and
    the empty-answer fallback, and it runs once per chunk: the chunks
    are separate messages, and Telegram counts messages, not answers.

    The answer goes out as HTML. The console output the model produced
    is a fenced code block, and Telegram renders a code block natively
    only when the message carries a parse mode -- sent as plain text
    the user reads the backticks too. `telegram_format` escapes the
    prose and turns the complete code blocks into `<pre>`, and
    `split_html` splits the result so that a `<pre>` is never cut in
    half: a message Telegram cannot parse is a message the user never
    gets.
    """
    html = render_html(text)
    chunks = split_html(html, TELEGRAM_TEXT_CHUNK)
    # The answer is remembered, so the delete_messages tool can take it
    # back. The Bot API cannot list a chat's messages, so "my last
    # answer" is only knowable from what the bot remembers sending.
    chat_id = update.effective_chat.id if update.effective_chat else None
    group = new_answer_group()
    if not chunks:
        # Nothing but whitespace. send_reply turns it into the friendly
        # "no answer" message; sending nothing would leave the user
        # waiting for a turn that already ended.
        sent = await send_reply(update, text, parse_mode="HTML")
        if sent is not None:
            remember_answer(chat_id, sent.message_id, group)
        return
    for chunk in chunks:
        # strip=False: a chunk trimmed at the boundary loses the space
        # between the last word of this chunk and the first word of the
        # next one.
        sent = await send_reply(update, chunk, strip=False, parse_mode="HTML")
        if sent is not None:
            remember_answer(chat_id, sent.message_id, group)

# === _reply_active ===


async def _reply_active(update: Update, user_id: int) -> str:
    """Returns the user's active sub-talk name (resolving to 'main' default)."""
    return await asyncio.to_thread(storage.get_store().get_active_thread, user_id) or 'main'

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
    active = await asyncio.to_thread(storage.get_store().get_active_thread, user_id)
    if active is not None:
        return user_id, active, False
    # No sub-talks at all yet — auto-create 'main' so the user's first
    # message lands somewhere sensible without them needing to /newsub.
    await asyncio.to_thread(storage.get_store().create_thread, user_id, 'main')
    await asyncio.to_thread(storage.get_store().set_active_thread, user_id, 'main')
    return user_id, 'main', False



# === _persist_for_reply ===


async def _persist_for_reply(context, update, chat_id, thread_id, answer,
                              rating_active, abort_event=None):
    """Rating dispatch, persist the turn, and say whether there is an answer.

    Returns the text to send, or None when nothing should go out: the
    rating path consumed it (an emoji reaction and no text), the model
    answered nothing, or the user pressed Stop in the window between the
    transcript write and the reply.

    That last one is why `abort_event` is a parameter and not a check
    left at the call site. A Stop landing after the answer is persisted
    but before it is sent would otherwise put a cancelled turn on screen
    with a transcript entry behind it. The two streaming handlers pass
    the event; photo and document do not, because they do not stream and
    have no such window between the two points -- their guard was always
    `if bot_response`.

    The answer goes in as it was written, and the text that comes out is
    the prose. Those are two different strings since 2026-10-24: the code
    blocks the model wrote are delivered as documents, and the chat gets
    the rest. The bot remembers the whole answer, because the model
    wrote that code and a follow-up question about the file it just
    sent needs it to still be there. The trace is part of it for the
    same reason the model has always been shown its own tool trace.

    The >4000-char split is not here either. It belongs to the sending
    side -- `_send_answer_text` -- because the chat is the thing with a
    size limit, and the database has none. Two of the handlers have
    split their answer since v0.10.1; the photo and document paths
    joined them on 2026-10-24 (T-FREEZE-CHUNK-1), when a long answer
    to a photo turned into `Message_too_long`.

    A diagnostic is not an answer either. When the model wrote nothing
    and the empty-response retry was exhausted, the text that goes out
    is the fallback sentence, and the diagnostic stays in the record.
    That is the rule the rest of the bot follows: the chat gets a
    sentence the user can act on, and the detail goes to the log and
    to the record.
    """
    text = await _apply_rating_and_persist(
        context, update, chat_id, thread_id, answer.raw, rating_active,
    )
    if not text:
        return None
    if abort_event is not None and abort_event.is_set():
        return None
    sent = answer.text
    if sent and sent.startswith(EMPTY_RESPONSE_PREFIX):
        # The chat gets a sentence the user can act on; the log and the
        # record get what actually happened.
        #
        # The diagnostic is a technical string about the model's
        # internals, and it can be three kilobytes of it with the
        # model's reasoning tail attached. It reached the chat because
        # the empty-response retry chose the fallback and this function
        # then returned the answer's prose over it -- the fallback was
        # assigned and thrown away in the same breath (T-FREEZE-EMPTY-1).
        #
        # The record keeps the diagnostic: what happened is what the
        # bot remembers. What the user reads is the sentence.
        log.info(f"[persist_for_reply] the model returned no answer; the "
                 f"user gets the fallback. {sent.splitlines()[0][:200]}")
        sent = EMPTY_RESPONSE_USER_FALLBACK
    return sent



# === _open_thinking ===


async def _open_thinking(update, *, label: str):
    """The thinking message and the display name. None means it never sent.

    The first thing every handler does with the chat, and the two
    results are what the abort registration and the persist both need.

    None is not hypothetical: `_reply` gives up after three TimedOuts
    (T48), and all four handlers read `thinking.message_id` on the next
    line, which raises AttributeError on None. The guard belongs here,
    where the message was asked for -- not in a `finally`, which is
    cleanup and not a guard.

    The sender name comes after the message on purpose. Three handlers
    computed it first and photo second; nothing reads it between, so both
    orders are the same turn and this pins one.
    """
    thinking = await _reply(update, '💭 thinking…', reply_markup=_stop_button_markup())
    if thinking is None:
        log.warning(f"[{label}] could not send the thinking message after "
                    f"three timeouts; giving up on this update")
        return None
    return thinking, _sender_display_name(update.message.from_user)


# === _typing_indicator ===


async def _typing_indicator(chat, *, label: str) -> None:
    """Show the typing indicator, and never let it cost the user an answer.

    2026-10-27: a voice message was lost in production. The turn died
    on `telegram.error.NetworkError` -- an `httpx.ConnectError` to the
    Telegram API -- raised by `send_action`, which the handler called
    unguarded before anything else. The hiccup was over by the time the
    next update arrived (the very next text message was served normally),
    but the voice message was already gone: the handler had given up.

    The indicator is decoration. A Telegram API that refuses it for a
    moment must not cost the user their turn, so the failure is logged
    and the turn goes on.

    `except Exception`, not `BaseException`: a cancelled turn must still
    cancel. The four handlers called `send_action` each on their own;
    three of them had no guard at all, and the fourth had one copy of it
    inline. One helper, one guard.
    """
    try:
        await chat.send_action(action='typing')
    except Exception as e:
        log.warning(f"[{label}] send_action FAILED: {type(e).__name__}: {e!r}")


# === _turn_context ===


async def _turn_context(update, chat_id, thread_id, *, label: str):
    """Conversation history and whether this turn is in rating mode.

    For the three handlers that persist a turn first. Photo does not,
    and its order is genuinely different -- it reads rating mode before
    the history because it has nothing to persist -- so it is not
    included rather than being given a flag it does not use.
    """
    history = await _load_history(chat_id, thread_id)
    log.info(f"[{label}] user_id={update.effective_user.id} "
             f"thread_id={thread_id!r} history_len={len(history)}")
    return history, _is_rating_active(update)

# === _acquire_turn_slot ===


class _TurnSlot(NamedTuple):
    """What a handler needs once routing and the per-user slot are done.

    A named tuple rather than a bare one: all four callers unpack four
    values in the same order, and a bare tuple makes a reordered field a
    silent runtime swap instead of a compile error.
    """
    sem: object          # the per-user semaphore; released in the finally
    chat_id: int
    thread_id: str
    user_id: int


async def _acquire_turn_slot(update, context, *, label: str):
    """Route the message, then take the per-user slot. None means drop it.

    The first half of every handler, written four times. It stays here
    rather than in a leaf module because `_route_to_thread` is here, and
    moving that out would make the leaf import the module it was
    extracted from.

    `user_id` is the effective user's id, never the chat id. In private
    mode Telegram uses the same value for both, but in group mode
    chat_id is the group's id and user_id must be the user's. The old
    `user_id = chat_id` alias made the abort_event key mismatch what the
    callback handler looks up -- (group_id, group_id) instead of
    (group_id, real user_id) -- and the Stop button failed silently.
    """
    result = await _route_to_thread(update, context)
    if result is None:
        log.info(f"[{label}] REJECTED")
        return None
    chat_id, thread_id, is_group = result
    log.info(f"[{label}] chat_id={chat_id} thread_id={thread_id!r} is_group={is_group}")
    user_id = update.effective_user.id
    sem = await _check_user_slot(chat_id, user_id, update)
    if sem is None:
        return None
    return _TurnSlot(sem=sem, chat_id=chat_id, thread_id=thread_id, user_id=user_id)


# === _reply ===


async def _reply(update, text, **kwargs):
    """Reply to update.message, preserving message_thread_id in group mode.

    PTB's Message.reply_text() does NOT pass message_thread_id
    through to sendMessage, so replies in group mode would land
    in the General topic instead of staying in the user's topic.
    This helper fixes that.

    v0.7.9: also retries on telegram.error.RetryAfter (Telegram
    flood control). If a burst of queued messages produces
    replies faster than Telegram accepts, RetryAfter is raised;
    we sleep retry_after+1s and retry up to 3 times.

    T48: also retries on telegram.error.TimedOut (httpx 5s
    default). When the Telegram Bot API takes longer than 5s
    to acknowledge a sendMessage, TimedOut is raised even
    though the message was almost always delivered. The bot
    used to re-raise this and the handle_text except block
    would send a second "❌ Error: Timed out" message,
    producing two messages for one user turn. Now we retry
    up to 3 times with exponential backoff, and if all
    attempts time out we return None and log a warning
    (assuming Telegram will deliver the message anyway).
    """
    from telegram.error import BadRequest, RetryAfter, TimedOut
    if _is_group_chat(update):
        kwargs.setdefault('message_thread_id', update.message.message_thread_id)
    last_err = None
    for attempt in range(3):
        try:
            return await update.message.reply_text(text, **kwargs)
        except RetryAfter as e:
            last_err = e
            wait_s = float(getattr(e, 'retry_after', 1)) + 1.0
            log.info(f"[_reply] RetryAfter attempt {attempt+1}/3, sleeping {wait_s:.1f}s")
            await asyncio.sleep(wait_s)
        except TimedOut as e:
            last_err = e
            # Telegram usually delivered the message; the
            # timeout is in our read of the response. Exponential
            # backoff: 1s, 2s, 4s.
            backoff = 2 ** attempt
            log.info(f"[_reply] TimedOut attempt {attempt+1}/3, sleeping {backoff}s")
            await asyncio.sleep(backoff)
        except BadRequest as e:
            # The message we are answering is gone. The delete tool can
            # remove the very message the bot is about to reply to --
            # "delete the message I just wrote" -- and Telegram then
            # refuses the reply: "Message to be replied not found".
            # On 2026-10-26 that refusal was raised out of here, the
            # handler caught it and tried to send a failure notice the
            # same way, and the chat got a wall of the same traceback
            # instead of an answer.
            #
            # `allow_sending_without_reply` is Telegram's own answer to
            # this: send the message even if the quoted one is not
            # found. The quote is lost; the answer is not.
            if "Message to be replied not found" not in str(e):
                raise
            chat_id = update.effective_chat.id if update.effective_chat else None
            log.info(f"[_reply] the message being answered is gone; sending "
                     f"without the quote. chat_id={chat_id} text_len={len(text)}")
            kwargs["allow_sending_without_reply"] = True
            return await update.message.reply_text(text, **kwargs)
    # Exhausted retries. For RetryAfter, re-raise so the
    # caller's except block runs (the user already saw the
    # LLM response if at least one try went through). For
    # TimedOut, assume delivered and return None - the user
    # does NOT get a duplicate "❌ Error" message.
    if isinstance(last_err, TimedOut):
        chat_id = update.effective_chat.id if update.effective_chat else None
        log.info(f"[_reply] all 3 TimedOut attempts failed; assuming Telegram "
            f"delivered the message for chat_id={chat_id}. Suppressing "
            f"duplicate error reply. text_len={len(text)}")
        return None
    raise last_err

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
    active = await asyncio.to_thread(storage.get_store().get_active_thread, user_id)
    if active is not None:
        return active
    # No sub-talks at all yet — auto-create 'main' so the user's first
    # message lands somewhere sensible without them needing to /newsub.
    await asyncio.to_thread(storage.get_store().create_thread, user_id, 'main')
    await asyncio.to_thread(storage.get_store().set_active_thread, user_id, 'main')
    return 'main'

# === handle_photo ===


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info(f"[handle_photo] ENTRY chat_id={update.effective_chat.id} thread_id={update.message.message_thread_id} caption={update.message.caption!r}")
    slot = await _acquire_turn_slot(update, context, label='handle_photo')
    if slot is None:
        return
    sem, chat_id, thread_id, user_id = slot
    # B1: the per-user slot is acquired above. Everything between here and
    # the try/finally was NOT covered by it, so an exception here -- Telegram
    # rejecting the typing indicator, a malformed update -- discarded the slot
    # outright. With _PER_USER_SEMAPHORE_LIMIT=2, two such failures lock the
    # user out of the bot until the process restarts, and the only thing that
    # could return the token is a finally that was never entered.
    #
    # BaseException rather than Exception: a cancelled handler leaks the same
    # way and would never appear in a log.
    try:
        await _typing_indicator(update.message.chat, label='handle_photo')
        thinking = None
        # T67.2: the v0.9.1 `persisted` flag and its two rollbacks are gone with
        # the image write. They existed solely to undo _persist_message when the
        # user hit Stop or the call crashed — there is nothing to undo now, and
        # a rollback that fires without a write is worse than no rollback.
    except BaseException:
        try:
            sem.release()
        except Exception:
            pass
        raise

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
        log.info(f"[handle_photo] downloaded {len(photo_bytes)} bytes, caption={caption!r}")
        photo_content = [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": photo_data_url}}
        ]
        # v0.9.1 fix: send thinking message and register abort_event FIRST,
        # BEFORE any operation that can crash (download, parse, persist).
        # If a later step raises, the Stop button still works — handler
        # can edit thinking to an error message and clean up. Without this,
        # a BadRequest on get_file() (wrong file_id, expired) registered
        # no abort_event, and the next text message answered about the
        # (now-orphaned) image that was already persisted to history.
        opened = await _open_thinking(update, label='handle_photo')
        if opened is None:
            return
        thinking, sender_name = opened
        abort_event = asyncio.Event()
        _register_abort_event(chat_id, thinking.message_id, abort_event)
        # T67.2: images are NOT persisted.
        #
        # The operator's call (2026-10-20): the image participates in the
        # current exchange — the model sees it and answers — but it is not
        # worth storing, and it is not remembered after a restart.
        #
        # Implementation note, because this line looks redundant: the photo
        # previously reached the engine *only* by being written to SQLite and
        # read straight back out into `history` (call_llama builds
        # `msgs = [sys] + messages` and its `user_text` arg is dead code).
        # So "don't persist" and "the model sees the image" were the same
        # edit. `history` now ends at the previous turn, and the current
        # photo turn is appended below.
        #
        # History therefore keeps the caption as a text-only record, which is
        # what a later "what did that photo show?" degrades to — the model
        # falls back to its own description, as the comment below notes.
        history = await _load_history(chat_id, thread_id)
        current_turn = {"role": "user", "content": photo_content,
                        "_sender_name": sender_name,
                        "_telegram_message_id": update.message.message_id}
        # Appended AFTER load_history so it is not subject to the context
        # window or the message cap — this message is the reason for the call.
        # Without this the image would never reach the model, since the
        # image is not in SQLite (T67.2) and call_llama's `user_text`
        # parameter is dead code.
        history = history + [current_turn]
        log.info(f"[handle_photo] chat_id={chat_id} thread_id={thread_id!r} history_len={len(history)} (image not persisted)")
        # Compute rating_active here (before call_llama) so we
        # can pass it to the model AND use it in the dispatcher.
        rating_active = _is_rating_active(update)
        # T1 (P0-1): global LLM concurrency cap.
        async with _get_global_llm_sem():
            _result = await call_llama(history, max_tokens=limits.MAX_TOKENS_CAPTION, user_text=caption, thinking_msg=thinking, use_stream=False, rating_active=rating_active, abort_event=abort_event, bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id, chat_type=update.effective_chat.type, reply_to_message_id=_reply_to_message_id(update))
            answer = unwrap_answer(_result)
            bot_response = answer.text
        # None-safe: when user clicks Stop, call_llama returns None.
        # The previous print(f"...len(bot_response)...") crashed with
        # TypeError on None (caught by the outer except and surfaced
        # as "object of type 'NoneType' has no len()"). Guard it.
        response_chars = len(bot_response) if bot_response else 0
        response_head = (bot_response if bot_response else "<aborted>")[:200]
        log.info(f"[handle_photo] call_llama returned: {response_chars} chars, head={response_head!r}")
        if bot_response is None or abort_event.is_set():
            await finalize_aborted_turn(
                label='handle_photo', response=bot_response,
                abort_event=abort_event, thinking=thinking)
            return
        # === Empty-response retry ===
        # The LLM sometimes returns no content, no reasoning,
        # and no finish_reason — usually on hard questions
        # (future predictions, niche facts). The empty-response
        # marker is config.EMPTY_RESPONSE_PREFIX; this is
        # EMPTY_RESPONSE_PREFIX + the rest of the finish_reason.
        # We retry once. If still empty, replace with a
        # user-friendly message that suggests rephrasing or
        # trying again.
        if bot_response.startswith(EMPTY_RESPONSE_PREFIX):
            log.info(f"[handle_photo] empty response, retrying once")
            try:
                await thinking.edit_text('🔄 asking again…')
            except Exception:
                pass
            retry_response = await call_llama(
                history, max_tokens=limits.MAX_TOKENS_CAPTION, user_text=caption,
                thinking_msg=thinking, use_stream=False,
                rating_active=rating_active, abort_event=abort_event,
                bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id,
                chat_type=update.effective_chat.type,
                reply_to_message_id=_reply_to_message_id(update),
            )
            if retry_response is None or abort_event.is_set():
                await finalize_aborted_turn(
                    label='handle_photo', response=retry_response,
                    abort_event=abort_event, thinking=thinking,
                    attempt='retry')
                return
            # Same unwrap the primary path does -- the property that matters is that
            # this answer goes through the one formatter, not a copy of it.
            # Added 2026-10-22: the retry did not have it, so every
            # reachable retry -- i.e. every empty-response retry, which is
            # the only reason this code exists -- died on
            # AttributeError: 'dict' object has no attribute 'startswith'.
            # The executed-tools preamble is reproduced too; dropping it
            # would answer without the tool trace, which fails quietly.
            retry_answer = unwrap_answer(retry_response)
            if not (isinstance(retry_answer.text, str)
                    and retry_answer.text.startswith(
                        EMPTY_RESPONSE_PREFIX)):
                answer = retry_answer
                bot_response = answer.text
                log.info(f"[handle_photo] retry succeeded")
            else:
                log.info(f"[handle_photo] retry also empty")
                # The fallback is chosen in `_persist_for_reply`.
        # === Edit-during-LLM detection: removed (was v0.4) ===
        # Earlier versions compared the user's current message
        # text against a snapshot taken at handler entry, to
        # detect "user edited during LLM call". That required a
        # concurrent getUpdates consumer, which Telegram rejects
        # (HTTP 409) — the check was a no-op in practice. The
        # _user_msg_text dict and the helper function were
        # deleted in v0.5.1 (T4). The edit-replace feature that
        # used to cover this was removed on 2026-10-24: an edited
        # message is ignored now, so there is a wrong-answer
        # window until the user asks again. The Stop button
        # (per-handler abort_event) is the recommended escape for
        # long-running LLM calls.
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
        bot_response = await _persist_for_reply(
            context, update, chat_id, thread_id, answer,
            rating_active,
        )
        if bot_response:
            await _send_answer_text(update, bot_response)
        # The files go out even when there is no prose: an answer that
        # is nothing but a document has no prose to send, and dropping
        # the file would leave the user with nothing at all.
        await send_files(update, answer.files)
    except Exception as e:
        # `log.exception`, not `log.info`: every real failure in this file
        # was logged at INFO, which left `journalctl -p warning` empty
        # while the bot silently dropped work. It also carries the
        # traceback, so `traceback.print_exc()` -- which printed to
        # stderr, outside the log formatter, and therefore outside the
        # container log entirely -- is redundant.
        log.exception(f"[ERR photo] {type(e).__name__}: {e}")
        # T67.2: no rollback needed — the image was never written.
        try:
            await _reply(update, failure_message(e))
        except Exception:
            pass
    finally:
        # T46: revert T45. See handle_text for rationale.
        if thinking is not None:
            try:
                await thinking.delete()
            except Exception:
                pass
        # B1: the abort event is only registered when a thinking message
        # exists, so there is nothing to pop otherwise -- and reaching for
        # thinking.message_id anyway raised AttributeError. 2026-10-22: the
        # slot release that used to be described here moved out to the
        # handler's outermost finally, so that it covers the LLM call and
        # not just the window after it.
        #
        # Both layers are deliberate. The guard makes the pop correct; the
        # try makes it impossible for any future line added here to cost a
        # user their access to the bot.
        if thinking is not None:
            try:
                _abort_events.pop((chat_id, thinking.message_id), None)
            except Exception:
                pass
        try:
            sem.release()
        except Exception:
            pass

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
        log.info(f"[download] {kind.lower()} download failed: "
                 f"{type(e).__name__}: {e}")
        await _reply(update, DOWNLOAD_FAILURE_MESSAGE)
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
    slot = await _acquire_turn_slot(update, context, label='handle_voice')
    if slot is None:
        return
    sem, chat_id, thread_id, user_id = slot
    # B1: the per-user slot is acquired above. Everything between here and
    # the try/finally was NOT covered by it, so an exception here -- Telegram
    # rejecting the typing indicator, a malformed update -- discarded the slot
    # outright. With _PER_USER_SEMAPHORE_LIMIT=2, two such failures lock the
    # user out of the bot until the process restarts, and the only thing that
    # could return the token is a finally that was never entered.
    #
    # BaseException rather than Exception: a cancelled handler leaks the same
    # way and would never appear in a log.
    try:
        await _typing_indicator(update.message.chat, label='handle_voice')
        thinking = None
        # Built before the try: the except below calls turn.rollback(), and a Turn
        # constructed inside the try is unbound for any exception raised before
        # that line -- which is the window the rollback exists to cover.
        turn = Turn(chat_id, thread_id, "handle_voice")
    except BaseException:
        try:
            sem.release()
        except Exception:
            pass
        raise

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
        # v0.9.2: persist AFTER thinking+abort_event are wired so a Stop
        # click during the LLM call can roll the transcript back. See
        # handle_photo for the same pattern.
        opened = await _open_thinking(update, label='handle_voice')
        if opened is None:
            return
        thinking, sender_name = opened
        abort_event = asyncio.Event()
        _register_abort_event(chat_id, thinking.message_id, abort_event)
        await turn.persist(transcript, role='user', sender_name=sender_name,
                           telegram_message_id=update.message.message_id)
        history, rating_active = await _turn_context(
            update, chat_id, thread_id, label='handle_voice')
        # T1 (P0-1): global LLM concurrency cap.
        async with _get_global_llm_sem():
            _result = await call_llama(history, max_tokens=limits.MAX_TOKENS_CAPTION, user_text=transcript, thinking_msg=thinking, rating_active=rating_active, abort_event=abort_event, bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id, chat_type=update.effective_chat.type, reply_to_message_id=_reply_to_message_id(update))
            answer = unwrap_answer(_result)
            bot_response = answer.text
        # v0.8.0: handle_voice had no abort check at all (handle_photo/
        # handle_document/handle_text had `if bot_response is None` but
        # voice didn't). When call_llama returns None on abort,
        # _apply_rating_and_persist(None) is harmless, but if post won
        # the race and returned a non-None response, we'd send it
        # even though the user clicked Stop. Add the explicit check.
        if bot_response is None or abort_event.is_set():
            await finalize_aborted_turn(
                label='handle_voice', response=bot_response,
                abort_event=abort_event, thinking=thinking,
                turn=turn)
            return
        # === RATING_MODE dispatch (continuation) ===
        # rating_active was computed above (before call_llama).
        # T3 (P1-1): the 30-line RATING_MODE dispatch is now a
        # single helper call. The helper handles all 4 paths
        # (rating_active off, info/statement with rating, bloat,
        # question/request/confirmation) and returns the text
        # to reply with (possibly empty for rating-only turns).
        bot_response = await _persist_for_reply(
            context, update, chat_id, thread_id, answer,
            rating_active, abort_event=abort_event,
        )
        # v0.10.1 (Qwen wave-2 review): final abort check before sending.
        if bot_response:
            # The answer may be longer than Telegram accepts in one
            # message; _send_answer_text splits it. The rating path is
            # short and never reaches the split.
            await _send_answer_text(update, bot_response)
        # The files go out even when there is no prose: an answer that
        # is nothing but a document has no prose to send, and dropping
        # the file would leave the user with nothing at all.
        await send_files(update, answer.files)
    except Exception as e:
        log.exception(f"[ERR voice] {type(e).__name__}: {e}")
        # Rollback is best-effort here. Unguarded, a rollback failure
        # skipped the reply below and the user got silence instead of an
        # error -- the exact thing this handler is failing to do already.
        try:
            await turn.rollback('aborted')
        except Exception:
            log.exception("[ERR voice] rollback failed")
        try:
            await _reply(update, failure_message(e))
        except Exception:
            log.exception("[ERR voice] error reply failed")
    finally:
        # T46: revert T41. See handle_text for rationale.
        if thinking is not None:
            try:
                await thinking.delete()
            except Exception:
                pass
        # Same LRU cleanup the other three handlers do. Voice was the
        # one that skipped it, so a stream of voice notes spent up to a
        # quarter of the _ABORT_EVENTS_MAXSIZE budget on entries nothing
        # would ever read. The asymmetry was never documented, which is
        # what let it survive as if it were deliberate.
        if thinking is not None:
            try:
                _abort_events.pop((chat_id, thinking.message_id), None)
            except Exception:
                pass
        sem.release()

# === handle_document ===


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 2026-10-23: the first document this bot ever received in production
    # raised a question the log could not answer. _acquire_turn_slot logs
    # the route, but nothing said WHICH file arrived, so a document that
    # never showed up looked exactly like a photo run. Logged before the
    # slot is taken, so a document dropped by the per-user cap is visible.
    _doc = getattr(update.message, 'document', None)
    log.info(f"[handle_document] ENTRY chat_id={update.effective_chat.id} "
             f"thread_id={update.message.message_thread_id} "
             f"file_name={getattr(_doc, 'file_name', None)!r} "
             f"file_size={getattr(_doc, 'file_size', None)} "
             f"mime={getattr(_doc, 'mime_type', None)!r} "
             f"caption={update.message.caption!r}")
    slot = await _acquire_turn_slot(update, context, label='handle_document')
    if slot is None:
        return
    sem, chat_id, thread_id, user_id = slot
    # 2026-10-22: built above the try whose except calls turn.rollback().
    # Inside it, an exception during the download left `turn` unbound and
    # the rollback turned into an UnboundLocalError that replaced the real
    # error. tmp_path is here for the same reason: the main finally reads
    # it, so an early return before its assignment raised inside the
    # finally and skipped the slot release.
    tmp_path = None
    thinking = None
    turn = Turn(chat_id, thread_id, "handle_document")
    try:
        # B1: the per-user slot is acquired above. Everything between here and
        # the try/finally was NOT covered by it, so an exception here -- Telegram
        # rejecting the typing indicator, a malformed update -- discarded the slot
        # outright. With _PER_USER_SEMAPHORE_LIMIT=2, two such failures lock the
        # user out of the bot until the process restarts, and the only thing that
        # could return the token is a finally that was never entered.
        #
        # BaseException rather than Exception: a cancelled handler leaks the same
        # way and would never appear in a log.
        try:
            await _typing_indicator(update.message.chat, label='handle_document')
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
        except BaseException:
            raise

        try:
            ext = doc.file_name.split('.')[-1] if '.' in doc.file_name else 'txt'
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as tmp:
                tmp.write(doc_bytes)
                tmp_path = tmp.name
            with open(tmp_path, 'r', encoding='utf-8', errors='ignore') as f:
                doc_text = f.read()[:16000]
            caption = update.message.caption or 'Read the document and answer questions.'
            user_content = f"{caption}\n\n--- Document ---\n{doc_text}"
            # v0.9.2: persist AFTER thinking+abort_event are wired so a
            # Stop click during the LLM call can roll the document back.
            opened = await _open_thinking(update, label='handle_document')
            if opened is None:
                return
            thinking, sender_name = opened
            abort_event = asyncio.Event()
            _register_abort_event(chat_id, thinking.message_id, abort_event)
            await turn.persist(user_content, role='user',
                               sender_name=sender_name,
                               telegram_message_id=update.message.message_id)
            history, rating_active = await _turn_context(
                update, chat_id, thread_id, label='handle_document')
            # T1 (P0-1): global LLM concurrency cap.
            async with _get_global_llm_sem():
                _result = await call_llama(history, max_tokens=limits.MAX_TOKENS_CAPTION, user_text=caption, thinking_msg=thinking, rating_active=rating_active, abort_event=abort_event, bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id, chat_type=update.effective_chat.type, reply_to_message_id=_reply_to_message_id(update))
                answer = unwrap_answer(_result)
                bot_response = answer.text
            if bot_response is None or abort_event.is_set():
                await finalize_aborted_turn(
                    label='handle_document', response=bot_response,
                    abort_event=abort_event, thinking=thinking,
                    turn=turn)
                return
            # === Empty-response retry (same as handle_photo) ===
            if bot_response.startswith(EMPTY_RESPONSE_PREFIX):
                log.info(f"[handle_document] empty response, retrying once")
                try:
                    await thinking.edit_text('🔄 asking again…')
                except Exception:
                    pass
                retry_response = await call_llama(
                    history, max_tokens=limits.MAX_TOKENS_CAPTION, user_text=caption,
                    thinking_msg=thinking, rating_active=rating_active,
                    abort_event=abort_event,
                    bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id,
                    chat_type=update.effective_chat.type,
                reply_to_message_id=_reply_to_message_id(update),
                )
                if retry_response is None or abort_event.is_set():
                    await finalize_aborted_turn(
                        label='handle_document', response=retry_response,
                        abort_event=abort_event, thinking=thinking,
                        attempt='retry',
                        turn=turn)
                    return
                # Same unwrap the primary path does -- the property that matters is that
                # this answer goes through the one formatter, not a copy of it.
                # Added 2026-10-22: the retry did not have it, so every
                # reachable retry -- i.e. every empty-response retry, which is
                # the only reason this code exists -- died on
                # AttributeError: 'dict' object has no attribute 'startswith'.
                # The executed-tools preamble is reproduced too; dropping it
                # would answer without the tool trace, which fails quietly.
                retry_answer = unwrap_answer(retry_response)
                if not (isinstance(retry_answer.text, str)
                        and retry_answer.text.startswith(
                            EMPTY_RESPONSE_PREFIX)):
                    answer = retry_answer
                    bot_response = answer.text
                    log.info(f"[handle_document] retry succeeded")
                else:
                    log.info(f"[handle_document] retry also empty")
                    # The fallback is chosen in `_persist_for_reply`.
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
            bot_response = await _persist_for_reply(
                context, update, chat_id, thread_id, answer,
                rating_active,
            )
            if bot_response:
                await _send_answer_text(update, bot_response)
            # The files go out even when there is no prose: an answer that
            # is nothing but a document has no prose to send, and dropping
            # the file would leave the user with nothing at all.
            await send_files(update, answer.files)
        except Exception as e:
            log.exception(f"[ERR doc] {type(e).__name__}: {e}")
            # The user sent a document and is waiting. text/photo/voice
            # all answer with the error; this one used to roll back and
            # say nothing, which is indistinguishable from a hung bot.
            try:
                await turn.rollback('aborted')
            except Exception:
                log.exception("[ERR doc] rollback failed")
            try:
                await _reply(update, failure_message(e))
            except Exception:
                log.exception("[ERR doc] error reply failed")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            # T46: revert T45. See handle_text for rationale.
            if thinking is not None:
                try:
                    await thinking.delete()
                except Exception:
                    pass
            # B1: the abort event is registered only when a thinking message
            # exists, so there is nothing to pop otherwise. The per-user slot is
            # released by this handler's outermost finally, not here -- see the
            # comment on that finally for why the timing matters.
            #
            # Both layers are deliberate. The guard makes the pop correct; the
            # try makes it impossible for any future line added here to cost a
            # user their access to the bot.
            if thinking is not None:
                try:
                    _abort_events.pop((chat_id, thinking.message_id), None)
                except Exception:
                    pass

    finally:
        # 2026-10-22: released at the outermost exit, so the slot is held
        # for the whole handler including the LLM call. Releasing inside
        # the B1 window covered every early return but happened before
        # call_llama, which left _PER_USER_SEMAPHORE_LIMIT measuring
        # nothing.
        sem.release()


# === handle_text ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No `_get_user_semaphore as _user_semaphore`: it was imported here
    # and never read (AST: zero `Name` uses), left over from when the
    # slot was taken here rather than inside `call_llama`.
    from state import _check_burst_split, _record_user_message, _user_recent_msg
    log.info(f"[handle_text] ENTRY t={time.monotonic():.3f} user_id={update.effective_user.id} chat_type={update.effective_chat.type} is_forum={getattr(update.effective_chat, 'is_forum', None)} thread_id={update.message.message_thread_id} text_len={len(update.message.text or '')} text_preview={update.message.text[:80]!r}")
    # T34 burst detection (race fix v2, 2026-10-13). Record the
    # current message in _user_recent_msg IMMEDIATELY on entry,
    # creating the abort_event at the same time. The event is
    # the SAME one the eventual LLM call receives — if a
    # follow-up bursts in 60 ms later, its burst check finds
    # this event, sets it, and the first message's LLM call
    # (which hasn't even started yet at the time of the burst)
    # sees is_set() == True and returns None.
    #
    # v1 of this fix recorded with abort_event=None. That made
    # _check_burst_split return None (because the recorded event
    # was None) so the burst was missed. The event must be
    # created and registered into _user_recent_msg at the very
    # entry, not later.
    user_id_check = update.effective_user.id
    chat_id_check = update.effective_chat.id
    text_bytes = len((update.message.text or '').encode('utf-8'))
    early_event = asyncio.Event()
    _record_user_message(chat_id_check, user_id_check, text_bytes, early_event)
    log.info(f"[handle_text] EARLY-RECORD t={time.monotonic():.3f} (chat={chat_id_check}, user={user_id_check}, size={text_bytes}, event_id={id(early_event)})")
    # Telegram-split detection (T34, 2026-10-13). Pattern-based:
    # if the SAME (chat, user) sent a >3000-byte message within
    # the last BURST_WINDOW_SEC (2s), the current message is
    # likely a follow-up of a Telegram auto-split. Drop the
    # current message and ABORT the in-flight LLM call for the
    # previous one, then reply with a clear error.
    prev_event = _check_burst_split(chat_id_check, user_id_check, text_bytes)
    if prev_event is not None:
        log.info(f"[handle_text] BURST-SPLIT: (chat={chat_id_check}, user={user_id_check}) current_size={text_bytes}; setting prev_event (id={id(prev_event)}) to abort the in-flight LLM call and dropping current message")
        prev_event.set()
        # Clear the registry entry so a third consecutive message
        # within the window is not also treated as a split
        # follow-up. The user has to /reset or wait 2s.
        _user_recent_msg.pop((chat_id_check, user_id_check), None)
        try:
            await update.message.reply_text(
                "🚫 ⚠️ Detected a Telegram auto-split: you sent two messages within 2 seconds, and the first was >3000 bytes. The first message's LLM call has been aborted and this follow-up was dropped to avoid two half-answers. Run /reset and resend as a single message, or send via the Telegram Bot API directly to bypass the 4096-char UI cap."
            )
        except Exception as e:
            log.info(f"[handle_text] burst-split reply failed: {type(e).__name__}: {e!r}")
        return
    slot = await _acquire_turn_slot(update, context, label='handle_text')
    if slot is None:
        return
    sem, chat_id, thread_id, user_id = slot
    user_message = update.message.text
    turn = Turn(chat_id, thread_id, "handle_text")
    thinking = None
    try:
        # B1: the per-user slot is acquired above and everything up to the main
        # try/finally was NOT covered by it. This window is not just awaits --
        # it contains turn.persist(), so a failure after that write left a
        # half-written user message in SQLite AND discarded the slot.
        # _PER_USER_SEMAPHORE_LIMIT is 2, so two such failures lock the user out
        # of the bot until the process restarts.
        #
        # The except mirrors the main one rather than just releasing: rollback if
        # we persisted, deregister the abort event, release, re-raise.
        # BaseException because a cancelled handler leaks the same way and would
        # never appear in a log.
        try:
            await _typing_indicator(update.message.chat, label='handle_text')
            # v0.9.2: persist AFTER thinking+abort_event are wired so a Stop
            # click during the LLM call can roll the message back.
            opened = await _open_thinking(update, label='handle_text')
            if opened is None:
                return
            thinking, sender_name = opened
            abort_event = early_event
            _register_abort_event(chat_id, thinking.message_id, abort_event)
            await turn.persist(user_message, role='user',
                               sender_name=sender_name,
                               telegram_message_id=update.message.message_id)
            # Refresh the registry entry so the abort_event and the
            # recorded size match (the EARLY-RECORD used text_bytes
            # of the raw message; LATE-RECORD uses the same value but
            # keeps the registry authoritative in case of a future
            # refactor).
            msg_bytes = len(user_message.encode('utf-8'))
            _record_user_message(chat_id, user_id, msg_bytes, abort_event)
            log.info(f"[handle_text] LATE-RECORD t={time.monotonic():.3f} (chat={chat_id}, user={user_id}, size_bytes={msg_bytes}, event_id={id(abort_event)}) [reused early_event, registered in _abort_events]")
            history, rating_active = await _turn_context(
                update, chat_id, thread_id, label='handle_text')
            # T1 (P0-1): wrap call_llama in the global semaphore so
            # 50 different users in a group can only cause 4
            # concurrent LLM forwards on the same GPU.
            async with _get_global_llm_sem():
                _result = await call_llama(history, max_tokens=limits.MAX_TOKENS_DEFAULT, user_text=user_message, thinking_msg=thinking, rating_active=rating_active, abort_event=abort_event, bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id, chat_type=update.effective_chat.type, reply_to_message_id=_reply_to_message_id(update))
                answer = unwrap_answer(_result)
                bot_response = answer.text
            if bot_response is None or abort_event.is_set():
                await finalize_aborted_turn(
                    label='handle_text', response=bot_response,
                    abort_event=abort_event, thinking=thinking,
                    turn=turn)
                return
            # === Empty-response retry (same as handle_photo) ===
            if bot_response.startswith(EMPTY_RESPONSE_PREFIX):
                log.info(f"[handle_text] empty response, retrying once")
                try:
                    await thinking.edit_text('🔄 asking again…')
                except Exception:
                    pass
                retry_response = await call_llama(
                    history, max_tokens=limits.MAX_TOKENS_DEFAULT, user_text=user_message,
                    thinking_msg=thinking, rating_active=rating_active,
                    abort_event=abort_event,
                    bot=context.bot, chat_id=chat_id, current_message_id=update.message.message_id,
                    chat_type=update.effective_chat.type,
                reply_to_message_id=_reply_to_message_id(update),
                )
                if retry_response is None or abort_event.is_set():
                    await finalize_aborted_turn(
                        label='handle_text', response=retry_response,
                        abort_event=abort_event, thinking=thinking,
                        attempt='retry',
                        turn=turn)
                    return
                # Same unwrap the primary path does -- the property that matters is that
                # this answer goes through the one formatter, not a copy of it.
                # Added 2026-10-22: the retry did not have it, so every
                # reachable retry -- i.e. every empty-response retry, which is
                # the only reason this code exists -- died on
                # AttributeError: 'dict' object has no attribute 'startswith'.
                # The executed-tools preamble is reproduced too; dropping it
                # would answer without the tool trace, which fails quietly.
                retry_answer = unwrap_answer(retry_response)
                if not (isinstance(retry_answer.text, str)
                        and retry_answer.text.startswith(
                            EMPTY_RESPONSE_PREFIX)):
                    answer = retry_answer
                    bot_response = answer.text
                    log.info(f"[handle_text] retry succeeded")
                else:
                    log.info(f"[handle_text] retry also empty")
                    # The fallback is chosen in `_persist_for_reply`.
            # Edit-during-LLM detection removed: the check is a
            # no-op without a concurrent getUpdates consumer.
            # See handle_photo for the full rationale.
            # === RATING_MODE dispatch ===
            # T3 (P1-1): the 30-line RATING_MODE dispatch is now a
            # single helper call. The helper handles all 4 paths
            # (rating_active off, info/statement with rating, bloat,
            # question/request/confirmation) and returns the text
            # to reply with (possibly empty for rating-only turns).
            bot_response = await _persist_for_reply(
                context, update, chat_id, thread_id, answer,
                rating_active, abort_event=abort_event,
            )
            # v0.10.1 (Qwen wave-2 review): final abort check before sending.
            if bot_response:
                await _send_answer_text(update, bot_response)
            # The files go out even when there is no prose: an answer that
            # is nothing but a document has no prose to send, and dropping
            # the file would leave the user with nothing at all.
            await send_files(update, answer.files)
        except Exception as e:
            import traceback
            log.exception(f"[ERR text] {type(e).__name__}: {e}")
            traceback.print_exc()
            await turn.rollback('aborted')
            await _reply(update, failure_message(e))
        finally:
            # T46: revert T45 (and original T41) edit_text pattern.
            # The success path already sends the final response via
            # send_reply, so editing the thinking message into the
            # same content produces a duplicate message. The
            # original "two message" issue (silent delete failure)
            # is an acceptable trade-off here - the thinking block
            # may occasionally linger in the chat, but the user
            # sees the final answer in a single, separate message
            # which is the desired behaviour.
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
            # B1: the abort event is registered only when a thinking message
            # exists, so there is nothing to pop otherwise. The per-user slot is
            # released by this handler's outermost finally, not here -- see the
            # comment on that finally for why the timing matters.
            #
            # Both layers are deliberate. The guard makes the pop correct; the
            # try makes it impossible for any future line added here to cost a
            # user their access to the bot.
            if thinking is not None:
                try:
                    _abort_events.pop((chat_id, thinking.message_id), None)
                except Exception:
                    pass



    finally:
        # 2026-10-22: released at the outermost exit, so the slot is held
        # for the whole handler including the LLM call. Releasing inside
        # the B1 window covered every early return but happened before
        # call_llama, which left _PER_USER_SEMAPHORE_LIMIT measuring
        # nothing.
        sem.release()


# === Re-exported helpers for tests ===
async def _persist_message(*args, **kwargs):
    return await persistence.persist(*args, **kwargs)


async def _load_history(*args, **kwargs):
    return await persistence.load_history(*args, **kwargs)
