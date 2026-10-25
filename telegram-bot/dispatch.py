# dispatch.py -- routing, and nothing else.
#
# TECH-DEBT #10 blocks D/E/F took this from 974 lines to under 400. What
# is left decides: `_dispatch_update` routes an update,
# `_handle_reply_to_other_user` is the reply-to-someone-else branch,
# `main` picks polling over webhook, the caps bound them.
# The PTB surface moved out -- `dispatch_polling.py` (signals, runtime
# prep, getUpdates loop, task factory, Application registry),
# `dispatch_webhook.py` (Application assembly, handler registration, the
# command menu, chat-member bookkeeping). None of it could pick a handler.

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

import storage
import dispatch_polling
import dispatch_webhook
from context_budget import get_budget
from selftest import _selftest
# TECH-DEBT #10 block D: the polling path moved to `dispatch_polling.py`,
# which also owns the Application registry.
# TECH-DEBT #10 block E: PTB construction and chat-member bookkeeping moved
# to `dispatch_webhook.py`. This module keeps the routing decision.
from dispatch_group import _handle_reply_to_other_user
from dispatch_webhook import (
    _attach_handlers,
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
    _abort_events, _global_llm_sem,
    _get_user_semaphore, _user_semaphores,
)
import handlers

from logconf import get_logger
log = get_logger(__name__)

# === Module-level state (lazy singleton) ===

import persistence
import prompts
import rating
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
        # (The list itself lives in config.ALLOWED_UPDATES.)
        if _config.WEBHOOK_URL and _config.WEBHOOK_SECRET_TOKEN:
            _webhook_auto_register_sync(
                url=_config.WEBHOOK_URL,
                secret_token=_config.WEBHOOK_SECRET_TOKEN,
            )
        # The command menu is registered by the webhook server itself,
        # at startup: the polling path does it in `_prepare_runtime`, and
        # webhook mode returns from `main()` before that point, so the
        # menu Telegram shows was whatever the last run that registered
        # one had set. Passing the registrar in keeps the wiring visible
        # here instead of hiding an import inside the server.
        asyncio.run(_webhook_run(register_bot_menu=_register_bot_menu))
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

# Four section markers stood here naming functions that had already moved:
# application assembly and `_register_bot_menu` / `_handle_chat_member_update`
# to `dispatch_webhook.py`, `_handle_reply_to_other_user` to
# `dispatch_group.py`. They headed empty sections, which is worse than no
# marker -- a reader looking for `_register_bot_menu` in this file found a
# heading and nothing under it.
#
# The fourth one is gone from this list on purpose: the edit-replace path
# it named (`_apply_edited_message`, in `dispatch_edit.py`) was removed with
# the feature itself on 2026-10-24. Naming a module that no longer exists
# is the same rot as naming a line that moved.

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
    # The registry holds only initialized Applications: `dispatch_webhook`
    # publishes after `initialize()` succeeded, and a failed
    # initialization publishes nothing. The second half of that check is
    # here because the property is what the whole bot rests on --
    # `app.bot.id` raises "ExtBot is not properly initialized" on an
    # Application that never finished initializing, and that id is read
    # on every single update.
    if app is None or not dispatch_webhook._is_initialized(app):
        # Double-checked locking: inside the lock another coroutine may
        # already have published an Application, and then that one --
        # the instance that ran initialize() -- is the one to use.
        async with _app_init_lock:
            app = dispatch_polling.get_dispatcher()
            if app is None or not dispatch_webhook._is_initialized(app):
                # One builder, one place that knows the invariant. The
                # Application is built and initialized there, and
                # published only if that worked.
                app = await dispatch_webhook.ensure_application_ready()
                log.info("[dispatch] Application ready")

    # `app` is live from here on: either it came from the registry, or the
    # branch above built and published it. Every read below goes through
    # this local, not through the registry, because re-reading a global
    # five times is how a module ends up with two copies of one truth.
    #
    # The single entry point both delivery modes share: the poll loop
    # calls _spawn_dispatch -> here, and webhook.py calls here directly.
    # Anything that decides what an update MEANS has to happen here, or
    # one of the two modes silently loses it.
    upd = Update.de_json(upd_dict, app.bot)
    if upd is None:
        log.info(f"[dispatch] de_json returned None for {upd_dict}")
        return
    # === an edited message is not a message ===
    #
    # The edit-replace feature was removed on 2026-10-24: an edited
    # message is ignored, and the answer to the original stands. The
    # user waits for the answer to the wrong question and asks again.
    #
    # The subscription is the first line of defence --
    # `config.ALLOWED_UPDATES` does not list `edited_message`, so
    # Telegram does not send edits. It is not the only one, and it
    # cannot be the only one: `allowed_updates` is a setting on
    # Telegram's side, and it survives until the webhook is
    # re-registered. A bot that was restarted against a stale
    # registration would keep receiving edits.
    #
    # The second line is here, and it has to be here, because PTB's
    # `MessageFilter` reads `update.effective_message`, and
    # `effective_message` falls back to `edited_message`. So
    # `filters.TEXT` matches an edited message, and without this guard
    # the bot would answer the edit as if it were a new message --
    # two answers to one question, with the old one still on screen.
    if upd.message is None and upd.edited_message is not None:
        log.info(
            f"[dispatch] ignoring edited_message update_id={upd.update_id} "
            f"(the edit-replace feature was removed on 2026-10-24)")
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
