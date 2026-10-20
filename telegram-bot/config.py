# config.py
# Environment-driven configuration. Every constant here is
# initialised from os.environ at import time. This module is
# imported by every other module; no imports from other
# project modules to avoid circular deps.
import asyncio
import os
from typing import Optional

# === Telegram bot identity ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required but not set")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
# v0.10.2: TELEGRAM_API is env-driven to support smoke testing with a
# local mock Telegram server. Default is the real Telegram API.
# Format: full URL including the /bot<TOKEN> suffix, e.g.
#   https://api.telegram.org/bot<TOKEN>
# or for tests:
#   http://127.0.0.1:18090/bot<TOKEN>
TELEGRAM_API = os.environ.get(
    "TELEGRAM_API",
    f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else "",
)

# === LLM endpoints ===
LLAMA_URL = os.environ.get("LLAMA_URL", "http://localhost:8080/v1")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://localhost:8000")
# v0.10.2: Whisper language hint. Empty = autodetect (recommended for multilingual users).
# Default "ru" for backwards compat with the Russian-speaking LlmChatPlace group.
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "ru")
API_KEY = os.environ.get("API_KEY", "sk-no-key")
MODEL = os.environ.get("MODEL", "").strip()

# === Authorization (LOCKDOWN) ===
ALLOWED_USER_IDS_RAW = os.environ.get("ALLOWED_USER_IDS", "").strip()
ALLOWED_USERNAMES_RAW = os.environ.get("ALLOWED_USERNAMES", "").strip()
ALLOWED_USER_IDS = {int(x) for x in ALLOWED_USER_IDS_RAW.split(",") if x.strip().isdigit()}
ALLOWED_USERNAMES = {x.lstrip("@").lower() for x in ALLOWED_USERNAMES_RAW.split(",") if x.strip()}

# LOCKDOWN: True when BOTH ALLOWED_USER_IDS and ALLOWED_USERNAMES
# are empty. In that case is_authorized() rejects everything. The
# flag exists so command handlers can short-circuit ("bot is in
# lockdown, do not respond to any /commands either") instead of
# relying on update_id iteration.
if not ALLOWED_USER_IDS and not ALLOWED_USERNAMES:
    LOCKDOWN = True
    print("[SECURITY] ALLOWED_USER_IDS and ALLOWED_USERNAMES both empty -> LOCKDOWN (reject all).", flush=True)
else:
    LOCKDOWN = False
    print(f"[SECURITY] whitelist: {len(ALLOWED_USER_IDS)} ids, {len(ALLOWED_USERNAMES)} usernames", flush=True)
    if ALLOWED_USERNAMES and not ALLOWED_USER_IDS:
        print("[SECURITY] WARNING: ALLOWED_USERNAMES is set but ALLOWED_USER_IDS is empty. "
              "Username-based access can break if a user changes their @username. "
              "Prefer numeric IDs.", flush=True)

# === File-size limits ===
MAX_PHOTO_BYTES = int(os.environ.get("MAX_PHOTO_BYTES", "10000000"))     # 10 MB


# === v0.10.2: Friendly message when the LLM returns an empty response.
# Telegram rejects empty messages with "Message text is empty", and
# some models (e.g. reasoning-heavy ones) spend the whole token
# budget on internal reasoning. Without this fallback, the user
# sees a cryptic API error or a crash (NameError in older code).
# The message is in English because the LLM error path is
# language-agnostic — even a Russian bot user should understand
# "the LLM didn't return a reply, try /reset".
EMPTY_RESPONSE_FALLBACK = (
    "[model returned an empty response. This usually means the LLM "
    "spent all its tokens on internal reasoning without writing a reply. "
    "Try /reset to clear context, or switch to a less reasoning-heavy model.]"
)
MAX_DOC_BYTES = int(os.environ.get("MAX_DOC_BYTES", "5000000"))         # 5 MB
MAX_VOICE_BYTES = int(os.environ.get("MAX_VOICE_BYTES", "20000000"))    # 20 MB
MAX_VIDEO_NOTE_BYTES = int(os.environ.get("MAX_VIDEO_NOTE_BYTES", "50000000"))  # 50 MB

# === Tool gating ===
DISABLED_TOOLS = {
    x.strip() for x in os.environ.get("DISABLED_TOOLS", "").split(",")
    if x.strip()
}

# === Async primitives (singletons, populated on bot startup) ===
SHUTDOWN_EVENT: Optional[asyncio.Event] = None

# === Tool cache (lazy-init) ===
_TOOLS_CACHE = None

# === Concurrency caps (v0.5.1 T1 / P0-1) ===
# Re-exports for backward compat. The actual values live in
# state.py (created in F3 stage 6 cleanup). Modules that
# need them: `from config import _GLOBAL_LLM_SEM_LIMIT` still
# works, but new code should import from state.py.
from state import (
    _GLOBAL_LLM_SEM_LIMIT, _PER_USER_SEMAPHORE_LIMIT,
    _abort_events, _bot_replies, _get_global_llm_sem,
)

# === Conversation store ===
DB_PATH = os.environ.get("CONVERSATIONS_DB", "/app/data/conversations.db")
# Deprecated (T70): superseded by CONTEXT_MAX_MESSAGES in context_budget.py,
# which is where the context policy now lives. Kept because it is a
# documented env var that forks may already set — CONTEXT_MAX_MESSAGES
# wins when it is set, and this is only the last-resort default.
# Do NOT wire new call sites to this; use context_budget.get_budget().
CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "20"))

# === Selftest toggle ===
LLAMABOT_SELFTEST = os.environ.get("LLAMABOT_SELFTEST") == "1"

# === Donsetch MCP ===
DONSETCH_URL = os.environ.get("DONSETCH_URL", "http://localhost:8765/mcp")
DONSETCH_SESSION_ID: Optional[str] = None

# === Webhook mode (T25) ===
# When WEBHOOK_MODE=1, the bot starts an aiohttp server on
# WEBHOOK_HOST:WEBHOOK_PORT (default 0.0.0.0:8443) instead of
# polling getUpdates. The nginx-proxy in front of the bot
# terminates TLS and forwards POST <WEBHOOK_PATH>/<secret-token>
# to this server.
#
# WEBHOOK_URL is the PUBLIC URL Telegram should POST to. If set
# (e.g. https://bot.example.com/tg-webhook), the bot auto-registers
# the webhook with Telegram on startup via bot.setWebhook(url=...,
# secret_token=...). If empty, the operator must register manually
# (see docs/WEBHOOK.md). The secret token is NOT appended to the
# URL — Telegram stores it separately and sends it on every request
# as the X-Telegram-Bot-Api-Secret-Token header. The handler must
# verify that header before processing.
WEBHOOK_MODE = os.environ.get("WEBHOOK_MODE", "0") == "1"
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8443"))
# Path component. The secret token is appended at runtime
# (random or WEBHOOK_SECRET_TOKEN env).
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/tg-webhook")
# Shared secret Telegram sends in X-Telegram-Bot-Api-Secret-Token.
# If empty, the handler refuses every request (safe-by-default).
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN", "")
