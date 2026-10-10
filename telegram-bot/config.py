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

# === Conversation store ===
DB_PATH = os.environ.get("CONVERSATIONS_DB", "/app/data/conversations.db")
CONTEXT_MESSAGES = int(os.environ.get("CONTEXT_MESSAGES", "20"))

# === Selftest toggle ===
LLAMABOT_SELFTEST = os.environ.get("LLAMABOT_SELFTEST") == "1"

# === Donsetch MCP ===
DONSETCH_URL = os.environ.get("DONSETCH_URL", "http://localhost:8765/mcp")
DONSETCH_SESSION_ID: Optional[str] = None
