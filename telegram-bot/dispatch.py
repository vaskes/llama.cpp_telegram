# dispatch.py -- routing, and nothing else.
#
# TECH-DEBT #10 blocks D/E/F took this from 974 lines to under 400. What
# is left decides: `_dispatch_update` routes an update,
# `_handle_reply_to_other_user` is the reply-to-someone-else branch,
# `main` picks polling over webhook, the caps bound them.
# The PTB surface moved out -- `dispatch_polling.py` (signals, runtime
# prep, getUpdates loop, task factory, Application registry),
# `dispatch_webhook.py` (Application assembly, handler registration, the
# command menu, chat-member bookkeeping), `dispatch_edit.py` (the
# edited-message rewrite). None of it could pick a handler.

import asyncio
import signal
import json
import os
import time
from typing import Optional

import httpx
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

import call_llama
import storage
import dispatch_polling
from context_budget import get_budget
from reanchor import start_reanchor_scheduler as _start_reanchor_scheduler
from reanchor import start_summarise_on_boot as _start_summarise_on_boot
from selftest import _selftest
# TECH-DEBT #10 block D: the polling path moved to `dispatch_polling.py`,
# which also owns the Application registry.
# TECH-DEBT #10 block E: PTB construction and chat-member bookkeeping moved
# to `dispatch_webhook.py`. This module keeps the routing decision.
# TECH-DEBT #10 block F: the edited-message rewrite is its own module now.
from dispatch_edit import _apply_edited_message
from dispatch_webhook import (
    _attach_handlers,
    _build_application,
    _handle_chat_member_update,
    _register_bot_menu,
    _webhook_auto_register_sync,
)
from dispatch_polling import (
    _dispatch_tasks,
    _poll_loop,
    _spawn_dispatch,
)
# TECH-DEBT #2: these ten used to be defined in dispatch.py and imported by
# handlers.py *inside functions* to dodge a cycle. None of them touch the
# dispatcher, so they live in leaf modules now and both sides import them at
# module level. Re-exported under their old names because bot.py and existing
# tests reach them through `dispatch`.
from access import (
    _check_user_slot, _is_reply_to_other_user, _should_mute_in_group,
    is_authorized, reject_if_unauthorized,
)
from llm_markup import _stop_button_markup
from chat_utils import (
    _RATE_MAX_PER_WINDOW, _RATE_WINDOW_SEC, _SEND_WINDOW_MAX_KEYS,
    _is_group_chat, _parse_subtalk_arg, _parse_topic_arg,
    _rate_limit_send, _send_window, _SUBTALK_NAME_RE, _TOPIC_NAME_RE,
)
import state
from state import (
    _GLOBAL_LLM_SEM_LIMIT, _PER_USER_SEMAPHORE_LIMIT,
    _abort_events, _bot_replies, _global_llm_sem,
    _get_user_semaphore, _user_semaphores,
)
import handlers

from logconf import get_logger
log = get_logger(__name__)

# === Module-level state (lazy singleton) ===

import persistence
import prompts
import rating
import limits
from config import (
    ALLOWED_USER_IDS, ALLOWED_USERNAMES, API_KEY, BOT_TOKEN,
    BOT_USERNAME, DB_PATH, DISABLED_TOOLS, DONSETCH_URL,
    LLAMA_URL, LOCKDOWN, MAX_PHOTO_BYTES, MAX_DOC_BYTES, MAX_VOICE_BYTES,
    MAX_VIDEO_NOTE_BYTES, MODEL, SHUTDOWN_EVENT, TELEGRAM_API,
    WHISPER_URL, _TOOLS_CACHE, LLAMABOT_SELFTEST,
)
from prompts import (
    BLOAT_EMOJI, GROUP_CONTEXT, RATING_EMOJI, RATING_MODE,
    RATING_RULES, WELCOME_TEXT, _EMOJI_CHAR_RE, _RATING_PREFIX_RE,
    _LLM_TOKEN_RE, _SUBTALK_NAME_RE, _TOPIC_NAME_RE,
)
from persistence import persist as _persist_message, load_history as _load_history
from handlers import (
    handle_document, handle_photo, handle_text, handle_voice,
    send_reply, _download_with_limit, _general_thread_id, _reply,
    _reply_active, _resolve_active, _route_to_thread, _sender_display_name,
    _reject_in_group,
)

from rating import (
    _apply_rating_and_persist, _apply_reaction, _execute_react_to_message,
    _is_rating_active, _parse_rating_response,
)
# TECH-DEBT #10: the command handlers moved into `commands/`. Re-exported
# under their old names because bot.py and the tests reach them through
# `dispatch`, and because the routing here refers to them by bare name.
from commands import (
    cmd_callback, cmd_delsub, cmd_here, cmd_help, cmd_newsub, cmd_sub,
    cmd_subs, reset, start, stats,
)


async def _handle_reply_to_other_user(upd, bot) -> None:
    """T65.1 — a human replied to another human in a group.

    The operator's design, verbatim from the ticket: the message must still be
    *seen*. The model reads it, thinks about it, and leaves a reaction via the
    react_to_message tool. What it must NOT do is answer, and the reasoning
    must not appear in the chat — the group is talking to itself, and a visible
    "💭 думаю…" from the bot on someone else's message is noise.

    So: real LLM call, `silent=True`. Not a skip.

    What was here before: an unconditional `return` before process_update,
    which meant the message was never written to the DB at all. That is the
    silent data loss this ticket exists to fix — the operator keeps this
    history for a LoRA dataset, and an entire class of group conversation was
    silently dropped.
    """
    msg = upd.message
    rtm = msg.reply_to_message
    rtm_from = rtm.from_user if rtm and rtm.from_user else None
    tag = (
        f"@{rtm_from.username}" if rtm_from and rtm_from.username
        else f"id={rtm_from.id}" if rtm_from else "?"
    )
    chat_id = upd.effective_chat.id
    thread_id = str(getattr(msg, "message_thread_id", None) or "main")
    who = msg.from_user
    sender = (
        f"@{who.username}" if getattr(who, "username", None)
        else (getattr(who, "first_name", None) or "unknown")
    )
    log.info(f"[dispatch] reply-to-other-user: absorbing silently "
        f"(reply_to={tag}, chat={chat_id}, thread={thread_id}) "
        f"update_id={upd.update_id}")

    # 1. Persist. ALWAYS, and before anything that can fail — this is the part
    #    the ticket is really about.
    body = msg.text or msg.caption or ""
    content = f"[reply to {tag}] {body}" if tag else body
    try:
        await _persist_message(
            chat_id, thread_id, "user", content, sender_name=sender,
        )
    except Exception as exc:
        log.info(f"[dispatch] persist failed for reply-to-other-user: {exc!r}")

    # 2. Build context exactly as a normal turn would, then ask the model to
    #    react without answering. `silent` suppresses the reasoning stream.
    try:
        history = await _load_history(chat_id, thread_id)
        silent_prompt = (
            "This message was sent by one human in the group to ANOTHER human, "
            "not to you. Do not reply and do not say anything. Look at it, then "
            "express your reaction ONLY by calling react_to_message with a "
            "single emoji. Your reasoning is private and will not be shown."
        )
        await call_llama.call_llama(
            history,
            max_tokens=limits.MAX_TOKENS_REACT_ONLY,
            user_text=silent_prompt,
            thinking_msg=None,          # nothing to update: no chat message
            use_stream=False,
            bot=bot,
            chat_id=chat_id,
            current_message_id=msg.message_id,
            chat_type=upd.effective_chat.type,
            silent=True,                # reasoning stays out of the GUI
        )
        log.info(f"[dispatch] absorbed reply-to-other-user "
            f"update_id={upd.update_id}")
    except Exception as exc:
        # A failed reaction must never lose the persisted message, and must
        # never surface as an error to the group.
        log.info(f"[dispatch] silent-absorb failed (message already persisted) "
            f"update_id={upd.update_id}: {type(exc).__name__}: {exc}")

# === Dispatch task tracking ===
#
# Review finding (Qwen, 2026-10-21): the polling loop called
# `asyncio.create_task(_dispatch_update(...))` and dropped the reference. Two
# problems, both invisible:
#
#   1. Nothing holds the task, so it is only kept alive by the loop's internal
#      bookkeeping. On interpreter shutdown an in-flight turn can be cancelled
#      mid-write, leaving a persisted user message with no assistant reply.
#   2. An unhandled exception inside the task dies quietly. The bot simply stops
#      answering that chat and the traceback goes to stderr, unconnected to any
#      update id -- you cannot correlate it back to what the user did.
#
# So: keep a set, drop entries in a done callback, and log the failure WITH the
# update id. The set is what stops the GC from collecting a running task.

# === _rate_limit_send ===
# v0.7.9: Telegram rate-limits the bot to ~20 messages/minute per chat.
# A burst of queued messages (the v0.7.8 fix) can produce replies
# faster than Telegram accepts them, triggering RetryAfter. We keep
# a 60s window of send timestamps; if the window is full, we sleep
# until the oldest entry ages out before sending.
from collections import deque
import time as _time
#: How many chat_ids we are willing to track at once. Above this we start
#: dropping idle entries; an active chat re-creates its own deque on the next
#: send, so the cap costs a re-allocation and never correctness.


# === main ===

def main():
    # === Pure-httpx polling loop, no PTB Updater, no app.start() ===
    # Rationale: with `app.initialize()` or `app.start()` PTB holds a
    # second keep-alive connection open to api.telegram.org, which makes
    # Telegram see a "duplicate long-poll" and 409 every other cycle.
    # Solution: never use PTB's HTTP client. Hit the Bot API directly
    # with a single dedicated httpx.AsyncClient, de_json() into Update
    # objects, then call handler functions ourselves (not via dispatcher,
    # to skip process_update's internal queue/worker logic).
    log.info('🤖 LlamaBot v2 started...')

    # === T25: webhook mode (replaces polling loop) ===
    # When WEBHOOK_MODE=1, run an aiohttp server bound to
    # 127.0.0.1:WEBHOOK_PORT. nginx-proxy in front terminates
    # TLS and forwards POST /tg-webhook/<token> here. The
    # handler verifies X-Telegram-Bot-Api-Secret-Token and
    # delegates to the same _dispatch_update() the polling
    # loop would call. Falling back to polling is as simple
    # as setting WEBHOOK_MODE=0 (or removing the env var).
    import config as _config
    if _config.WEBHOOK_MODE:
        from webhook import run as _webhook_run
        log.info(f"[main] webhook mode: aiohttp on "
            f"http://{_config.WEBHOOK_HOST}:{_config.WEBHOOK_PORT}"
            f"{_config.WEBHOOK_PATH}/")
        # v0.9.3: auto-register the webhook with Telegram if WEBHOOK_URL
        # is set, using a sync HTTP call. The v0.9.2 implementation
        # initialized a Bot object in a first asyncio.run() loop and
        # then closed it; the Bot's httpx client was dead by the time
        # the second loop (_webhook_run) started, so all subsequent
        # bot.send_message() calls failed.
        #
        # Sync HTTP sidesteps that: set_webhook is a one-shot call,
        # it doesn't need the persistent Bot, and urllib's
        # connection pool closes cleanly when the with-block exits.
        #
        # MUST include `allowed_updates` to receive callback_query
        # (the Stop button) and chat_member (new-member join) updates.
        # Telegram defaults to ["message", "edited_message"] which
        # silently drops everything else. Without callback_query,
        # the Stop button appears in the UI but clicks are lost.
        if _config.WEBHOOK_URL and _config.WEBHOOK_SECRET_TOKEN:
            _webhook_auto_register_sync(
                url=_config.WEBHOOK_URL,
                secret_token=_config.WEBHOOK_SECRET_TOKEN,
            )
        asyncio.run(_webhook_run())
        return

    # W6-H2 follow-up. This used to be:
    #     TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
    # which silently discarded the env-driven API root config builds
    # builds for smoke testing -- the whole point of the v0.10.2
    # override. The module-level import at line 59 already binds
    # TELEGRAM_API correctly; the local assignment only shadowed it.
    # Webhook mode returns above and never reached the old line, which
    # is why the drift went unnoticed.
    assert TELEGRAM_API, (
        "TELEGRAM_API resolved to empty; check the env override in "
        "config.py and BOT_TOKEN"
    )
    log.info('[main] polling mode: long-poll getUpdates')

    # Graceful shutdown: SIGINT (Ctrl+C) and SIGTERM (docker stop / systemd
    # stop) set this event; the polling loop checks it at the top of each
    # iteration. Without this, KeyboardInterrupt aborts the in-flight
    # httpx call mid-stream and any in-progress handler dies with an
    # unhandled exception traceback instead of returning cleanly.
    shutdown_event = asyncio.Event()

    try:
        # Passed in rather than imported, so `dispatch_polling` has no edge
        # back into this module and the import graph stays a cycle smaller
        # than it was before the split.
        asyncio.run(_poll_loop(
            shutdown_event,
            register_bot_menu=_register_bot_menu,
            dispatch_update=_dispatch_update,
            run_selftest=_selftest,
        ))
    except KeyboardInterrupt:
        log.info('[main] KeyboardInterrupt')

# === application assembly ===

# === _register_bot_menu ===

# === _handle_chat_member_update ===

# === _apply_edited_message ===


# === _dispatch_update ===
# Guards the lazy Application build. Two updates arriving together (a
# Telegram retry burst after a restart, with `_MAX_PENDING_DISPATCH=50`
# ready to run them) both read None, both build an Application, and both
# start the boot jobs. Measured on wave 5: 10 gathered updates produced
# 10 Applications and 20 scheduler runs -- and `_summarise_on_boot`
# walks every thread on a single-GPU box, so that is a self-inflicted
# OOM rather than just a leaked httpx pool.
_app_init_lock = asyncio.Lock()


async def _dispatch_update(upd_dict):
    app = dispatch_polling.get_dispatcher()
    if app is None:
        # Double-checked locking: inside the lock another coroutine may
        # already have published an Application, and then that one --
        # the instance that ran initialize() -- is the one to use.
        async with _app_init_lock:
            app = dispatch_polling.get_dispatcher()
            if app is None:
                # W2-H2. Without base_url the PTB Bot talks to api.telegram.org
                # no matter what TELEGRAM_API says, so sendMessage, editMessageText,
                # getMe, setMyCommands, set_message_reaction and every file
                # download all bypass the override. The v0.10.2 env override only
                # ever covered the raw-httpx getUpdates loop. Without this line a
                # smoke test against a mock server sends half its traffic to the
                # real Telegram.
                app = _build_application()

                async def log_error(update, context):
                    err = context.error
                    log.exception(
                        f"[handler error] {type(err).__name__}: {err!r}")

                app.add_error_handler(log_error)
                # PTB 21 requires Application.initialize() before process_update.
                # We do not call app.start() — the hand-rolled polling loop drives
                # dispatch. initialize() sets up the application context.
                #
                # Exactly once. Twice here for two waves was harmless -- PTB opens
                # with `if self._initialized: return` -- but a duplicate makes
                # "removed" and "added a redundant one" identical in the diff.
                await app.initialize()
                # Written through the polling module's setter, not by rebinding an
                # imported name: it owns the registry, and a rebind here would only
                # touch this module's copy of the binding.
                dispatch_polling.set_dispatcher(app)
                log.info("[dispatch] Application initialized (lazy)")
                _start_reanchor_scheduler()
                _start_summarise_on_boot()

    # `app` is live from here on: either it came from the registry, or the
    # branch above built and published it. Every read below goes through
    # this local, not through the registry, because re-reading a global
    # five times is how a module ends up with two copies of one truth.
    #
    # The single entry point both delivery modes share: the poll loop
    # calls _spawn_dispatch -> here, and webhook.py calls here directly.
    # Anything that decides what an update MEANS has to happen here, or
    # one of the two modes silently loses it -- which is exactly how the
    # edit-replace block ended up dead in production for years while two
    # comments described it as living right here.
    await _apply_edited_message(upd_dict)

    upd = Update.de_json(upd_dict, app.bot)
    if upd is None:
        log.info(f"[dispatch] de_json returned None for {upd_dict}")
        return
    # === chat_member (new join) handler ===
    # We handle chat_member updates BEFORE PTB's process_update
    # because PTB may not have a handler registered for them, and
    # we want full control of the welcome flow. The bot detects
    # new members joining the group and sends the WELCOME_TEXT
    # (a) in the General topic, and (b) as a PM to the new
    # member. This ensures the rating rules are the first
    # information the new member (especially an LLM-claimed
    # account) receives after joining.
    if getattr(upd, 'chat_member', None) is not None:
        await _handle_chat_member_update(upd, app.bot)
        return
    txt = upd.message.text if (upd.message and upd.message.text) else None
    log.info(f"[dispatch] update_id={upd.update_id} msg={txt!r}")
    # Group-mode noise filter. Apply here (in the dispatch path) so
    # EVERY handler is covered uniformly: message handlers AND
    # command handlers. If a message is from another Telegram bot
    # or contains the "[llm]" convention marker, drop it before
    # any handler runs.
    if upd.message is not None and _should_mute_in_group(upd):
        who = upd.message.from_user
        tag = (
            f"bot @{who.username}" if getattr(who, 'is_bot', False)
            else f"[llm] @{who.username}" if who.username
            else f"id={who.id}"
        )
        log.info(f"[dispatch] muting group-mode message from {tag}")
        return
    # T65.1: reply-to-other-user is ABSORBED, not skipped.
    # Persist + react silently; do not answer, do not show reasoning.
    if upd.message is not None and _is_reply_to_other_user(upd, app.bot.id):
        await _handle_reply_to_other_user(upd, app.bot)
        return
    await app.process_update(upd)
    log.info(f"[dispatch] processed update_id={upd.update_id}")


# === Module-level state that other modules need ===

def get_global_llm_sem_limit() -> int:
    return _GLOBAL_LLM_SEM_LIMIT
