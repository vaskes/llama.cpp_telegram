# config.py
# Environment-driven configuration. Every constant here is
# initialised from os.environ at import time. This module is
# imported by every other module; no imports from other
# project modules to avoid circular deps.
import asyncio
import ast
import os
from pathlib import Path
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


# --- unrecognised env vars -------------------------------------------------
#
# A fork that sets a misspelled variable gets the default and no indication
# of why. That is a silent failure: the only symptom is the bot behaving as
# though the operator never configured anything.
#
# Warned, not fatal. This repository is a fork base with 200+ forks and a
# public container image, and refusing to boot on an unrelated exported
# variable would take every one of them down over a typo in one .env. The
# contract that IS enforced -- documented vars are read, read vars are
# documented -- lives in tests/test_env_contract.py and fails CI.

_BOT_ENV_PREFIXES = ("BOT_", "MAX_", "LLAMA_", "RATING_", "TELEGRAM_",
                     "CONTEXT_", "WHISPER_", "SANDBOX_", "PERSONA_",
                     "ALLOWED_", "WEBHOOK_", "DONSETCH_", "REANCHOR_")

_BOT_ENV_NAMES = frozenset({
    # Documented in .env.example; kept here as a fast path so the common case
    # does not scan every source file at startup.
})


_ENV_LITERAL_RE = None


def _known_env_names() -> set:
    """Every env var the code references, from the code itself.

    The first version read .env.example for this, and .env.example does not
    ship in the container image -- so in production the known-set was empty and
    every BOT_-prefixed variable warned on every start. A warning that fires on
    correct configuration trains the operator to ignore it, which is worse than
    not warning at all.

    So the source of truth is the source tree, which is always present. The
    scan is deliberately broad (any env-var-shaped string literal): the cost
    of over-matching here is missing a typo, and the cost of under-matching is
    a wall of false alarms at every startup.
    """
    global _ENV_LITERAL_RE
    if _ENV_LITERAL_RE is None:
        import re as _re
        _ENV_LITERAL_RE = _re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

    here = os.path.dirname(os.path.abspath(__file__))
    names = set(_BOT_ENV_NAMES)

    for path in _iter_python_sources(here):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _ENV_LITERAL_RE.match(node.value):
                    names.add(node.value)
    return names


def _iter_python_sources(root):
    """Every .py under root, personal_tools/ included.

    A top-level-only glob missed bash_command.py, which reads
    LLAMABOT_BASH_RPM and LLAMABOT_DEV_ALLOWED_CHAT_IDS.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def warn_unrecognised_env(env=None) -> list:
    """Return human-readable warnings for BOT_-prefixed vars nothing reads.

    Prefix-scoped on purpose: warning about PATH, TZ and every framework
    variable a container image exports would bury the real signal.
    """
    import re as _re
    env = os.environ if env is None else env
    known = _known_env_names()
    pattern = _re.compile(r"^[A-Z_][A-Z0-9_]*$")

    warnings = []
    for key in sorted(env):
        if key in known:
            continue
        if not key.startswith(_BOT_ENV_PREFIXES):
            continue
        if not pattern.match(key):
            continue
        warnings.append(
            f"[config] env var {key} is set but no code reads it. If this "
            f"was meant to configure something, check the spelling against "
            f".env.example -- a typo here is silently ignored."
        )
    return warnings


def emit_env_warnings() -> int:
    """Print warnings and return the count, so callers can assert on it."""
    warnings = warn_unrecognised_env()
    for w in warnings:
        print(w, flush=True)
    return len(warnings)


# Report anything BOT_-prefixed that nothing reads. Runs at import, after the
# helpers above are defined, so it costs one scan of .env.example per start.
_ = emit_env_warnings()
