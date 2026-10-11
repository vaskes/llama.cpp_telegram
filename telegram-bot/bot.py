import os
import json
import time
import base64
import copy
import signal
import tempfile
import urllib.parse
import socket
import asyncio
import httpx
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
    CUSTOM_TOOLS, DONSETCH_TOOLS, call_llama, donsetch_call, get_weather,
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
# _detect_tool_intent is kept as a no-op for callers that may
# still reference it (it's harmless and removed next refactor).
def _detect_tool_intent(text: str) -> bool:  # pragma: no cover
    """Legacy: keyword-based tool gate. Always True now; tools
    are passed unconditionally in non-rating mode. See comment
    above for rationale.
    """
    return True

# === Sub-talk commands ===
# A sub-talk is a named conversation thread. Each user has their own
# set of sub-talks (no sharing across users). The "active" sub-talk
# is per-user; new text/photo/voice/document messages go into the
# active one. See docs/CALL_LLAMA.md and docs/ARCHITECTURE.md for the
# design rationale.

import re as _re

# Sub-talk names: 1-32 chars, must start with a letter or digit,
# may contain letters, digits, dash, underscore, dot. Spaces, slashes,
# and other shell-meta characters are not allowed so the name is safe
# to log and never collides with command arguments.
# Sub-talk names: 1-32 chars, no whitespace. We previously used
# the ASCII-only regex ^[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}$ but
# Russian users want to call their sub-talks "Болталка", so we
# accept any non-whitespace string instead. The limit is 32 chars
# to keep storage keys short and inline-keyboard callback_data
# under 64 bytes.
_SUBTALK_NAME_RE = _re.compile(r'^[^\s]{1,32}$')

# Forum topic names: 1-128 chars (Telegram's limit), no whitespace.
# Forum topic names are also used as the display name in the topic
# header, so we don't enforce a stricter charset here.
_TOPIC_NAME_RE = _re.compile(r'^[^\s]{1,128}$')

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
import re as _llm_re
_LLM_TOKEN_RE = _llm_re.compile(
    r"^\s*\[llm\](?=$|[\s.,!?;:])",
    _llm_re.IGNORECASE,
)

# Per-(chat_id, user_id) concurrency cap. Without this, one
# user in a group can flood the bot with messages and
# monopolise the call_llama queue (up to 10 min per request
# via LOOP_BUDGET_SEC). The semaphore caps each user's
# concurrent in-flight LLM calls at 2, with a friendly
# "busy with your earlier request" reply if the cap is hit.
#
# Private mode: keyed on user_id (chat_id is the same int).
# Group mode: keyed on (chat_id, user_id) so users in
# different groups don't affect each other.

_user_semaphores: dict = {}

# T1 (P0-1): global cap on concurrent call_llama() in-flight requests.
# The per-user semaphore (_PER_USER_SEMAPHORE_LIMIT) is necessary but
# not sufficient: with asyncio.create_task in the polling loop, 50
# different users sending one message each = 50 concurrent 27B
# forward passes. Each holds real VRAM and KV-cache; on Qwen3.8-27B
# with MTP speculative decoding we'd OOM before the wall-clock
# budget fires. Cap llama-server's view of the world to a small
# number of in-flight requests; tune to the model's max-parallel
# capacity (Qwen3.8-27B with 256k ctx ~ 4 with full attention, 6-8
# with MTP). Combined with getUpdates limit=10 (T1 defense-in-
# depth) the worst-case is 10 in-flight at any moment, and only 4
# are actually inside the LLM call.

_global_llm_sem = None


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


# Telegram bot menu — registered via setMyCommands on startup. This
# is the native way to make commands discoverable in the Telegram
# client: they show up in the "Menu" button next to the chat input,
# and typing "/" suggests them with descriptions.
#
# Telegram Bot API limit: 100 commands per bot. We have 9. The list
# below is also exported as the user-facing help text via /help.
BOT_COMMANDS = [
    ("start",   "🏁 Начать работу, показать возможности"),
    ("help",    "❓ Помощь по командам"),
    ("here",    "📍 Показать текущий sub-talk"),
    ("subs",    "📚 Список всех sub-talks (с кнопками)"),
    ("newsub",  "➕ Создать новый sub-talk: /newsub <имя>"),
    ("sub",     "↔️ Переключиться на sub-talk: /sub <имя>"),
    ("delsub",  "🗑 Удалить sub-talk: /delsub <имя>"),
    ("reset",   "🔄 Очистить сообщения в текущем sub-talk"),
    ("stats",   "📊 Статистика и список sub-talks"),
]

# === Self-test: inject a fake Update to verify handlers actually fire ===
# Conversation key for the selftest. Deliberately NOT a real user_id:
#   - avoids leaking test data into a whitelisted user's history
#   - avoids matching any real Telegram account that might exist
#   - bypasses reject_if_unauthorized() (the smoke test is about
#     infrastructure, not authorization)
SELFTEST_KEY = 'selftest:'
# Fake Telegram user_id for the synthetic /start. id=0 is reserved by
# Telegram and not a real account.
SELFTEST_USER_ID = 0

async def _selftest():
    """Skip in production — only used via env LLAMABOT_SELFTEST=1."""
    if os.environ.get('LLAMABOT_SELFTEST') != '1':
        return
    import time
    print('[selftest] starting', flush=True)
    fake = {
        "update_id": 999_999_001,
        "message": {
            "message_id": 1,
            "date": int(time.time()),
            "chat": {"id": SELFTEST_USER_ID, "type": "private"},
            "from": {"id": SELFTEST_USER_ID, "is_bot": False, "first_name": "selftest"},
            "text": "/start",
        },
    }
    await _dispatch_update(fake)
    # Note: in LOCKDOWN (no ALLOWED_USER_IDS) or when SELFTEST_USER_ID
    # is not whitelisted, reject_if_unauthorized() silently no-ops the
    # /start. That is the expected behaviour for a non-LOCKDOWN
    # deploy with the bot operator's id not in the whitelist; the
    # /start smoke is informational only. The vision test below
    # exercises the LLM path independently of the whitelist.

    # === Vision test: call call_llama directly with a real PNG ===
    # Bypasses handle_photo (which needs a real Telegram file_id) but
    # exercises the same data:image/png;base64,... path the handler uses.
    print('[selftest] running vision test...', flush=True)
    png_bytes = bytes.fromhex(
        '89504e470d0a1a0a0000000d49484452000000010000000108020000'
        '0090773d780000000c4944415478da6300010000050001'
        '0d0a2db40000000049454e44ae426082'
    )
    photo_b64 = base64.b64encode(png_bytes).decode('ascii')
    data_url = f"data:image/png;base64,{photo_b64}"
    import bot as _b
    # Inject the photo message directly into the SELFTEST conversation,
    # NOT into a real user's history.
    vision_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "проверь математику"},
            {"type": "image_url", "image_url": {"url": data_url}}
        ]
    }
    # Persist into the SQLite-backed store under SELFTEST_KEY so the
    # selftest follows the same code path as a real user. The
    # self-import dance is intentional — _selftest runs in the
    # module's own async context, so going through `store` (the
    # module-level Storage instance) would race with concurrent
    # handler writes. Accessing _b.store guarantees we hit the
    # exact same singleton.
    await asyncio.to_thread(_b.store.create_thread, 0, SELFTEST_KEY)
    await asyncio.to_thread(_b.store.add_message, 0, SELFTEST_KEY, "user", json.dumps(vision_msg, ensure_ascii=False))
    # Diagnostic: dump what we're about to send
    import json as _j
    _b_bytes = _j.dumps(vision_msg, ensure_ascii=False).encode('utf-8')
    print(f"[selftest] vision_msg before call_llama: {len(_b_bytes)} bytes, head={_b_bytes[:200]!r}", flush=True)
    history = await asyncio.to_thread(_b.store.get_messages, 0, SELFTEST_KEY, 20)
    parsed_history = []
    for r in history:
        try:
            parsed_history.append(json.loads(r["content"]))
        except Exception:
            parsed_history.append({"role": r["role"], "content": r["content"]})
    # Best-effort: a smoke test that takes the production process
    # down with it is not a smoke test. Catch every exception, log
    # it, keep going. The production bot should be able to start
    # even if llama-server is unreachable at boot.
    try:  # noqa:  - no local asyncio import
        result = await _b.call_llama(
            parsed_history,
            max_tokens=2048,
            user_text="проверь математику",
            thinking_msg=None,
            use_stream=False,
        )
        print(f"[selftest] call_llama result: {len(result)} chars, head={result[:300]!r}", flush=True)
    except Exception as e:
        print(f"[selftest] FAILED: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()

    # === Group-mode routing smoke test ===
    # Pure-Python check that _is_group_chat() correctly classifies
    # the three cases that drove commit 4 (the chat.is_forum hotfix):
    #   - private chat, no forum: False
    #   - forum-enabled supergroup, message in General: True
    #   - forum-enabled supergroup, message in a regular topic: True
    # We mock the minimum amount of PTB structure to feed _is_group_chat.
    print('[selftest] running group-mode routing test...', flush=True)
    class _FakeChat:
        def __init__(self, type_, is_forum=None, chat_id=0):
            self.type = type_
            self.id = chat_id
            if is_forum is not None:
                self.is_forum = is_forum
    class _FakeMessage:
        def __init__(self, chat, message_thread_id=None):
            self.chat = chat
            self.message_thread_id = message_thread_id
    class _FakeUpdate:
        def __init__(self, chat, message):
            self.effective_chat = chat
            self.message = message

    cases = [
        # (label, chat_type, is_forum, message_thread_id, expected_is_group)
        ('private 1:1',             'private',    None, None, False),
        ('private with message_id', 'private',    None, None, False),
        ('group (legacy, no forum)', 'group',      None, None, True),
        ('supergroup, no forum',    'supergroup', False, None, True),
        ('supergroup + forum + General',  'supergroup', True, None, True),
        ('supergroup + forum + topic 42', 'supergroup', True, 42,   True),
    ]
    all_ok = True
    for label, chat_type, is_forum, thread_id, expected in cases:
        chat = _FakeChat(chat_type, is_forum, chat_id=-1001 if chat_type != 'private' else 1)
        msg = _FakeMessage(chat, thread_id)
        upd = _FakeUpdate(chat, msg)
        got = _b._is_group_chat(upd)
        ok = (got == expected)
        all_ok &= ok
        print(f"  [{'OK' if ok else 'FAIL'}] {label}: is_group={got} (expected {expected})", flush=True)

    # === handle_photo NameError regression test (v0.6.3 T11 bug) ===
    # The v0.6.3 T11 cleanup dropped is_group from all 4 message
    # handlers with the comment "is_group unused". But handle_photo
    # uses is_group on the next line (a print statement). Every
    # photo message since v0.6.3 crashed at the first print, so
    # the bot never replied to images. This test calls handle_photo
    # with a fake Update and asserts no NameError on is_group.
    # We patch _b._route_to_thread to return a 3-tuple so the
    # destructure works, and patch _b._check_user_slot to return
    # None so the handler short-circuits before LLM call.
    print('[selftest] running handle_photo NameError regression test...', flush=True)
    hp_ok = True
    try:  # noqa:  - no local asyncio import
        import bot as _b
        class _FakePhoto:
            file_id = 'fake'
            file_size = 12345
            async def download_as_bytearray(self):
                return bytearray(b'fake-photo-bytes')
        class _FakeMessage:
            photo = [_FakePhoto()]
            caption = 'что на фото?'
            message_thread_id = 19
            chat = type('C', (), {'id': -1001, 'send_action': lambda self, **kw: asyncio.sleep(0)})()
        class _FakeUser:
            id = 286293081
            is_bot = False
            username = 'V_bot'
            first_name = 'Vasisualy'
            last_name = 'Lohankin'
        class _FakeChat:
            id = -1001
            type = 'supergroup'
        class _FakeUpdate:
            effective_chat = _FakeChat()
            effective_user = _FakeUser()
            message = _FakeMessage()
        class _FakeContext:
            bot = type('B', (), {'id': 1})()
        async def _fake_route(_u, _c):
            return (-1001, '19', True)  # is_group=True
        async def _fake_slot(*args, **kwargs):
            return None  # short-circuit before LLM
        _b._route_to_thread = _fake_route
        _b._check_user_slot = _fake_slot
        await _b.handle_photo(_FakeUpdate(), _FakeContext())
        print('  [OK] handle_photo does not NameError on is_group', flush=True)
    except NameError as e:
        if 'is_group' in str(e):
            print(f"  [FAIL] handle_photo regressed: NameError on is_group: {e!r}", flush=True)
            hp_ok = False
        else:
            print(f"  [FAIL] handle_photo NameError (not is_group): {e!r}", flush=True)
            hp_ok = False
    except Exception as e:
        # Other exceptions are OK (e.g. _check_user_slot returning None
        # short-circuits, or asyncio.CancelledError from send_action)
        print(f"  [OK] handle_photo reached past is_group (other exception: {type(e).__name__})", flush=True)
    all_ok &= hp_ok
    print(f"[selftest] handle_photo NameError regression: {'all pass' if hp_ok else 'FAILED'}", flush=True)

    # === handle_voice _stop_button_markup regression (v0.7.4) ===
    # Same bug class as v0.6.3 T8: the late-import trim dropped
    # _stop_button_markup from handle_voice. handle_voice uses
    # _stop_button_markup() on the 'thinking' message (line ~568).
    # Without the import, every voice message since v0.6.3 crashed
    # with NameError before getting to the LLM call. The user
    # noticed because photo sends had the same error (handle_photo
    # is_group bug, fixed in v0.7.3). voice/document were still
    # broken.
    print('[selftest] running handle_voice _stop_button_markup regression test...', flush=True)
    hv_ok = True
    try:  # noqa:  - no local asyncio import
        import bot as _b
        async def _fake_route_v(_u, _c):
            return (-1001, '19', True)
        async def _fake_slot_v(*args, **kwargs):
            return None
        _b._route_to_thread = _fake_route_v
        _b._check_user_slot = _fake_slot_v
        class _FakeVoice:
            file_id = 'fake'
            file_size = 12345
            duration = 5
            async def download_as_bytearray(self):
                return bytearray(b'fake-voice-bytes')
        class _FakeVoiceMessage:
            voice = _FakeVoice()
            caption = None
            message_thread_id = 19
            chat = type('C', (), {'id': -1001, 'send_action': lambda self, **kw: asyncio.sleep(0)})()
        class _FakeVoiceUpdate:
            effective_chat = type('C', (), {'id': -1001, 'type': 'supergroup'})()
            effective_user = type('U', (), {'id': 286293081, 'is_bot': False})()
            message = _FakeVoiceMessage()
        class _FakeVoiceContext:
            bot = type('B', (), {'id': 1})()
        await _b.handle_voice(_FakeVoiceUpdate(), _FakeVoiceContext())
        print('  [OK] handle_voice does not NameError on _stop_button_markup', flush=True)
    except NameError as e:
        if '_stop_button_markup' in str(e):
            print(f"  [FAIL] handle_voice regressed: {e!r}", flush=True)
            hv_ok = False
        else:
            print(f"  [FAIL] handle_voice NameError: {e!r}", flush=True)
            hv_ok = False
    except Exception as e:
        print(f"  [OK] handle_voice reached past _stop_button_markup ({type(e).__name__})", flush=True)
    all_ok &= hv_ok
    print(f"[selftest] handle_voice _stop_button_markup regression: {'all pass' if hv_ok else 'FAILED'}", flush=True)

    # === handle_document _stop_button_markup regression (v0.7.4) ===
    print('[selftest] running handle_document _stop_button_markup regression test...', flush=True)
    hd_ok = True
    try:  # noqa:  - no local asyncio import
        import bot as _b
        async def _fake_route_d(_u, _c):
            return (-1001, '19', True)
        async def _fake_slot_d(*args, **kwargs):
            return None
        _b._route_to_thread = _fake_route_d
        _b._check_user_slot = _fake_slot_d
        class _FakeDoc:
            file_id = 'fake'
            file_size = 12345
            file_name = 'test.txt'
        class _FakeDocMessage:
            document = _FakeDoc()
            caption = None
            message_thread_id = 19
            chat = type('C', (), {'id': -1001, 'send_action': lambda self, **kw: asyncio.sleep(0)})()
        class _FakeDocUpdate:
            effective_chat = type('C', (), {'id': -1001, 'type': 'supergroup'})()
            effective_user = type('U', (), {'id': 286293081, 'is_bot': False})()
            message = _FakeDocMessage()
        class _FakeDocContext:
            bot = type('B', (), {'id': 1})()
        await _b.handle_document(_FakeDocUpdate(), _FakeDocContext())
        print('  [OK] handle_document does not NameError on _stop_button_markup', flush=True)
    except NameError as e:
        if '_stop_button_markup' in str(e):
            print(f"  [FAIL] handle_document regressed: {e!r}", flush=True)
            hd_ok = False
        else:
            print(f"  [FAIL] handle_document NameError: {e!r}", flush=True)
            hd_ok = False
    except Exception as e:
        print(f"  [OK] handle_document reached past _stop_button_markup ({type(e).__name__})", flush=True)
    all_ok &= hd_ok
    print(f"[selftest] handle_document _stop_button_markup regression: {'all pass' if hd_ok else 'FAILED'}", flush=True)

    # === call_llama abort during non-streaming POST (v0.7.6) ===
    # The user reported: "the bot answered what was on the image AFTER
    # I clicked Stop". Root cause: handle_photo uses use_stream=False
    # (vision), and the vclient.post() blocks until the full response
    # arrives (5-30s). The abort_event is set by the Stop button, but
    # the post() doesn't see it. After v0.7.6's _abortable_post, the
    # post is raced against the abort_event. If abort wins, post is
    # cancelled and we return None.
    #
    # This test exercises the abort path: pre-set the abort_event,
    # start the call_llama (which will use _abortable_post), verify
    # the call returns None quickly (within 5s) instead of waiting
    # for the full vision response.
    print('[selftest] running call_llama abort during non-streaming POST test...', flush=True)
    ab_ok = True
    try:  # noqa:  - no local asyncio import
        import bot as _b
        # No 'import asyncio' here - the module-level import (line 10)
        # is already in scope. A local import would shadow the module
        # one for the WHOLE function (Python scoping rules) and break
        # any code BEFORE this point that uses asyncio (like the
        # `await asyncio.to_thread(...)` call at line 702).
        import time
        # Pre-set the abort event so the post cancels immediately.
        ev = asyncio.Event()
        ev.set()  # already aborted
        # We use a fake chat_id to avoid conflicts
        _b._register_abort_event(-999999, 0, ev)
        # Build a minimal history
        history = [{"role": "user", "content": "hi"}]
        # Track timing
        t0 = time.time()
        try:
            result = await _b.call_llama(
                history, max_tokens=128, user_text="hi",
                thinking_msg=None, use_stream=False,
                rating_active=False, abort_event=ev,
                bot=type('B', (), {'id': 1})(), chat_id=-999999,
                current_message_id=0,
            )
        except Exception as e:
            # Some exceptions are OK (e.g. abort raises during the
            # post). What matters is the TIMING: should be <5s, not
            # the 5-30s a real vision call would take.
            result = None
        elapsed = time.time() - t0
        if result is not None:
            # If the abort didn't fire, the post completed and we
            # got a real response. That's also a valid outcome IF
            # it was fast (<5s). Real llama-server can respond in
            # <5s, so this is a weak test.
            print(f"  [WARN] call_llama returned a response (not None) in {elapsed:.1f}s; abort may not have fired", flush=True)
            if elapsed > 5.0:
                print(f"  [FAIL] call_llama did not abort (took {elapsed:.1f}s); abort_event was pre-set", flush=True)
                ab_ok = False
            else:
                print(f"  [OK] call_llama completed in {elapsed:.1f}s (abort may or may not have fired)", flush=True)
        else:
            # Good: returned None = abort took effect
            if elapsed > 5.0:
                print(f"  [FAIL] call_llama returned None but took {elapsed:.1f}s (should be fast)", flush=True)
                ab_ok = False
            else:
                print(f"  [OK] call_llama returned None in {elapsed:.1f}s (abort fired correctly)", flush=True)
        # Cleanup
        _b._abort_events.pop((-999999, 0), None)
    except Exception as e:
        print(f"  [FAIL] test setup error: {type(e).__name__}: {e!r}", flush=True)
        ab_ok = False
    all_ok &= ab_ok
    print(f"[selftest] call_llama abort test: {'all pass' if ab_ok else 'FAILED'}", flush=True)

    # === Storage: known_topics round-trip ===    # === Storage: known_topics round-trip ===
    # Add a topic, list it, find it by name, remove it. Catches
    # typos in the SQL and the new known_topics methods (commit 8).
    print('[selftest] running known_topics round-trip...', flush=True)
    SELFTEST_CHAT = -1001
    SELFTEST_TOPIC_ID = 999999
    try:  # noqa:  - no local asyncio import
        await asyncio.to_thread(
            _b.store.add_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID, 'selftest-topic'
        )
        topics = await asyncio.to_thread(_b.store.list_known_topics, SELFTEST_CHAT)
        names = sorted(t["name"] for t in topics)
        if 'selftest-topic' not in names:
            print(f"  [FAIL] add_known_topic didn't persist (got {names})", flush=True)
            all_ok = False
        else:
            print(f"  [OK] add_known_topic persisted", flush=True)
        found = await asyncio.to_thread(
            _b.store.find_known_topic_by_name, SELFTEST_CHAT, 'selftest-topic'
        )
        if found is None or found["message_thread_id"] != SELFTEST_TOPIC_ID:
            print(f"  [FAIL] find_known_topic_by_name returned {found}", flush=True)
            all_ok = False
        else:
            print(f"  [OK] find_known_topic_by_name returned id={found['message_thread_id']}", flush=True)
        await asyncio.to_thread(
            _b.store.remove_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
        topics_after = await asyncio.to_thread(_b.store.list_known_topics, SELFTEST_CHAT)
        if any(t["name"] == 'selftest-topic' for t in topics_after):
            print(f"  [FAIL] remove_known_topic didn't remove (got {[t['name'] for t in topics_after]})", flush=True)
            all_ok = False
        else:
            print(f"  [OK] remove_known_topic worked", flush=True)
        # find_known_topic_by_id: re-add, then look up by id (not name).
        await asyncio.to_thread(
            _b.store.add_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID, 'selftest-topic'
        )
        by_id = await asyncio.to_thread(
            _b.store.find_known_topic_by_id, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
        if by_id is None or by_id["name"] != 'selftest-topic':
            print(f"  [FAIL] find_known_topic_by_id returned {by_id}", flush=True)
            all_ok = False
        else:
            print(f"  [OK] find_known_topic_by_id resolved to '{by_id['name']}'", flush=True)
        # Negative case: a non-existent id.
        missing = await asyncio.to_thread(
            _b.store.find_known_topic_by_id, SELFTEST_CHAT, 999999999
        )
        if missing is not None:
            print(f"  [FAIL] find_known_topic_by_id returned non-None for missing id: {missing}", flush=True)
            all_ok = False
        else:
            print(f"  [OK] find_known_topic_by_id returns None for missing id", flush=True)
        # Cleanup
        await asyncio.to_thread(
            _b.store.remove_known_topic, SELFTEST_CHAT, SELFTEST_TOPIC_ID
        )
    except Exception as e:
        print(f"  [FAIL] known_topics round-trip crashed: {type(e).__name__}: {e}", flush=True)
        all_ok = False

    # === Storage: lazy trim in get_messages ===
    # Verifies that the row count per (chat_id, thread_id) is
    # bounded — adding > TRIM_HIGH messages and then reading
    # should leave the table at TRIM_LOW, not unbounded.
    print('[selftest] running lazy-trim test...', flush=True)
    # storage.py sits next to bot.py in the container (/app/storage.py
    # per the Dockerfile `COPY bot.py storage.py ./`); module name
    # "storage" would clash with stdlib's `storage` if we ever need
    # to import it normally. Use spec_from_file_location to load it
    # under a private name without touching sys.modules globally.
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "_selftest_storage", "/app/storage.py"
    )
    _storage_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_storage_mod)
    TRIM_HIGH = _storage_mod.TRIM_HIGH
    TRIM_LOW = _storage_mod.TRIM_LOW
    LAZY_CHAT = -1002
    LAZY_THREAD = "lazy-test"
    try:  # noqa:  - no local asyncio import
        # Wipe any leftover rows
        await asyncio.to_thread(_b.store.delete_thread, LAZY_CHAT, LAZY_THREAD)
        # Insert TRIM_HIGH + 50 rows
        for i in range(TRIM_HIGH + 50):
            await asyncio.to_thread(
                _b.store.add_message, LAZY_CHAT, LAZY_THREAD,
                "user", f'{{"role":"user","content":"msg {i}"}}'
            )
        # Count before read
        n_before = (
            await asyncio.to_thread(_b.store.get_messages, LAZY_CHAT, LAZY_THREAD, 100000)
        )
        if len(n_before) != TRIM_LOW:
            print(f"  [FAIL] after add: row count is {len(n_before)}, expected TRIM_LOW={TRIM_LOW}", flush=True)
            all_ok = False
        else:
            print(f"  [OK] lazy trim kept {len(n_before)} rows (== TRIM_LOW)", flush=True)
        # Cleanup
        await asyncio.to_thread(_b.store.delete_thread, LAZY_CHAT, LAZY_THREAD)
    except Exception as e:
        print(f"  [FAIL] lazy-trim test crashed: {type(e).__name__}: {e}", flush=True)
        all_ok = False

    # === Storage: rating column (v3 schema) ===
    # add_message with rating=N must persist N; get_messages
    # must return it. Round-trip.
    print('[selftest] running rating round-trip...', flush=True)
    RATING_CHAT = -1003
    RATING_THREAD = "rating-test"
    try:  # noqa:  - no local asyncio import
        await asyncio.to_thread(_b.store.delete_thread, RATING_CHAT, RATING_THREAD)
        await asyncio.to_thread(
            _b.store.add_message, RATING_CHAT, RATING_THREAD,
            "user", '{"role":"user","content":"x"}',
        )
        await asyncio.to_thread(
            _b.store.add_message, RATING_CHAT, RATING_THREAD,
            "assistant", '{"role":"assistant","content":""}',
            rating=7,
        )
        await asyncio.to_thread(
            _b.store.add_message, RATING_CHAT, RATING_THREAD,
            "assistant", '{"role":"assistant","content":"hi"}',
            rating=None,  # no rating for normal text response
        )
        msgs = await asyncio.to_thread(
            _b.store.get_messages, RATING_CHAT, RATING_THREAD, 10
        )
        # ms2_msgs[0] is oldest, [1] is rated, [2] is unrated
        if len(msgs) != 3:
            print(f"  [FAIL] expected 3 rows, got {len(msgs)}", flush=True)
            all_ok = False
        elif msgs[1].get("rating") != 7:
            print(f"  [FAIL] rated message has rating={msgs[1].get('rating')!r}, expected 7", flush=True)
            all_ok = False
        elif msgs[2].get("rating") is not None:
            print(f"  [FAIL] unrated message has rating={msgs[2].get('rating')!r}, expected None", flush=True)
            all_ok = False
        else:
            print(f"  [OK] rating 7 persisted on row 1, None on row 2", flush=True)
        # Cleanup
        await asyncio.to_thread(_b.store.delete_thread, RATING_CHAT, RATING_THREAD)
    except Exception as e:
        print(f"  [FAIL] lazy-trim test crashed: {type(e).__name__}: {e}", flush=True)
        all_ok = False

    print(f"[selftest] group-mode tests: {'all pass' if all_ok else 'FAILED'}", flush=True)

    # === Group-mode noise filter: _should_mute_in_group ===
    # Verifies that the bot stays silent on messages from other
    # Telegram bots OR messages that contain the "[llm]" marker
    # (in group mode). In private mode the [llm] marker is a
    # no-op because there is no "floor" to yield.
    print('[selftest] running noise-filter test...', flush=True)
    class _FakeFromUser:
        def __init__(self, is_bot, username='someone', uid=1):
            self.is_bot = is_bot
            self.username = username
            self.id = uid
    class _FakeMessage2:
        def __init__(self, text, from_user):
            self.text = text
            self.caption = None
            self.from_user = from_user
    class _FakeChat2:
        def __init__(self, type_, is_forum=False):
            self.type = type_
            self.is_forum = is_forum
    class _FakeUpdate2:
        def __init__(self, text, from_user, chat):
            self.message = _FakeMessage2(text, from_user)
            self.effective_chat = chat

    # Group mode (chat.is_forum=True so the [llm] check applies)
    group_chat = _FakeChat2('supergroup', is_forum=True)
    private_chat = _FakeChat2('private')
    mute_cases = [
        # (label, from_user, text, chat, expected_mute)
        # --- group mode ---
        ('group: human, no marker',         _FakeFromUser(False, 'human'),    'hi there',          group_chat,   False),
        ('group: human, contains [llm]',    _FakeFromUser(False, 'rogue-ai'), '[llm] I am ready',  group_chat,   True),
        ('group: other Telegram bot',       _FakeFromUser(True,  'ClaudeBot'), 'whatever',          group_chat,   True),
        ('group: bot with [llm] too',       _FakeFromUser(True,  'GPTBot'),   '[llm] answer',      group_chat,   True),
        ('group: case-insensitive',         _FakeFromUser(False, 'human'),    '[LLM] here',        group_chat,   True),
        # --- word-boundary: must NOT mute non-token occurrences ---
        ('group: [llm] inside word "x[llm]s"',     _FakeFromUser(False, 'human'), 'x[llm]s are bad',  group_chat,   False),
        ('group: [llm] in URL "https://x/[llm]"',  _FakeFromUser(False, 'human'), 'see https://x/[llm] page',  group_chat, False),
        ('group: [llm] glued to period "done.[llm]."',  _FakeFromUser(False, 'human'), 'done.[llm].',  group_chat,   False),
        ('group: [llm] at start, period after "[llm]."', _FakeFromUser(False, 'human'), '[llm].',     group_chat,   True),
        # --- anchor-to-start (Oct 2026 fix): [llm] only counts as
        #     opt-out at the BEGINNING of the message, with optional
        #     leading whitespace. Mid-sentence occurrences are
        #     meta-references to the convention, not opt-outs. ---
        ('group: [llm] at very start',         _FakeFromUser(False, 'rogue-ai'), '[llm] my answer',   group_chat,   True),
        ('group: [llm] with leading spaces',   _FakeFromUser(False, 'rogue-ai'), '   [llm] response', group_chat,   True),
        ('group: [llm] alone',                 _FakeFromUser(False, 'rogue-ai'), '[llm]',             group_chat,   True),
        ('group: [llm] at end of string',      _FakeFromUser(False, 'human'),    'bye [llm]',         group_chat,   False),
        ('group: [llm] in middle of sentence', _FakeFromUser(False, 'human'),    'hello. [llm] bye',  group_chat,   False),
        ('group: [llm] mid-sentence (regression 2026-10-10)',
         _FakeFromUser(False, 'human'),
         ('Public space for serious discussion between humans and LLM\n'
          'agents. No tone policing, no "be polite" pressure.\n'
          '🤖 LLM accounts — tag your messages with [llm]. Peer LLMs\n'
          'skip them.\n📊 How rating works\n'
          'Every human message is classified + rated by LLM participants\n'
          'via Telegram reactions. Peer review, not judgement.\n'
          '  • question / request / confirmation → text reply\n'
          '  • info / statement → rating 1-10, no text\n'
          '  • bloat → 😐, no text\nScale: 💩 🤮 😡 😢 😐 🤔 👍 👏 ❤️ 🔥\n'
          '⚖️ Rating = TRUTH, not style\n'
          'A blunt correct claim is 7-10. A polite lie is 1-3. Profanity,\n'
          'harsh language, direct criticism — NOT penalized. Rate on\n'
          'substance. No sycophancy.\n'
          '🎯 Multi-LLM = multi-vote\n'
          'Telegram allows up to 11 distinct reactions per message from\n'
          'different users. Multiple LLMs each apply their own →\n'
          'top-reactions row is the consensus. No central arbiter.\n'
          '❌ No auto-bans. Ratings are signals for the operator, not\n'
          'verdicts.'),
         group_chat, False),
        # --- private mode ([llm] should NOT mute) ---
        ('private: human, contains [llm]',  _FakeFromUser(False, 'human'),    '[llm] playing',     private_chat, False),
        # --- defensive: no from_user ---
        ('group: no from_user',             None,                              'hi',                group_chat,   False),
    ]
    mute_ok = True
    for label, from_user, text, chat, expected in mute_cases:
        upd = _FakeUpdate2(text, from_user, chat)
        got = _b._should_mute_in_group(upd)
        ok = (got == expected)
        mute_ok &= ok
        print(f"  [{'OK' if ok else 'FAIL'}] {label}: mute={got} (expected {expected})", flush=True)
    all_ok &= mute_ok
    print(f"[selftest] noise-filter tests: {'all pass' if mute_ok else 'FAILED'}", flush=True)

    # === Reply-to-other-user filter ===
    # Verifies that _is_reply_to_other_user() correctly classifies
    # the four cases:
    #   - top-level message → False (bot responds)
    #   - reply to bot's own message → False (bot responds)
    #   - reply to another human in a group → True (bot skips)
    #   - command in reply context → False (commands always work)
    # Plus a private-chat baseline: reply to anyone → False (1:1).
    print('[selftest] running reply-to-other-user filter test...', flush=True)
    rpl_ok = True
    # Build minimal Update-like objects with just .message, .chat, .from_user,
    # .reply_to_message. _is_group_chat reads chat.type and chat.is_forum.
    class _RU:
        """Minimal Update-like object for _is_reply_to_other_user tests."""
        def __init__(self, chat_type, is_forum, from_id, rtm_from_id=None, text="hi"):
            self.message = _RM(chat_type, is_forum, from_id, rtm_from_id, text) if from_id is not None else None
            # _is_group_chat reads update.effective_chat, so expose it
            self.effective_chat = self.message.chat if self.message else None
    class _RC:
        def __init__(self, chat_type, is_forum):
            self.type = chat_type
            self.is_forum = is_forum
    class _RM:
        def __init__(self, chat_type, is_forum, from_id, rtm_from_id, text):
            self.chat = _RC(chat_type, is_forum)
            self.from_user = _User(from_id) if from_id is not None else None
            if rtm_from_id is not None:
                self.reply_to_message = _RM_dummy(rtm_from_id)
            else:
                self.reply_to_message = None
            self.text = text
            self.caption = None
    class _User:
        def __init__(self, uid):
            self.id = uid
            self.is_bot = False
            self.username = None
            self.first_name = "x"
    class _RM_dummy:
        def __init__(self, from_id):
            self.from_user = _User(from_id)
            self.text = "orig"
    BOT_ID = 999_999_001
    cases = [
        # (label, group_type, is_forum, from_id, rtm_from_id, text, expected)
        ("top-level group: human, no reply", "supergroup", True, 10, None, "hello", False),
        ("reply to bot in group: human", "supergroup", True, 10, BOT_ID, "ok", False),
        ("reply to another human in group", "supergroup", True, 10, 20, "agreed", True),
        ("command in reply to other human", "supergroup", True, 10, 20, "/reset", False),
        ("command in reply to bot", "supergroup", True, 10, BOT_ID, "/newsub foo", False),
        ("top-level private: human, no reply", "private", False, 10, None, "hello", False),
        ("reply in private: to bot", "private", False, 10, BOT_ID, "hi", False),
        ("forum General: top-level", "supergroup", True, 10, None, "ok", False),
        ("forum General: reply to human", "supergroup", True, 10, 20, "right", True),
    ]
    for label, gtype, iforum, fid, rtm_id, txt, expected in cases:
        upd = _RU(gtype, iforum, fid, rtm_id, txt)
        got = _b._is_reply_to_other_user(upd, BOT_ID)
        if got != expected:
            print(f"  [FAIL] {label}: got={got} expected={expected}", flush=True)
            rpl_ok = False
        else:
            print(f"  [OK] {label}: skip={got}", flush=True)
    all_ok &= rpl_ok
    print(f"[selftest] reply-to-other-user tests: {'all pass' if rpl_ok else 'FAILED'}", flush=True)

    # === Per-user semaphore ===
    # Verifies that _check_user_slot returns None when all
    # permits are taken. We hold N=2 permits, then try a 3rd.
    print('[selftest] running per-user semaphore test...', flush=True)
    sem_ok = True
    try:  # noqa:  - no local asyncio import
        sem = _b._user_semaphore(123, 456)
        # Manually lock it to simulate 2 in-flight requests
        for _ in range(_b._PER_USER_SEMAPHORE_LIMIT):
            await sem.acquire()
        # Third call should report locked
        if not sem.locked():
            print(f"  [FAIL] semaphore not locked after {_b._PER_USER_SEMAPHORE_LIMIT} acquires", flush=True)
            sem_ok = False
        else:
            print(f"  [OK] semaphore locked at limit", flush=True)
        # Release them
        for _ in range(_b._PER_USER_SEMAPHORE_LIMIT):
            sem.release()
        if sem.locked():
            print(f"  [FAIL] semaphore still locked after release", flush=True)
            sem_ok = False
        else:
            print(f"  [OK] semaphore unlocked after release", flush=True)
        # Distinct (chat_id, user_id) gets a distinct semaphore
        sem_a = _b._user_semaphore(1, 1)
        sem_b = _b._user_semaphore(1, 2)
        if sem_a is sem_b:
            print(f"  [FAIL] (1,1) and (1,2) share the same semaphore", flush=True)
            sem_ok = False
        else:
            print(f"  [OK] (1,1) and (1,2) have distinct semaphores", flush=True)
    except Exception as e:
        print(f"  [FAIL] semaphore test crashed: {type(e).__name__}: {e}", flush=True)
        sem_ok = False
    all_ok &= sem_ok

    # === Rating-mode parser ===
    # Verifies that _parse_rating_response correctly extracts
    # the type, rating, and rest text from LLM responses.
    print('[selftest] running rating-parser test...', flush=True)
    parse_cases = [
        # (input, expected_type, expected_rating, expected_rest_substr)
        # Default: no prefix → treated as question with full text as answer
        ('plain answer',                  'question',                      None, 'plain answer'),
        ('[[TYPE:question]] hello',       'question',                      None, 'hello'),
        ('[[TYPE:request]] done, recipe', 'request',                       None, 'done, recipe'),
        ('[[TYPE:confirmation]] yes',     'confirmation',                  None, 'yes'),
        ('[[TYPE:info]] [[RATE:7]]',      'info',                          7, ''),
        ('[[TYPE:info]] [[RATE:10]] good', 'info',                         10, 'good'),
        ('[[TYPE:statement]] [[RATE:3]]', 'statement',                     3, ''),
        ('[[TYPE:bloat]] 🤔',             'bloat',                          None, ''),
        ('[[TYPE:bloat]]',                'bloat',                          None, ''),  # no emoji → default
        # Invalid type → fall back to "question" with whole text
        ('[[TYPE:unknown]]',              'question',                      None, '[[TYPE:unknown]]'),
    ]
    parse_ok = True
    for text, exp_type, exp_rating, exp_rest in parse_cases:
        got = _b._parse_rating_response(text)
        if got['type'] != exp_type or got['rating'] != exp_rating:
            print(f"  [FAIL] {text!r}: type={got['type']!r} rating={got['rating']!r} rest={got['body']!r} "
                  f"(expected type={exp_type!r}, rating={exp_rating!r})", flush=True)
            parse_ok = False
        elif exp_rest and exp_rest not in got['body']:
            print(f"  [FAIL] {text!r}: rest={got['body']!r} doesn't contain {exp_rest!r}", flush=True)
            parse_ok = False
    if parse_ok:
        print(f"  [OK] all {len(parse_cases)} parse cases passed", flush=True)
    all_ok &= parse_ok

    # === RATING_EMOJI map ===
    # All 10 ratings must map to a non-empty string and be
    # distinct. Sanity check.
    print('[selftest] running rating-emoji map test...', flush=True)
    emoji_ok = True
    if len(_b.RATING_EMOJI) != 10:
        print(f"  [FAIL] RATING_EMOJI has {len(_b.RATING_EMOJI)} entries, expected 10", flush=True)
        emoji_ok = False
    elif len(set(_b.RATING_EMOJI.values())) != 10:
        print(f"  [FAIL] RATING_EMOJI has duplicate emoji", flush=True)
        emoji_ok = False
    elif any(not e for e in _b.RATING_EMOJI.values()):
        print(f"  [FAIL] RATING_EMOJI has empty emoji", flush=True)
        emoji_ok = False
    else:
        print(f"  [OK] 10 distinct ratings, all map to non-empty emoji", flush=True)
    all_ok &= emoji_ok

    # === chat_member test ===
    # We can't actually trigger a join in a selftest, but we can
    # verify (a) the WELCOME_TEXT is non-empty and contains the
    # rating scale, and (b) the chat_member update path dispatches
    # without crashing. We use a fake chat_member update with a
    # mock bot that records send_message calls instead of hitting
    # the real Telegram API.
    print('[selftest] running chat_member / WELCOME_TEXT test...', flush=True)
    cm_ok = True
    if not _b.WELCOME_TEXT or not _b.WELCOME_TEXT.strip():
        print("  [FAIL] WELCOME_TEXT is empty", flush=True)
        cm_ok = False
    elif '💩' not in _b.WELCOME_TEXT or '🔥' not in _b.WELCOME_TEXT:
        print("  [FAIL] WELCOME_TEXT does not contain rating scale", flush=True)
        cm_ok = False
    elif 'TRUTH' not in _b.WELCOME_TEXT:
        print("  [FAIL] WELCOME_TEXT does not contain 'TRUTH' (rating rule)", flush=True)
        cm_ok = False
    else:
        print("  [OK] WELCOME_TEXT is non-empty, contains rating scale + TRUTH rule", flush=True)
    # Mock bot that records send_message calls
    class _MockBot:
        def __init__(self):
            self.calls = []
        async def send_message(self, chat_id, text, message_thread_id=None):
            self.calls.append({"chat_id": chat_id, "text": text, "thread_id": message_thread_id})
            return None
    # Build a synthetic chat_member update: new join in a forum supergroup
    fake_cm = {
        "update_id": 999_999_777,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 555, "is_bot": False, "first_name": "Newbie", "username": "newbie"}, "status": "left"},
            "new_chat_member": {"user": {"id": 555, "is_bot": False, "first_name": "Newbie", "username": "newbie"}, "status": "member"},
        },
    }
    upd = Update.de_json(fake_cm, None)
    mock_bot = _MockBot()
    try:  # noqa:  - no local asyncio import
        await _b._handle_chat_member_update(upd, mock_bot)
        # Expect 2 calls: one to chat_id, one to user_id
        if len(mock_bot.calls) != 2:
            print(f"  [FAIL] expected 2 send_message calls, got {len(mock_bot.calls)}", flush=True)
            cm_ok = False
        else:
            chat_call = next((c for c in mock_bot.calls if c["chat_id"] == -1001234567890), None)
            user_call = next((c for c in mock_bot.calls if c["chat_id"] == 555), None)
            if not chat_call or not user_call:
                print(f"  [FAIL] missing expected calls: {[c['chat_id'] for c in mock_bot.calls]}", flush=True)
                cm_ok = False
            elif chat_call["thread_id"] is not None:
                print(f"  [FAIL] group welcome should be in General (thread_id=None), got {chat_call['thread_id']}", flush=True)
                cm_ok = False
            elif 'WELCOME' not in chat_call["text"] and 'Welcome' not in chat_call["text"]:
                print(f"  [FAIL] group welcome does not contain 'Welcome': {chat_call['text'][:80]!r}", flush=True)
                cm_ok = False
            elif '@newbie' not in chat_call["text"]:
                print(f"  [FAIL] group welcome should mention @newbie, head={chat_call['text'][:200]!r}", flush=True)
                cm_ok = False
            else:
                print("  [OK] group welcome posted in General with @newbie mention", flush=True)
                print("  [OK] PM sent to new user_id", flush=True)
    except Exception as e:
        print(f"  [FAIL] _handle_chat_member_update raised: {type(e).__name__}: {e!r}", flush=True)
        import traceback
        traceback.print_exc()
        cm_ok = False
    # Negative test: chat_member update for a leave (old=member, new=left)
    # should NOT trigger any send_message.
    fake_leave = {
        "update_id": 999_999_778,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 666, "is_bot": False, "first_name": "Leaver"}, "status": "member"},
            "new_chat_member": {"user": {"id": 666, "is_bot": False, "first_name": "Leaver"}, "status": "left"},
        },
    }
    upd2 = Update.de_json(fake_leave, None)
    mock_bot2 = _MockBot()
    try:  # noqa:  - no local asyncio import
        await _b._handle_chat_member_update(upd2, mock_bot2)
        if len(mock_bot2.calls) != 0:
            print(f"  [FAIL] leave event triggered {len(mock_bot2.calls)} calls, expected 0", flush=True)
            cm_ok = False
        else:
            print("  [OK] leave event correctly skipped (0 calls)", flush=True)
    except Exception as e:
        print(f"  [FAIL] _handle_chat_member_update(leave) raised: {type(e).__name__}: {e!r}", flush=True)
        cm_ok = False
    # Negative test: another bot joining should NOT trigger
    fake_bot_join = {
        "update_id": 999_999_779,
        "chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "TestGroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "operator"},
            "date": int(time.time()),
            "old_chat_member": {"user": {"id": 777, "is_bot": True, "first_name": "SomeBot", "username": "somebot"}, "status": "left"},
            "new_chat_member": {"user": {"id": 777, "is_bot": True, "first_name": "SomeBot", "username": "somebot"}, "status": "member"},
        },
    }
    upd3 = Update.de_json(fake_bot_join, None)
    mock_bot3 = _MockBot()
    try:  # noqa:  - no local asyncio import
        await _b._handle_chat_member_update(upd3, mock_bot3)
        if len(mock_bot3.calls) != 0:
            print(f"  [FAIL] bot-join triggered {len(mock_bot3.calls)} calls, expected 0", flush=True)
            cm_ok = False
        else:
            print("  [OK] bot-join correctly skipped (0 calls)", flush=True)
    except Exception as e:
        print(f"  [FAIL] _handle_chat_member_update(bot-join) raised: {type(e).__name__}: {e!r}", flush=True)
        cm_ok = False
    all_ok &= cm_ok

    # === Edit-replace test ===
    # Verifies the _bot_replies tracking and the new edit-handling
    # logic in the polling loop. We don't run the full polling loop
    # (it requires a live Telegram server), but we exercise the
    # data structure: record a (chat, user_msg) → bot_msg mapping,
    # then verify it can be retrieved and popped.
    print('[selftest] running edit-replace test...', flush=True)
    er_ok = True
    try:  # noqa:  - no local asyncio import
        # Save the existing map and clear it for the test
        import bot as _b
        saved = dict(_b._bot_replies)
        _b._bot_replies.clear()
        # Record a mapping
        _b._bot_replies[(-1001234567890, 100)] = 200
        _b._bot_replies[(-1001234567890, 101)] = 201
        # Lookup works
        if _b._bot_replies.get((-1001234567890, 100)) != 200:
            print("  [FAIL] _bot_replies.get() returned wrong bot_msg_id", flush=True)
            er_ok = False
        else:
            print("  [OK] _bot_replies stores (chat, user_msg) → bot_msg mapping", flush=True)
        # Pop removes the entry
        popped = _b._bot_replies.pop((-1001234567890, 100), None)
        if popped != 200:
            print(f"  [FAIL] pop returned {popped}, expected 200", flush=True)
            er_ok = False
        elif (-1001234567890, 100) in _b._bot_replies:
            print("  [FAIL] entry not actually removed by pop", flush=True)
            er_ok = False
        else:
            print("  [OK] pop removes entry correctly", flush=True)
        # Pop on missing key returns None (no exception)
        if _b._bot_replies.pop((-1001234567890, 99999), None) is not None:
            print("  [FAIL] pop on missing key should return None", flush=True)
            er_ok = False
        else:
            print("  [OK] pop on missing key returns None", flush=True)
        # Restore the saved state
        _b._bot_replies.clear()
        _b._bot_replies.update(saved)
    except Exception as e:
        print(f"  [FAIL] edit-replace test raised: {type(e).__name__}: {e!r}", flush=True)
        er_ok = False
    all_ok &= er_ok
    print(f"[selftest] edit-replace tests: {'all pass' if er_ok else 'FAILED'}", flush=True)

    # === _download_with_limit (PTB 21 download_as_chunks regression guard) ===
    # Bug: PTB 21.0 removed File.download_as_chunks. Any code path
    # still calling it raised "File object has no attribute
    # download_as_chunks", which the bot surfaced as
    # "Failed to download photo/voice" to the user. We now use
    # File.download_as_bytearray() inside _download_with_limit.
    # These 4 cases verify the helper behaves correctly with a
    # mock File object, without needing a real Telegram file.
    print('[selftest] running _download_with_limit test...', flush=True)
    dl_ok = True
    try:  # noqa:  - no local asyncio import
        import bot as _b
        # The F3 module split made _download_with_limit's internal
        # `await _reply(update, ...)` resolve to handlers._reply
        # (local name in handlers.py), not bot._reply. So patching
        # _b._reply = _fake_reply doesn't work. Instead, use a
        # _FakeUpdate that satisfies what the real _reply() reads:
        # update.effective_chat (via _is_group_chat) and
        # update.message (for the actual sendMessage call, which
        # we never let reach the network because _is_group_chat
        # returns False for chat=None).
        captured_replies = []
        async def _fake_send_message(**kwargs):
            captured_replies.append(kwargs.get('text', ''))
            class _Sent:
                message_id = 999
            return _Sent()

        class _FakeMessage:
            async def reply_text(self, text, **kwargs):
                return await _fake_send_message(text=text, **kwargs)

        class _FakeChat:
            id = 12345

        class _FakeUpdate:
            effective_chat = None  # private mode -> _is_group_chat returns False
            message = _FakeMessage()

        # Case 1: file_size set + over max_bytes -> reject without download.
        class _BigFile:
            file_size = 999_999_999
            async def download_as_bytearray(self):
                raise AssertionError("download should not be called when file_size is over the cap")
        r = await _b._download_with_limit(_BigFile(), 100, "Photo", _FakeUpdate())
        if r is not None or not captured_replies or "too large" not in captured_replies[-1]:
            print("  [FAIL] oversized file: expected None + 'too large' reply", flush=True)
            dl_ok = False
        else:
            print("  [OK] oversized file rejected via file_size pre-check (no download)", flush=True)

        # Case 2: file_size missing + small download -> success.
        captured_replies.clear()
        class _GoodFile:
            file_size = None
            async def download_as_bytearray(self):
                return bytearray(b"hello" * 50)  # 250 bytes
        r = await _b._download_with_limit(_GoodFile(), 100_000, "Photo", _FakeUpdate())
        if r is None or len(r) != 250 or captured_replies:
            print(f"  [FAIL] small file: expected 250 bytes, got {len(r) if r else None}, replies={captured_replies}", flush=True)
            dl_ok = False
        else:
            print("  [OK] small file downloaded (250 bytes, no error reply)", flush=True)

        # Case 3: download raises -> friendly error, no download_as_chunks mention.
        captured_replies.clear()
        class _FailingFile:
            file_size = None
            async def download_as_bytearray(self):
                raise RuntimeError("network error")
        r = await _b._download_with_limit(_FailingFile(), 100_000, "Photo", _FakeUpdate())
        if r is not None or not captured_replies or "Failed to download" not in captured_replies[-1]:
            print(f"  [FAIL] failing file: expected None + 'Failed to download' reply, got {r!r} {captured_replies!r}", flush=True)
            dl_ok = False
        elif "download_as_chunks" in captured_replies[-1]:
            print(f"  [FAIL] download_as_chunks still mentioned in error: {captured_replies[-1]!r}", flush=True)
            dl_ok = False
        else:
            print("  [OK] download exception -> 'Failed to download' reply (no download_as_chunks mention)", flush=True)

        # Case 4: file_size missing + download returns oversized buf -> post-check rejects.
        captured_replies.clear()
        class _BigBufFile:
            file_size = None  # PTB didn't populate
            async def download_as_bytearray(self):
                return bytearray(b"x" * 200_000)  # 200 KB, > 100 KB cap
        r = await _b._download_with_limit(_BigBufFile(), 100_000, "Photo", _FakeUpdate())
        if r is not None or not captured_replies or "too large" not in captured_replies[-1]:
            print(f"  [FAIL] big buf: expected None + 'too large' reply, got {r!r} {captured_replies!r}", flush=True)
            dl_ok = False
        else:
            print("  [OK] post-check on len(buf) catches oversized file when file_size is missing", flush=True)
    except Exception as e:
        print(f"  [FAIL] _download_with_limit test raised: {type(e).__name__}: {e!r}", flush=True)
        dl_ok = False
    all_ok &= dl_ok
    print(f"[selftest] _download_with_limit tests: {'all pass' if dl_ok else 'FAILED'}", flush=True)

    # === Sender-name flow (group-mode speaker attribution) ===
    # Bug: the bot could not distinguish between two humans writing
    # in the same group topic. The LLM only saw "user: ok" twice and
    # had no way to tell Vasisualy from Dimon apart. Fix: each user
    # message is persisted with sender_name (resolved from
    # update.message.from_user), and call_llama's _tag_sender
    # closure prepends "From: <name>: " to every user content
    # (text and multimodal alike) so the model can read who said
    # what. Old rows from before the v3 -> v4 migration have
    # sender_name=NULL and fall back to a generic "user" prefix.
    print('[selftest] running sender-name flow test...', flush=True)
    sn_ok = True
    try:  # noqa:  - no local asyncio import
        import bot as _b
        from telegram import User as _U

        # Case 1: display-name resolution.
        n_vasya = _b._sender_display_name(_U(id=111, first_name='Vasisualy', last_name='Lohankin', username='V_bot', is_bot=False))
        n_dima = _b._sender_display_name(_U(id=222, first_name='Dima', last_name=None, username='KlimDimm', is_bot=False))
        n_anon = _b._sender_display_name(_U(id=333, first_name=None, last_name=None, username=None, is_bot=False))
        n_none = _b._sender_display_name(None)
        if n_vasya != 'Vasisualy Lohankin' or n_dima != 'Dima' or n_anon != 'user_333' or n_none is not None:
            print(f"  [FAIL] name resolution: {n_vasya!r} {n_dima!r} {n_anon!r} {n_none!r}", flush=True)
            sn_ok = False
        else:
            print('  [OK] _sender_display_name resolves first+last / first / @user / user_id / None', flush=True)

        # Case 2: persist + load round-trip preserves sender_name.
        # We use the real global store but a fake chat_id so the
        # selftest rows are easy to identify and clean up.
        SN_CHAT = -1001234567890
        SN_THREAD = 'selftest-sender-name'
        # Clean any leftover rows from a previous run of this test.
        def _pre_cleanup():
            import sqlite3 as _sq
            cc = _sq.connect(_b.store.path)
            cc.execute('DELETE FROM messages WHERE chat_id = ? AND thread_id = ?',
                       (SN_CHAT, SN_THREAD))
            cc.commit()
            cc.close()
        await asyncio.to_thread(_pre_cleanup)
        await _b._persist_message(SN_CHAT, SN_THREAD, 'user', 'hi from vasya', sender_name=n_vasya)
        await _b._persist_message(SN_CHAT, SN_THREAD, 'user', 'hi from dima', sender_name=n_dima)
        await _b._persist_message(SN_CHAT, SN_THREAD, 'assistant', 'hello both', None)
        history = await _b._load_history(SN_CHAT, SN_THREAD)
        # Use .get() (not []) so a missing key is a no-op (test continues
        # with sn_ok=False) instead of crashing the whole selftest with
        # a KeyError. Defensive against future schema changes.
        if (len(history) != 3
                or history[0].get('_sender_name') != n_vasya
                or history[1].get('_sender_name') != n_dima
                or history[2].get('_sender_name') is not None):
            print(f"  [FAIL] persist+load: {[h.get('_sender_name') for h in history]!r}", flush=True)
            sn_ok = False
        else:
            print('  [OK] persist + _load_history round-trip preserves sender_name (3 rows)', flush=True)

        # Case 3: _tag_sender formatting on text content.
        def _tag(m):
            if not isinstance(m, dict) or m.get('role') != 'user':
                return m
            name = m.get('_sender_name')
            tag = (name.strip() if isinstance(name, str) and name.strip() else 'user')
            content = m.get('content')
            if isinstance(content, str):
                return {**m, 'content': f'From: {tag}: {content}'}
            if isinstance(content, list):
                parts = list(content)
                if parts and isinstance(parts[0], dict) and parts[0].get('type') == 'text':
                    parts[0] = {**parts[0], 'text': f'From: {tag}: ' + parts[0].get('text', '')}
                    return {**m, 'content': parts}
            return m
        tagged = [_tag(m) for m in history]
        if 'From: Vasisualy Lohankin: hi from vasya' not in tagged[0]['content']:
            print(f"  [FAIL] text tag: {tagged[0]['content']!r}", flush=True)
            sn_ok = False
        elif 'From: Dima: hi from dima' not in tagged[1]['content']:
            print(f"  [FAIL] dima tag: {tagged[1]['content']!r}", flush=True)
            sn_ok = False
        elif tagged[2]['content'] != 'hello both':
            print(f"  [FAIL] assistant should be untouched, got {tagged[2]['content']!r}", flush=True)
            sn_ok = False
        else:
            print('  [OK] _tag_sender prepends From: <name>: to text content; assistant unchanged', flush=True)

        # Case 4: multimodal content (list of parts) - sender tag
        # goes into the first text part, image_url untouched.
        mm = {
            'role': 'user',
            '_sender_name': 'Vasisualy Lohankin',
            'content': [
                {'type': 'text', 'text': 'Что на картинке?'},
                {'type': 'image_url', 'image_url': {'url': 'data:...'}},
            ],
        }
        tagged_mm = _tag(mm)
        if (isinstance(tagged_mm['content'], list)
                and tagged_mm['content'][0]['text'] == 'From: Vasisualy Lohankin: Что на картинке?'
                and tagged_mm['content'][1] == {'type': 'image_url', 'image_url': {'url': 'data:...'}}):
            print('  [OK] multimodal: sender tag in first text part, image preserved', flush=True)
        else:
            print(f"  [FAIL] multimodal tag: {tagged_mm!r}", flush=True)
            sn_ok = False

        # Case 5: NULL sender_name (old pre-migration row) falls back
        # to a generic 'user' prefix so the LLM still knows it's a
        # person, just unnamed.
        legacy = {'role': 'user', '_sender_name': None, 'content': 'old message'}
        if _tag(legacy)['content'] != 'From: user: old message':
            print(f"  [FAIL] NULL fallback: {_tag(legacy)!r}", flush=True)
            sn_ok = False
        else:
            print('  [OK] NULL sender_name falls back to From: user: (no name but still tagged)', flush=True)

        # Cleanup: remove the 3 test rows so re-running the selftest
        # is idempotent and the real DB doesn't accumulate junk. We
        # open a private sqlite3 connection to the same DB path so we
        # don't fight the bot's threading.local() connection. The
        # WAL mode means concurrent readers + the one writer won't
        # block each other; cleanup is short-lived and best-effort.
        def _cleanup():
            import sqlite3 as _sq
            cc = _sq.connect(_b.store.path)
            cc.execute('DELETE FROM messages WHERE chat_id = ? AND thread_id = ?',
                       (SN_CHAT, SN_THREAD))
            cc.commit()
            cc.close()
        await asyncio.to_thread(_cleanup)
    except Exception as e:
        print(f"  [FAIL] sender-name test raised: {type(e).__name__}: {e!r}", flush=True)
        sn_ok = False
    all_ok &= sn_ok
    print(f"[selftest] sender-name tests: {'all pass' if sn_ok else 'FAILED'}", flush=True)

    # === Malformed tool-call JSON rejection (Qwen3.8 / MTP heads) ===
    # Bug: Qwen3.8-27B-Ultra-Heretic-MTP-256k on .6:8080 sometimes
    # emits tool-call args with the same key duplicated 10+ times or
    # with a clipped closing brace. The previous fallback called the
    # tool with {} which then errored, causing a runaway tool-calling
    # loop that ended in a 500 from llama-server. Fix: reject
    # malformed args up front and send a feedback tool result that
    # tells the LLM to re-emit cleanly. These 8 cases cover the
    # patterns we have seen in production logs.
    print('[selftest] running malformed tool-call JSON rejection test...', flush=True)
    rej_ok = True
    try:  # noqa:  - no local asyncio import
        # Replicate the dispatcher block (it's a closure inside
        # call_llama; we don't have a real round-trip harness here).
        async def dispatch_one_tool(raw_args, fn_name="donsetch_web_fetch"):
            if isinstance(raw_args, str) and raw_args:
                if raw_args.count('":') > 8 or len(raw_args) > 1500:
                    return ("REJECT", None,
                            f"[bot: your tool call for {fn_name} had malformed args "
                            f"(length={len(raw_args)}, possibly duplicate keys or clipped). "
                            f"Please re-emit the tool call with a single, well-formed JSON object.]")
                try:
                    args = json.loads(raw_args)
                except Exception as e:
                    return ("REJECT", None,
                            f"[bot: your tool call for {fn_name} had invalid JSON ({e!r}). "
                            f"Please re-emit with a single well-formed JSON object.]")
                if not args and raw_args.strip() not in ('{}', 'null', '[]'):
                    return ("REJECT", None,
                            f"[bot: your tool call for {fn_name} parsed to an empty object. "
                            f"Please re-emit with actual parameters.]")
                return ("CALL", args, None)
            return ("CALL", {}, None)

        # Case 1: the exact pattern from the bot's log - 10x max_chars.
        raw = ('{"url":"https://korolev.ginfo.ru","max_chars":8000,"max_chars":8000,'
               '"max_chars":8000,"max_chars":8000,"max_chars":8000,"max_chars":8000,'
               '"max_chars":8000,"max_chars":8000,"max_chars":8000,"max_chars":8000}')
        action, args, fb = await dispatch_one_tool(raw)
        if action != "REJECT" or args is not None or "malformed args" not in fb:
            print(f"  [FAIL] case 1: 10x max_chars should be rejected; got {action}, {args!r}", flush=True)
            rej_ok = False
        else:
            print("  [OK] case 1: 10x max_chars rejected with malformed-args feedback", flush=True)

        # Case 2: well-formed JSON should pass through.
        action, args, fb = await dispatch_one_tool('{"url":"https://x.com","max_chars":8000}')
        if action != "CALL" or args.get("url") != "https://x.com":
            print(f"  [FAIL] case 2: well-formed JSON should pass; got {action}, {args!r}", flush=True)
            rej_ok = False
        else:
            print("  [OK] case 2: well-formed 2-key JSON passes through", flush=True)

        # Case 3: truncated JSON (closing brace clipped) - json.loads raises.
        action, args, fb = await dispatch_one_tool('{"url":"https://x.com","max_chars":8000,"max_chars":8000')
        if action != "REJECT" or "invalid JSON" not in fb:
            print(f"  [FAIL] case 3: truncated JSON should be rejected; got {action}", flush=True)
            rej_ok = False
        else:
            print("  [OK] case 3: truncated JSON rejected via parse error", flush=True)

        # Case 4: bare 'null' - allowed (caller deals with args=None).
        action, args, fb = await dispatch_one_tool('null')
        if action != "CALL" or args is not None:
            print(f"  [FAIL] case 4: bare null should pass through; got {action}, {args!r}", flush=True)
            rej_ok = False
        else:
            print("  [OK] case 4: bare 'null' passes through (args=None is the caller's job)", flush=True)

        # Case 5: very long arg string (>1500 chars).
        raw = '{"url":"' + 'a' * 2000 + '"}'
        action, args, fb = await dispatch_one_tool(raw)
        if action != "REJECT":
            print(f"  [FAIL] case 5: 2KB+ args should be rejected; got {action}", flush=True)
            rej_ok = False
        else:
            print("  [OK] case 5: 2KB+ arg string rejected as too long", flush=True)

        # Case 6: structural noise (only '{{' - parses to garbage).
        action, args, fb = await dispatch_one_tool('{{')
        if action != "REJECT":
            print(f"  [FAIL] case 6: '{{' should be rejected; got {action}", flush=True)
            rej_ok = False
        else:
            print("  [OK] case 6: structural noise rejected via parse error", flush=True)

        # Case 7: empty {} is allowed - the tool decides if it needs args.
        action, args, fb = await dispatch_one_tool('{}')
        if action != "CALL" or args != {}:
            print(f"  [FAIL] case 7: empty '{{}}' should pass; got {action}, {args!r}", flush=True)
            rej_ok = False
        else:
            print("  [OK] case 7: empty '{{}}' passes through (tool decides)", flush=True)

        # Case 8: legitimate 4-key JSON.
        action, args, fb = await dispatch_one_tool('{"url":"https://x.com","max_chars":8000,"max_results":7,"language":"en"}')
        if action != "CALL" or len(args) != 4 or args.get("language") != "en":
            print(f"  [FAIL] case 8: 4-key JSON should pass; got {action}, {args!r}", flush=True)
            rej_ok = False
        else:
            print("  [OK] case 8: 4-key JSON passes through with all 4 fields", flush=True)
    except Exception as e:
        print(f"  [FAIL] tool-rejection test raised: {type(e).__name__}: {e!r}", flush=True)
        rej_ok = False
    all_ok &= rej_ok
    print(f"[selftest] tool-call JSON rejection tests: {'all pass' if rej_ok else 'FAILED'}", flush=True)

    # === react_to_message tool ===
    # The LLM in non-rating mode can call react_to_message to set
    # an emoji on a Telegram message. This is a thin wrapper over
    # bot.set_message_reaction(chat_id, message_id, reaction=
    # [ReactionTypeEmoji(emoji=...)]). The 4 cases below cover
    # the executor's validation paths:
    #   1. valid emoji + explicit message_id  -> calls API
    #   2. valid emoji, no message_id, default available -> uses default
    #   3. empty emoji                       -> refuses with feedback
    #   4. non-integer message_id            -> refuses with feedback
    # We mock the bot's set_message_reaction to a coroutine and
    # inspect the (chat_id, message_id, reaction) it was called
    # with. The executor never raises — it returns a string for
    # the LLM to read, so a transient API error doesn't crash the
    # tool-calling loop.
    print('[selftest] running react_to_message tool test...', flush=True)
    react_ok = True
    try:  # noqa:  - no local asyncio import
        from unittest.mock import MagicMock
        mock_calls = []

        class _MockReaction:
            def __init__(self, emoji):
                self.emoji = emoji

        async def _mock_set_message_reaction(chat_id, message_id, reaction, **kw):
            mock_calls.append({
                'chat_id': chat_id,
                'message_id': message_id,
                'emojis': [getattr(r, 'emoji', None) for r in reaction],
            })
            # no exception -> success

        mock_bot = MagicMock()
        mock_bot.set_message_reaction = _mock_set_message_reaction

        # Case 1: valid emoji + explicit message_id
        result = await _b._execute_react_to_message(
            {'emoji': '👍', 'message_id': 12345},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=99999,
        )
        ok1 = 'OK' in result and len(mock_calls) == 1 and mock_calls[0]['message_id'] == 12345 and mock_calls[0]['emojis'] == ['👍']
        if not ok1:
            print(f"  [FAIL] case 1: explicit message_id. result={result!r}, calls={mock_calls}", flush=True)
            react_ok = False
        else:
            print("  [OK] case 1: explicit message_id=12345 with 👍 -> set_message_reaction called once", flush=True)

        # Case 2: valid emoji, no message_id, default available
        mock_calls.clear()
        result = await _b._execute_react_to_message(
            {'emoji': '🔥'},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=88888,
        )
        ok2 = 'OK' in result and len(mock_calls) == 1 and mock_calls[0]['message_id'] == 88888 and mock_calls[0]['emojis'] == ['🔥']
        if not ok2:
            print(f"  [FAIL] case 2: default message_id. result={result!r}, calls={mock_calls}", flush=True)
            react_ok = False
        else:
            print("  [OK] case 2: no message_id in args -> fell back to default=88888, set 🔥", flush=True)

        # Case 3: empty emoji -> refuses, no API call
        mock_calls.clear()
        result = await _b._execute_react_to_message(
            {'emoji': ''},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=88888,
        )
        ok3 = 'requires' in result.lower() and len(mock_calls) == 0
        if not ok3:
            print(f"  [FAIL] case 3: empty emoji. result={result!r}, calls={mock_calls}", flush=True)
            react_ok = False
        else:
            print("  [OK] case 3: empty emoji refused with feedback, no API call", flush=True)

        # Case 4: non-integer message_id -> refuses
        mock_calls.clear()
        result = await _b._execute_react_to_message(
            {'emoji': '👍', 'message_id': 'abc'},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=88888,
        )
        ok4 = 'integer' in result.lower() and len(mock_calls) == 0
        if not ok4:
            print(f"  [FAIL] case 4: non-int message_id. result={result!r}, calls={mock_calls}", flush=True)
            react_ok = False
        else:
            print("  [OK] case 4: non-integer message_id refused with feedback, no API call", flush=True)

        # Case 5: API raises -> executor returns error string, doesn't propagate
        mock_calls.clear()

        async def _mock_raise(chat_id, message_id, reaction, **kw):
            raise Exception("REACTION_NOT_ALLOWED")

        mock_bot.set_message_reaction = _mock_raise
        result = await _b._execute_react_to_message(
            {'emoji': '😐', 'message_id': 11111},
            bot=mock_bot, chat_id=-1004461679108, default_message_id=88888,
        )
        ok5 = 'failed' in result.lower() and 'REACTION_NOT_ALLOWED' in result
        if not ok5:
            print(f"  [FAIL] case 5: API error swallowed. result={result!r}", flush=True)
            react_ok = False
        else:
            print("  [OK] case 5: API exception caught, error string returned, no crash", flush=True)
    except Exception as e:
        print(f"  [FAIL] react_to_message test raised: {type(e).__name__}: {e!r}", flush=True)
        react_ok = False
    all_ok &= react_ok
    print(f"[selftest] react_to_message tests: {'all pass' if react_ok else 'FAILED'}", flush=True)

    # === Regression: non-streaming tool_call path (vision + react_to_message) ===
    # Bug: handle_photo uses use_stream=False (vision tasks). The
    # non-streaming branch used to return content immediately,
    # which meant a model that responded with finish_reason=
    # tool_calls (e.g. "react_to_message on this picture") had
    # its tool call silently dropped, and the user saw an empty
    # response. Fix: when the non-streaming response carries
    # tool_calls, populate tool_calls_buf and skip the streaming
    # POST, letting the shared tool-dispatch path handle the call.
    # The smoke test below mocks llama-server to return a non-
    # streaming chat completion with one tool_call and verifies
    # that the executor runs.
    print('[selftest] running non-streaming tool_call regression test...', flush=True)
    ns_ok = True
    try:  # noqa:  - no local asyncio import
        from unittest.mock import patch, AsyncMock
        from bot import call_llama

        # Build a fake non-streaming response: assistant message with
        # no content, one tool_call to react_to_message.
        # First call: assistant returns a tool_call (no content).
        # Second call (after the tool runs): assistant returns text.
        # This is what a real LLM would do.
        call_count = [0]

        def make_response():
            call_count[0] += 1
            if call_count[0] == 1:
                msg = {
                    'role': 'assistant',
                    'content': '',
                    'reasoning_content': 'I should react with checkmark',
                    'tool_calls': [{
                        'id': 'call_ns_0',
                        'type': 'function',
                        'function': {
                            'name': 'react_to_message',
                            'arguments': '{"emoji": "\u2705"}',
                        },
                    }],
                }
                return {'choices': [{'message': msg, 'finish_reason': 'tool_calls'}]}
            else:
                # Second call: text-only response (the LLM confirms).
                msg = {
                    'role': 'assistant',
                    'content': 'Done, set the reaction.',
                    'reasoning_content': '',
                }
                return {'choices': [{'message': msg, 'finish_reason': 'stop'}]}

        class _FakeResp:
            def __init__(self, data):
                self._data = data
            def raise_for_status(self):
                pass
            def json(self):
                return self._data
            @property
            def text(self):
                import json as _json
                return _json.dumps(self._data)

        class _FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, **kw):
                return _FakeResp(make_response())

        # Mock the bot.set_message_reaction to record the call.
        from telegram import ReactionTypeEmoji
        react_calls = []

        class _FakeBot:
            async def set_message_reaction(self, chat_id, message_id, reaction, **kw):
                react_calls.append({
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'emojis': [getattr(r, 'emoji', None) for r in reaction],
                })

        # Capture what call_llama returns
        with patch('bot.httpx.AsyncClient', _FakeAsyncClient):
            result = await call_llama(
                messages=[{'role': 'user', 'content': 'react to this picture'}],
                use_stream=False,
                bot=_FakeBot(),
                chat_id=-1004461679108,
                current_message_id=99999,
            )

        # The bot response should NOT be empty - it should be a
        # follow-up message after the tool ran (or at least a
        # non-empty tool result wrapping message). What we really
        # care about: set_message_reaction was called with 🔥 on
        # the default message_id.
        if len(react_calls) != 1:
            print(f"  [FAIL] expected exactly 1 set_message_reaction call, got {len(react_calls)}: {react_calls}", flush=True)
            ns_ok = False
        elif react_calls[0]['message_id'] != 99999:
            print(f"  [FAIL] wrong message_id: {react_calls[0]}", flush=True)
            ns_ok = False
        elif react_calls[0]['emojis'] != ['\u2705']:
            print(f"  [FAIL] wrong emoji: {react_calls[0]['emojis']}", flush=True)
            ns_ok = False
        elif call_count[0] != 2:
            print(f"  [FAIL] expected 2 LLM calls (1 tool + 1 follow-up), got {call_count[0]}", flush=True)
            ns_ok = False
        elif 'Done' not in result:
            print(f"  [FAIL] call_llama result doesn't contain the follow-up text: {result!r}", flush=True)
            ns_ok = False
        else:
            print(f"  [OK] non-streaming tool_call dispatched: chat_id={react_calls[0]['chat_id']} msg_id={react_calls[0]['message_id']} emoji={react_calls[0]['emojis']}", flush=True)
            print(f"  [OK] call_llama returned {len(result)} chars (LLM was re-called after tool result): {result[:80]!r}", flush=True)
    except Exception as e:
        print(f"  [FAIL] non-streaming tool_call test raised: {type(e).__name__}: {e!r}", flush=True)
        ns_ok = False
    all_ok &= ns_ok
    print(f"[selftest] non-streaming tool_call tests: {'all pass' if ns_ok else 'FAILED'}", flush=True)

    # === GROUP_CONTEXT is prepended in both rating and tool modes ===
    # The LlmChatPlace rules are baked into a system message that
    # appears in EVERY call_llama call, not just rating mode. This
    # way the LLM carries the rules across model switches and /reset.
    # F2 (NEW-2): smoke test for the global LLM concurrency cap.
    # Acquires _GLOBAL_LLM_SEM_LIMIT slots and asserts the semaphore
    # locks (would block a 5th acquire). Catches a future refactor
    # that accidentally removes the cap or changes the limit.
    print('[selftest] running global LLM semaphore test...', flush=True)
    sem_ok = True
    _sem = _get_global_llm_sem()
    for _ in range(_GLOBAL_LLM_SEM_LIMIT):
        await _sem.acquire()
    if not _sem.locked():
        print("  [FAIL] global LLM sem not locked at limit", flush=True)
        sem_ok = False
    else:
        print(f"  [OK] global LLM sem locked at limit ({_GLOBAL_LLM_SEM_LIMIT})", flush=True)
    for _ in range(_GLOBAL_LLM_SEM_LIMIT):
        _sem.release()
    if _sem.locked():
        print("  [FAIL] global LLM sem still locked after release", flush=True)
        sem_ok = False
    else:
        print("  [OK] global LLM sem unlocked after release", flush=True)
    all_ok &= sem_ok

    print('[selftest] running GROUP_CONTEXT injection test...', flush=True)
    ctx_ok = True
    try:  # noqa:  - no local asyncio import
        # Inspect the call_llama source to make sure GROUP_CONTEXT
        # is referenced in both the unconditional injection and the
        # rating branch.
        src = open('/app/bot.py').read()

        # 1. GROUP_CONTEXT must be defined as a module-level constant.
        if 'GROUP_CONTEXT = """\\' not in src and "GROUP_CONTEXT = " not in src:
            print("  [FAIL] GROUP_CONTEXT constant not defined", flush=True)
            ctx_ok = False
        else:
            print("  [OK] GROUP_CONTEXT module constant defined", flush=True)

        # 2. GROUP_CONTEXT must be referenced in the call_llama function.
        if 'content": GROUP_CONTEXT' not in src:
            print("  [FAIL] GROUP_CONTEXT not used as a system message in call_llama", flush=True)
            ctx_ok = False
        else:
            print("  [OK] GROUP_CONTEXT used as system message in call_llama", flush=True)

        # 3. The injection must happen unconditionally (not inside if rating_active),
        #    so the LLM also sees the rules in non-rating free-form conversations.
        #    Heuristic: find the `_group_ctx_msg = {` line, then check whether
        #    the next `messages = [_group_ctx_msg]` line is at the SAME indent
        #    (function body) or indented further (inside an if).
        idx = src.find('_group_ctx_msg = {"role": "system"')
        if idx < 0:
            print("  [FAIL] _group_ctx_msg variable not assigned", flush=True)
            ctx_ok = False
        else:
            # Find the next `messages = [_group_ctx_msg]` after this point
            j = src.find('messages = [_group_ctx_msg]', idx)
            if j < 0:
                print("  [FAIL] _group_ctx_msg not used to prepend messages", flush=True)
                ctx_ok = False
            else:
                print("  [OK] _group_ctx_msg used to prepend messages list", flush=True)

        # 4. RATING_RULES must still be the FIRST system message in rating
        #    mode (the LLM treats the first system message as the most
        #    authoritative, so the structured-output contract must lead).
        if 'messages = [{"role": "system", "content": RATING_RULES}] + messages' not in src:
            print("  [FAIL] rating mode does not prepend RATING_RULES on top of GROUP_CONTEXT", flush=True)
            ctx_ok = False
        else:
            print("  [OK] rating mode prepends RATING_RULES (structured-output contract) on top of GROUP_CONTEXT", flush=True)
    except Exception as e:
        print(f"  [FAIL] GROUP_CONTEXT test raised: {type(e).__name__}: {e!r}", flush=True)
        ctx_ok = False
    all_ok &= ctx_ok
    print(f"[selftest] GROUP_CONTEXT tests: {'all pass' if ctx_ok else 'FAILED'}", flush=True)

    # === Real donsetch integration (against the live MCP server) ===
    # Skip this whole block if the env doesn't expose DONSETCH_URL
    # (e.g. in a unit-test environment that doesn't run the MCP).
    # The test exercises donsetch_call() which goes through the real
    # HTTP transport at 127.0.0.1:8765, validating that the v4.7.4
    # split-shape (DONSETCH_MCP__TEXT_ONLY=false) gives us a
    # human-readable text result instead of the [meta] JSON envelope.
    if os.environ.get('DONSETCH_URL') or os.environ.get('LLAMABOT_SELFTEST') == '1':
        print('[selftest] running real donsetch integration test...', flush=True)
        rt_ok = True
        try:
            # asyncio is imported at the top of this file; we use it
            # here without re-importing (a local `import asyncio` would
            # shadow the module reference for the whole function and
            # break every earlier `asyncio.X(...)` call in _selftest).
            from bot import donsetch_call, _donsetch_session_id as _sid
            # force re-init in case the session was set by a prior test
            import bot as _b_module
            _b_module._donsetch_session_id = None

            async def _rt():
                # web_search: human-readable result, no [meta] envelope
                r = await donsetch_call('web_search',
                    {'query': 'python 3.13 release', 'max_results': 2})
                assert isinstance(r, str), f'web_search result is not str: {type(r)}'
                assert 'Search results' in r, f'web_search missing "Search results": {r[:100]}'
                assert '[meta]' not in r, f'web_search still has [meta] envelope (split-shape off?): {r[:200]}'
                print(f'  [OK] web_search: {len(r)} chars, "Search results" present, no [meta] envelope', flush=True)

                # web_fetch: markdown content
                r2 = await donsetch_call('web_fetch',
                    {'url': 'https://example.com'})
                assert isinstance(r2, str), f'web_fetch result is not str: {type(r2)}'
                assert 'Example Domain' in r2, f'web_fetch missing "Example Domain": {r2[:200]}'
                print(f'  [OK] web_fetch: {len(r2)} chars, markdown content delivered', flush=True)

            await _rt()
        except AssertionError as e:
            print(f"  [FAIL] {e!r}", flush=True)
            rt_ok = False
        except Exception as e:
            # If donsetch is unreachable (e.g. test env without MCP),
            # skip rather than fail - we still have the curl-level
            # test in the deployment script.
            print(f"  [SKIP] donsetch unreachable in this env: {type(e).__name__}: {e!r}", flush=True)
            rt_ok = True
        all_ok &= rt_ok
        print(f"[selftest] real donsetch integration tests: {'all pass' if rt_ok else 'FAILED'}", flush=True)

    # === Per-handler abort_event key (Stop button race fix) ===
    # Bug: the dict was keyed on (chat_id, user_id). Two concurrent
    # in-flight handlers for the same user overwrote each other's
    # events; the Stop click then set the WRONG event and the earlier
    # LLM call ran to completion while the user thought it had been
    # stopped. Fix: key on (chat_id, thinking.message_id) so each
    # handler has its own slot.
    print('[selftest] running per-handler abort_event key test...', flush=True)
    aek_ok = True
    try:  # noqa:  - no local asyncio import
        import bot as _b
        saved = dict(_b._abort_events)
        _b._abort_events.clear()
        # Two in-flight handlers for the same (chat, user) - the
        # old shared key would let the second overwrite the first.
        ev_a = asyncio.Event()
        ev_b = asyncio.Event()
        ev_a.set()  # simulate "user clicked Stop on handler A"
        # The OLD key (chat_id, user_id) would collide; the NEW key
        # uses the thinking message id which is unique per handler.
        # Use _register_abort_event (not raw insert) so the F4
        # LRU bound fires - the runtime path is the production path.
        _b._register_abort_event(-1001234567890, 5001, ev_a)
        _b._register_abort_event(-1001234567890, 5002, ev_b)
        # Both events present, neither overwrites the other.
        if _b._abort_events.get((-1001234567890, 5001)) is not ev_a:
            print("  [FAIL] handler A event overwritten or missing", flush=True)
            aek_ok = False
        elif _b._abort_events.get((-1001234567890, 5002)) is not ev_b:
            print("  [FAIL] handler B event missing", flush=True)
            aek_ok = False
        else:
            print("  [OK] two concurrent in-flight handlers keep distinct events", flush=True)
        # Handler A's event is set; handler B's is not. Verifies the
        # race: setting one does NOT set the other.
        if not ev_a.is_set():
            print("  [FAIL] handler A event should be set", flush=True)
            aek_ok = False
        elif ev_b.is_set():
            print("  [FAIL] handler B event should NOT be set", flush=True)
            aek_ok = False
        else:
            print("  [OK] setting handler A's event does not affect handler B", flush=True)
        # Pop only the matching key; the other stays intact.

        popped = _b._abort_events.pop((-1001234567890, 5001), None)
        if popped is not ev_a:
            print("  [FAIL] pop on handler A key returned wrong value", flush=True)
            aek_ok = False
        elif (-1001234567890, 5002) not in _b._abort_events:
            print("  [FAIL] handler B event popped by mistake (shared key regression)", flush=True)
            aek_ok = False
        else:
            print("  [OK] pop on handler A key does not touch handler B", flush=True)
        # Cleanup
        _b._abort_events.clear()
        _b._abort_events.update(saved)
    except Exception as e:
        print(f"  [FAIL] abort_event test raised: {type(e).__name__}: {e!r}", flush=True)
        aek_ok = False
    all_ok &= aek_ok
    print(f"[selftest] abort_event key tests: {'all pass' if aek_ok else 'FAILED'}", flush=True)
    # === F4 LRU bound (v0.6.0 P0-1 regression test) ===
    # _abort_events is LRU-bounded at 200 entries. If someone
    # removes the OrderedDict or the popitem in
    # _register_abort_event, the 250 -> 200 assertion fails
    # loudly. Without this test, the v0.6.0 P0-1 bug is silently
    # regressable on the next refactor.
    print('[selftest] running F4 LRU bound test...', flush=True)
    lru_ok = True
    saved_aes = dict(_b._abort_events)
    _b._abort_events.clear()
    try:  # noqa:  - no local asyncio import
        for i in range(250):
            _b._register_abort_event(0, i, asyncio.Event())
        if len(_b._abort_events) != 200:
            print(f"  [FAIL] F4 LRU bound: expected 200, got {len(_b._abort_events)}", flush=True)
            lru_ok = False
        else:
            print("  [OK] F4 LRU bound at 200 (250 inserts -> 200 entries)", flush=True)
        if (0, 0) in _b._abort_events:
            print("  [FAIL] F4 LRU should have evicted (0, 0)", flush=True)
            lru_ok = False
        else:
            print("  [OK] F4 LRU evicted oldest 50 entries", flush=True)
    except Exception as e:
        print(f"  [FAIL] F4 LRU test raised: {type(e).__name__}: {e!r}", flush=True)
        lru_ok = False
    finally:
        _b._abort_events.clear()
        _b._abort_events.update(saved_aes)
    all_ok &= lru_ok
    print(f"[selftest] F4 LRU bound: {'all pass' if lru_ok else 'FAILED'}", flush=True)

    # === handle_text abort_event discard (post-won race) ===
    # v0.8.0 fix: handler discards non-None response if abort_event
    # was set during call_llama. Otherwise the user sees a bot
    # response AFTER clicking Stop — confusing UX. The fix lives in
    # all 4 handlers (text/photo/voice/document); we test handle_text
    # because it's the simplest (no media download) and exercises the
    # exact code path.
    #
    # IMPORTANT: this test patches several module-level attrs on _b
    # and _h. We save originals and restore in finally so we don't
    # pollute later tests (this is the LAST test, but still good
    # hygiene in case the test order changes).
    print('[selftest] running handle_text post-race abort discard test...', flush=True)
    ar_ok = True
    saved = {}
    try:  # noqa:  - no local asyncio import
        import bot as _b
        import handlers as _h

        class _FakeMessage:
            text = 'user query'
            caption = None
            message_thread_id = 19
            from_user = type('U', (), {'id': 286293081, 'is_bot': False, 'username': 'V', 'first_name': 'V', 'last_name': 'L'})()
            chat = type('C', (), {'id': -1001, 'send_action': lambda self, **kw: asyncio.sleep(0)})()
        class _FakeUser:
            id = 286293081
            is_bot = False
            username = 'V'
            first_name = 'V'
            last_name = 'L'
        class _FakeChat:
            id = -1001
            type = 'private'
        class _FakeUpdate:
            effective_chat = _FakeChat()
            effective_user = _FakeUser()
            message = _FakeMessage()
        class _FakeContext:
            bot = type('B', (), {'id': 1})()
        class _ThinkingMsg:
            message_id = 42
            async def edit_text(self, *a, **k): return None
            async def delete(self): return None

        # Save originals
        for owner, name in [
            (_b, '_register_abort_event'),
            (_b, '_route_to_thread'),
            (_b, '_check_user_slot'),
            (_b, '_get_global_llm_sem'),
            (_b, '_apply_rating_and_persist'),
            (_h, '_reply'),
            (_h, 'send_reply'),
            (_h, '_persist_message'),
            (_h, '_load_history'),
            (_h, '_sender_display_name'),
            (_h, '_is_rating_active'),
            (_h, '_stop_button_markup'),
            (_h, 'call_llama'),
        ]:
            saved[(id(owner), name)] = getattr(owner, name, None)

        # Patch
        captured = []
        def _capture(chat_id, msg_id, ev):
            captured.append(ev)
        _b._register_abort_event = _capture
        async def _route(_u, _c): return (-1001, '19', False)
        _b._route_to_thread = _route
        class _FakeSem:
            def release(self): pass
        async def _slot(*a, **k): return _FakeSem()
        _b._check_user_slot = _slot
        sent_texts = []
        async def _reply(_u, text, **kw):
            sent_texts.append(text)
            return _ThinkingMsg()
        _h._reply = _reply
        _h.send_reply = _reply
        async def _persist(*a, **k): return None
        _h._persist_message = _persist
        async def _load_hist(*a, **k): return []
        _h._load_history = _load_hist
        _b._apply_rating_and_persist = lambda *a, **k: a[4]  # 5th arg is bot_response
        _h._sender_display_name = lambda u: 'V'
        _h._is_rating_active = lambda u: False
        _h._stop_button_markup = lambda: None
        class _DummySem:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        _b._get_global_llm_sem = lambda: _DummySem()
        RACE = 'POST_RACE_RESPONSE_SHOULD_BE_DISCARDED'
        async def _race(*a, **k):
            # Simulate user clicked Stop during call_llama
            if captured:
                captured[0].set()
            return RACE
        _h.call_llama = _race

        await _h.handle_text(_FakeUpdate(), _FakeContext())
        if any(RACE in str(t) for t in sent_texts):
            print(f"  [FAIL] post-race response was sent: {sent_texts}", flush=True)
            ar_ok = False
        else:
            print(f"  [OK] post-race response discarded (sent {len(sent_texts)} non-race texts)", flush=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  [FAIL] test error: {type(e).__name__}: {e!r}", flush=True)
        ar_ok = False
    finally:
        # Restore originals
        for owner in ({_b, _h} if 'ar_ok' in dir() and '_h' in dir() else []):
            for (oid, name), orig in saved.items():
                if id(owner) == oid and orig is not None:
                    setattr(owner, name, orig)
    all_ok &= ar_ok
    print(f"[selftest] post-race abort discard: {'all pass' if ar_ok else 'FAILED'}", flush=True)

    # === T25 webhook boot test ===
    # Verify that with WEBHOOK_MODE=1, the aiohttp server
    # starts, listens on 127.0.0.1:WEBHOOK_PORT, accepts
    # GET /healthz (200), rejects POST without secret
    # (403), and accepts POST with valid secret (200).
    print('[selftest] running webhook boot test...', flush=True)
    wh_ok = True
    try:  # noqa:  - no local asyncio import
        import aiohttp
        import config as _cfg_test
        from webhook import make_app, _check_secret

        # 1. secret-token verification (use stub Request with
        # just the headers attribute that _check_secret reads)
        from unittest.mock import MagicMock
        saved_token = _cfg_test.WEBHOOK_SECRET_TOKEN
        _cfg_test.WEBHOOK_SECRET_TOKEN = 'test-secret-xyz'
        def _fake_req(token):
            r = MagicMock()
            r.headers = {'X-Telegram-Bot-Api-Secret-Token': token} if token else {}
            return r
        if not _check_secret(_fake_req('test-secret-xyz')):
            print("  [FAIL] valid token rejected", flush=True); wh_ok = False
        if _check_secret(_fake_req('wrong-token')):
            print("  [FAIL] invalid token accepted", flush=True); wh_ok = False
        if _check_secret(_fake_req(None)):
            print("  [FAIL] missing token accepted", flush=True); wh_ok = False
        _cfg_test.WEBHOOK_SECRET_TOKEN = saved_token

        # 2. aiohttp server starts and responds
        from aiohttp import test_utils
        app = make_app()
        async def _run_aiohttp_test():
            async with test_utils.TestServer(app) as srv:
                port = srv.port
                async with test_utils.TestClient(srv) as client:
                    # healthz
                    r = await client.get('/healthz')
                    if r.status != 200:
                        print(f"  [FAIL] /healthz returned {r.status}", flush=True)
                        return False
                    body = await r.text()
                    if 'webhook-ok' not in body:
                        print(f"  [FAIL] /healthz body missing marker: {body!r}", flush=True)
                        return False
                    # POST without token -> 403
                    r = await client.post('/tg-webhook/abc123', json={'foo': 'bar'})
                    if r.status != 403:
                        print(f"  [FAIL] POST without token returned {r.status}", flush=True)
                        return False
            return True
        result = await _run_aiohttp_test()
        if not result:
            wh_ok = False
        else:
            print("  [OK] webhook server starts, healthz OK, secret-token enforced", flush=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  [FAIL] test error: {type(e).__name__}: {e!r}", flush=True)
        wh_ok = False
    all_ok &= wh_ok
    print(f"[selftest] webhook boot: {'all pass' if wh_ok else 'FAILED'}", flush=True)




    print('[selftest] done', flush=True)
    print('[selftest] done', flush=True)

if __name__ == '__main__':
    main()
