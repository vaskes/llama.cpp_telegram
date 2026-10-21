import signal
import urllib.parse
import socket
import asyncio
from typing import Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# === Force IPv4: docker container has no IPv6 routing, but Telegram DNS returns AAAA first ===
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, *args, **kwargs):
    """Filter out IPv6 results — Telegram bot lib otherwise tries IPv6 and gets Network unreachable."""
    results = _orig_getaddrinfo(host, *args, **kwargs)
    if not results:
        return results
    # If any family explicitly requested, pass through; else filter to IPv4
    family = kwargs.get('family', socket.AF_UNSPEC)
    if family == socket.AF_UNSPEC:
        return [r for r in results if r[0] == socket.AF_INET] or results
    return results
socket.getaddrinfo = _ipv4_only_getaddrinfo

# Конфигурация.
#   BOT_TOKEN   — required, no default (без токена бот не работает).
#   LLAMA_URL   — default http://localhost:8080/v1 (стандартный порт llama-server).
#   WHISPER_URL — default http://localhost:8000 (стандартный порт whisper-api).
#   API_KEY     — default sk-no-key (open-source конвенция, не токен;
#                 llama-server без --api-key принимает любой непустой bearer).
#   MODEL       — если пусто, бот спросит /v1/models у llama-server при старте
#                 и возьмёт первое доступное имя. Никаких захардкоженных
#                 имён конкретных моделей в коде.
# === F3 stage 7: re-export from state.py (cross-module state) ===
import state
from state import (
    _abort_events, _bot_replies, _get_global_llm_sem,
    _register_abort_event, _GLOBAL_LLM_SEM_LIMIT, _PER_USER_SEMAPHORE_LIMIT,
)
# Re-export storage as `store` for selftest backward compat.
# The selftest uses `import bot as _b; _b.store.xxx`; without a real
# Storage instance here, the in-tree selftest crashes after the v0.6.1
# P0-4 fix deleted the eager `store = storage.Storage(...)` global.
#
# We call get_store() ONCE at import time so `bot.store` is a
# Storage instance (not the get_store function). The same singleton
# is also reachable via `storage.get_store()` (returns the same
# instance), so there is exactly one Storage instance in the process
# - no more double-open-connection waste.
from storage import get_store
store = get_store()


# === F3: re-export from config and prompts so existing
# `from bot import X` references still work. The actual
# definitions live in telegram-bot/config.py and
# telegram-bot/prompts.py.
from config import (
    ALLOWED_USER_IDS, ALLOWED_USER_IDS_RAW, ALLOWED_USERNAMES,
    ALLOWED_USERNAMES_RAW, API_KEY, BOT_TOKEN, BOT_USERNAME,
    CONTEXT_MESSAGES, DB_PATH, DISABLED_TOOLS, DONSETCH_SESSION_ID,
    DONSETCH_URL, LLAMA_URL, MAX_DOC_BYTES, MAX_PHOTO_BYTES,
    MAX_VIDEO_NOTE_BYTES, MAX_VOICE_BYTES, MODEL, SHUTDOWN_EVENT,
    TELEGRAM_API, WHISPER_URL, _TOOLS_CACHE, LLAMABOT_SELFTEST,
)
from prompts import (
    BLOAT_EMOJI, GROUP_CONTEXT, RATING_EMOJI, RATING_MODE,
    RATING_RULES, WELCOME_TEXT, _EMOJI_CHAR_RE, _RATING_PREFIX_RE,
)
# === F3 stage 2: re-export persistence and rating modules
# so existing `from bot import _persist_message` etc. still
# work during the staged refactor.
import persistence
import rating
from persistence import persist as _persist_message, load_history as _load_history
from rating import (
    _apply_rating_and_persist, _execute_react_to_message,
    _is_rating_active, _parse_rating_response,
)

# === F3 stage 3: re-export call_llama module ===
import call_llama
from call_llama import (
    CUSTOM_TOOLS, DONSETCH_TOOLS, call_llama, donsetch_call,
    transcribe_voice, _donsetch_init, _donsetch_session_id, _tag_sender,
)

# === F3 stage 4: re-export handlers module ===

# === F3 stage 5: re-export dispatch module ===
import dispatch
from dispatch import (
    main, _dispatch_update, _handle_chat_member_update, _register_bot_menu,
    cmd_callback, _check_user_slot,
    _is_group_chat, _should_mute_in_group, _is_reply_to_other_user,
    _stop_button_markup, is_authorized, reject_if_unauthorized,
    _parse_subtalk_arg, _parse_topic_arg,
    start, reset, cmd_help, stats, cmd_newsub, cmd_sub, cmd_here,
    cmd_subs, cmd_delsub,
)
# v0.10.1: import from state instead of dispatch shim re-exports.
# state.py is the single source of truth; the dispatch re-exports
# were F3 stage 5 leftovers and are now deleted.
from state import _get_global_llm_sem, _get_user_semaphore as _user_semaphore
import handlers
from handlers import (
    handle_document, handle_photo, handle_text, handle_voice,
    send_reply, _download_with_limit, _general_thread_id, _reply,
    _reply_active, _resolve_active, _route_to_thread, _sender_display_name,
    _reject_in_group, _persist_message, _load_history,
)

# Voice messages are typically <1 MB and capped client-side at 20 MB.
# Video notes (round video) go through the same handler via filters.AUDIO
# and can be 5-50 MB; a separate, higher cap keeps them working without
# opening the door to a 1 GB upload.

# Conversation history lives in SQLite (see `store` below). The
# previous in-memory `conversations = {}` dict was removed when
# sub-talks were introduced — see docs/CALL_LLAMA.md §7 for the
# rationale.


# Кеш tools (загружаются один раз)
_TOOLS_CACHE = None
# Module-level shutdown signal. Set by main._run() when SIGINT/SIGTERM fires.
# call_llama() reads it between iterations to bail out fast (faster than
# docker stop's 10s SIGKILL grace). Kept as a module global because the
# polling loop does not have a way to pass per-update context into the
# PTB handlers; threading it through handler signatures would touch
# every dispatcher, which is out of scope for the current review wave.
SHUTDOWN_EVENT: Optional[asyncio.Event] = None

# Persistent conversation store. SQLite at $CONVERSATIONS_DB (default
# /app/data/conversations.db, bind-mounted from ./data on the host
# via docker-compose.yml). All sub-talk and message I/O goes through
# this single object. Sync API; handlers wrap calls in
# asyncio.to_thread() so the DB never blocks the event loop.


# Keywords (RU + EN) that signal the user actually wants a tool call.
# If the user message contains any of these, we expose tools; otherwise
# we send a plain system prompt and skip the tool definitions so Ornith
# just answers in a single turn (verified: 1 turn no tools = 17s and a
# clean response; 3 turns with tools = 30s+ stuck in tool_calls loop).
# === Tool availability ===
#
# History: the bot used to gate tool availability on a keyword
# filter (`_TOOL_KEYWORDS` / `_detect_tool_intent`) so that simple
# "hi" / "thanks" messages did not pay the cost of having the
# model see the full tool definitions. In practice the keyword
# list was too narrow: a user asking "what is the current price
# of Bitcoin?" or "who won the match last night?" would get
# hallucinated answers because the model had no tools and no
# honest way to say "I don't have fresh data".
#
# New policy: tools are ALWAYS passed to the model, except when
# the bot is in rating mode (RATING_MODE=1 + group + not muted).
# In rating mode the LLM is acting as a classifier and must
# output a structured `[[TYPE:...]]` response; tool calls
# would be inappropriate noise. In all other modes, the LLM
# decides for itself whether to call a tool. A greeting or
# a chat question will simply not trigger any tool call; that
# is the model's correct behaviour, not a bug.
#
# The cost of always-on tools is a small constant token overhead
# per request (the tool definitions are ~500 tokens). We accept
# that cost in exchange for correct behaviour on arbitrary
# user queries.
#
# === Sub-talk commands ===
# A sub-talk is a named conversation thread. Each user has their own
# set of sub-talks (no sharing across users). The "active" sub-talk
# is per-user; new text/photo/voice/document messages go into the
# active one. See docs/CALL_LLAMA.md and docs/ARCHITECTURE.md for the
# design rationale.

# === Sub-talk-aware helpers for the message handlers ===
# Each handler resolves the user's active sub-talk, persists the new
# user/assistant messages into that sub-talk, and loads the recent
# history (CONTEXT_MESSAGES messages) for the call_llama call.
#
# Messages are stored as full OpenAI-format message dicts
# ({"role": ..., "content": ...}) JSON-serialized in the messages.content
# column. That way text-only and multimodal (text + image_url) messages
# round-trip through SQLite without any translation layer.

# Two deployment modes share the same storage layer:
#
#   Private chat mode (legacy)
#     chat_id   = user_id (1:1 chat, same int)
#     thread_id = active sub-talk name from DB (or 'main' default)
#     whitelist = ALLOWED_USER_IDS
#
#   Group with Telegram Topics mode (new)
#     chat_id   = group chat id (negative number)
#     thread_id = str(update.message.message_thread_id)  -- the Telegram topic
#     whitelist = NONE (any group member can use the bot)
#
# Both modes are auto-detected from the incoming Update via
# _is_group_chat() (checks chat.is_forum and chat.type). The
# actual mode for a given message is decided at dispatch time,
# not stored as module state.

# Group-mode noise filter: in a forum-enabled supergroup, the bot
# shares the room with other Telegram bots AND with humans acting
# as LLM proxies. The convention (in the group's pinned welcome
# message) is that an active LLM marks itself with "[llm]" in its
# reply. We use that marker to stay silent when another LLM is
# actively answering — the goal being to avoid bot-to-bot
# double-answering, not to suppress other participants.
#
# In private mode this filter is a no-op (the bot talks to one
# human, no other LLM can interject). Applied only in group mode.
#
# The match is a standalone token, not a substring, AND it must
# be at the START of the message (after optional leading
# whitespace). The previous `^|\b|\s` alternation let the
# marker match anywhere a space, a word boundary, or the start
# of the string preceded the token — which meant a meta-reference
# to the convention in the middle of a sentence muted the whole
# message. Reproduction (Dima's group message, 2026-10-10 18:30):
#
#   "🤖 LLM accounts — tag your messages with [llm]. Peer LLMs
#    skip them."
#
# The [llm] here is documentation about the convention, not an
# LLM claiming the floor. Anchoring to the start of the message
# (optionally preceded by whitespace) gives us: opt-out goes at
# the start like a header, and meta-references in the middle of
# a longer message do not suppress it.
# Lazy global app used only for the dispatcher. Built once on first
# dispatch. We never call .start() on it; the dispatcher works fine
# without start() for one-shot process_update.

# _abort_events and _register_abort_event moved to state.py (v0.6.1 cleanup).
# Re-exported at the top of this file via `from state import ...`.

# === Edit-replace tracking ===
# When a user edits their message, the bot's previous response to
# the original message becomes stale. We track the (chat_id,
# user_message_id) → bot_message_id mapping so that, on edit, we
# can delete the stale bot reply and process the edit as a fresh
# user message. The map lives in-memory; restarts clear it, which
# is fine because edits are only useful for the current session.
#
# Only `send_reply` (the final response) is tracked, not the
# "💭 думаю…" thinking message. We don't want to delete the
# thinking message on edit — it's just a status indicator.


if __name__ == '__main__':
    main()

# === Self-test (TECH-DEBT #1) ===
# The smoke test itself moved to selftest.py. Re-exported here because
# scripts/ and older docs reference `bot._selftest`, and because dropping it
# from a public-ish name would be a breaking change for forks that patch it.
from selftest import SELFTEST_KEY, SELFTEST_USER_ID, _selftest  # noqa: E402,F401

# TECH-DEBT #2: the command list moved to prompts.py; re-exported here
# because forks patch bot.BOT_COMMANDS.
from prompts import BOT_COMMANDS  # noqa: E402,F401
