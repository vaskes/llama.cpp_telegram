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
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

# === LLM endpoints ===
LLAMA_URL = os.environ.get("LLAMA_URL", "http://localhost:8080/v1")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://localhost:8000")
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
# Per-user: at most 2 concurrent call_llama() per (chat_id, user_id).
# Global: at most 4 concurrent call_llama() across all users.
# Combined: getUpdates limit=10 + 2*2 per-user + 4 global = at most
# 10 in-flight dispatches at any moment, only 4 in the LLM call.
_GLOBAL_LLM_SEM_LIMIT = 4
_PER_USER_SEMAPHORE_LIMIT = 2

# === Concurrency caps (v0.5.1 T1 / P0-1) ===
# Per-user: at most 2 concurrent call_llama() per (chat_id, user_id).
# Global: at most 4 concurrent call_llama() across all users.
# Combined: getUpdates limit=10 + 2*2 per-user + 4 global = at most
# 10 in-flight dispatches at any moment, only 4 in the LLM call.
_GLOBAL_LLM_SEM_LIMIT = 4
_PER_USER_SEMAPHORE_LIMIT = 2

# === Concurrency caps (v0.5.1 T1 / P0-1) ===
# Per-user: at most 2 concurrent call_llama() per (chat_id, user_id).
# Global: at most 4 concurrent call_llama() across all users.
# Combined: getUpdates limit=10 + 2*2 per-user + 4 global = at most
# 10 in-flight dispatches at any moment, only 4 in the LLM call.
_GLOBAL_LLM_SEM_LIMIT = 4
_PER_USER_SEMAPHORE_LIMIT = 2

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
CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "20"))

# === Selftest toggle ===
LLAMABOT_SELFTEST = os.environ.get("LLAMABOT_SELFTEST") == "1"

# === Donsetch MCP ===
DONSETCH_URL = os.environ.get("DONSETCH_URL", "http://localhost:8765/mcp")
DONSETCH_SESSION_ID: Optional[str] = None
