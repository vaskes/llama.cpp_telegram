# config.py
# Environment-driven configuration. Every constant here is
# initialised from os.environ at import time. This module is
# imported by every other module; no imports from other
# project modules to avoid circular deps.
import asyncio
import ast
import json as _json
import os
from pathlib import Path
from typing import Optional

from logconf import get_logger
from mcp_client import GATE_DEV_CHAT, McpServerConfig
log = get_logger(__name__)

# === Telegram bot identity ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required but not set")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
# v0.10.2: TELEGRAM_API is env-driven to support smoke testing with a
# local mock Telegram server. Default is the real Telegram API.
#
# Format: API root WITH the trailing "/bot" suffix. PTB 21's
# Application.builder().base_url() treats the value as a prefix and
# appends "/<TOKEN>/<method>", e.g. /bot123:ABC/getMe. The colon in
# the token is parsed as a port separator unless the path already
# starts with /bot<TOKEN> -- the trailing "/bot" on the prefix is
# what keeps the token in the path. This is also PTB's native
# default (see BaseRequest).
#
# Examples:
#   real Telegram:        https://api.telegram.org/bot
#   local mock (smoke):   http://127.0.0.1:18090/bot
#
# History: an earlier version of this default used
# f"https://api.telegram.org/bot{BOT_TOKEN}" (the full URL with the
# token baked in). Once dispatch.py added `.base_url(TELEGRAM_API)`
# in 88a119b, that baked token got doubled into the URL path
# (`/bot<TOKEN>/bot<TOKEN>/<method>`) and Telegram replied with
# InvalidToken. The .env override that ships in this commit was
# the empirical workaround; this commit is the source fix.
TELEGRAM_API = os.environ.get(
    "TELEGRAM_API",
    "https://api.telegram.org/bot" if BOT_TOKEN else "",
)

# Which update types this bot subscribes to. ONE declaration, read by
# both transports.
#
# It used to be written twice, in two formats and in two orders:
#
#   dispatch_polling  "allowed_updates": '["message","edited_message","chat_member","callback_query"]'
#   dispatch_webhook  "allowed_updates": ["message","edited_message","callback_query","chat_member"]
#
# Same four types, different serialisation, two elements swapped. A
# tuple plus the two encodings below means the two transports cannot
# disagree about the SET even if they must disagree about the wire
# format -- polling passes a JSON string in the query, webhook passes a
# real list in the JSON body.
#
# Why one constant matters: the edit-replace feature was dead in
# production for years because its handler lived in the polling branch
# and production ran webhook. A subscription that names a type nobody
# handles is a promise the bot cannot keep, and the subscription list is
# the one place where that promise is visible.
#
# `edited_message` was dropped on 2026-10-24, together with the feature:
# the bot no longer re-answers an edited message, so subscribing to
# edits only buys traffic the bot has nothing to do with. An edited
# message is simply ignored -- the answer to the original stays.
ALLOWED_UPDATES = ("message", "callback_query", "chat_member")

# Query-parameter form: getUpdates wants JSON-in-a-string.
ALLOWED_UPDATES_JSON = _json.dumps(list(ALLOWED_UPDATES))

# Body form: setWebhook wants a real JSON array.
ALLOWED_UPDATES_LIST = list(ALLOWED_UPDATES)

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
    log.info("[SECURITY] ALLOWED_USER_IDS and ALLOWED_USERNAMES both empty -> LOCKDOWN (reject all).")
else:
    LOCKDOWN = False
    log.info(f"[SECURITY] whitelist: {len(ALLOWED_USER_IDS)} ids, {len(ALLOWED_USERNAMES)} usernames")
    if ALLOWED_USERNAMES and not ALLOWED_USER_IDS:
        log.warning("[SECURITY] WARNING: ALLOWED_USERNAMES is set but ALLOWED_USER_IDS is empty. "
              "Username-based access can break if a user changes their @username. "
              "Prefer numeric IDs.")

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
# The marker for "the model produced no answer", as one string.
#
# This used to be a local of call_llama called EMPTY_FALLBACK_PREFIX with
# a comment claiming it kept "the marker and the recovery logic in one
# place". It did: call_llama defined it and read it nowhere, while two
# producers wrote the text out longhand and six consumers in handlers.py
# matched on a sixth copy. A constant nobody reads is a comment with a
# name, and the comment was the part that was false.
#
# Both producers now build from this one string, so they cannot drift
# apart, and every consumer matches on it rather than on a literal.
EMPTY_RESPONSE_PREFIX = "[model returned an empty response."

# User-facing version of the same message. Built from the prefix rather
# than restating it, so the text handlers.py screens for and the text
# the user reads cannot disagree about where one ends and the other
# starts.
EMPTY_RESPONSE_FALLBACK = (
    EMPTY_RESPONSE_PREFIX
    + " This usually means the LLM "
    "spent all its tokens on internal reasoning without writing a reply. "
    "Try /reset to clear context, or switch to a less reasoning-heavy model.]"
)
# What the user actually reads when the model came back empty twice. A
# separate string from EMPTY_RESPONSE_FALLBACK on purpose: that one is
# the internal marker, this one is for a person. They were the same text
# written out three times in handlers.py, where a fix to the wording only
# reached two of the three.
EMPTY_RESPONSE_USER_FALLBACK = (
    "\U0001F914 Could not get an answer from the model. "
    "Possible reasons: an overloaded long context, "
    "a hard question (predictions, niche facts) or "
    "a temporary llama-server failure. Try:\n"
    "\u2022 /reset \u2014 clear the history and start over\n"
    "\u2022 Rephrase the question\n"
    "\u2022 Add more detail to the question"
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
# ONE event object for the whole process. It is created here, at import,
# and never reassigned.
#
# It used to be `Optional[asyncio.Event] = None`, and
# `dispatch_polling._install_signal_handlers` did `global SHUTDOWN_EVENT`
# to publish the real one. That rebinds only *that module's* binding of a
# `from config import SHUTDOWN_EVENT` name -- every other holder keeps
# None. `llm_iteration`, which is where the ToolLoop abort guard reads
# it, therefore never saw the shutdown at all: SIGTERM was dead for the
# in-flight inference, which is the exact failure this event exists to
# prevent. This is the "unwritable globals" class from TECH-DEBT #1, in
# its purest form: every name is bound, and the reader still sees None.
#
# A module-level `asyncio.Event()` is safe on 3.11 (`python:3.11-slim`):
# it does not bind a loop in `__init__`, the loop is resolved lazily on
# first await. So it can exist before `asyncio.run()`.
SHUTDOWN_EVENT: asyncio.Event = asyncio.Event()

# === Tool cache (lazy-init) ===
_TOOLS_CACHE = None

# === Concurrency caps (v0.5.1 T1 / P0-1) ===
# The values live in state.py and are imported from THERE.
#
# They used to be re-exported here "for backward compat", which made
# config a dependent of state -- and config is the leaf that fifteen
# modules read from, so the one module that must know nothing now
# imported the module that knows the most. The re-export had exactly one
# consumer (dispatch.py); it now imports from state directly.

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

# === MCP servers (one config block) ===
#
# The imported half of the tool set: the tools the model may call
# that somebody else's server implements. One list, one shape --
# adding a server is adding an entry here, and nothing else in the
# bot changes.
#
# Each entry says:
#   name      the prefix its tool names carry ("<name>_<tool>");
#   url       where the server listens (MCP over HTTP);
#   headers   what to send (the sandbox wants its bearer token);
#   include   the server's own tools to take (empty = all of them);
#   exclude   the server's own tools to leave alone;
#   gate      "profile" = every chat the profile allows,
#             "dev-chat" = only the allowlisted chats, in dev mode;
#   timeout   the per-call budget, in seconds.
#
# The tool NAMES are deliberately not listed here: the registry asks
# the server what it has. A list kept by hand is a list that goes
# stale -- which is how the bot ended up advertising tools it could
# not execute.
def _mcp_servers() -> list:
    """The MCP servers, as the operator configured them.

    A server without a token is not configured: the sandbox's MCP
    endpoint wants the same bearer token as its HTTP ones, and a
    server that would answer 401 is a server that only produces log
    noise.

    The side-car's address and token are read from the module that
    owns them (the shell tool's), because they are the same
    side-car: two readers of one fact is how two facts drift apart.
    """
    from personal_tools.bash_command import AGENT_STACK_TOKEN, AGENT_STACK_URL

    servers = []
    if AGENT_STACK_TOKEN:
        servers.append(McpServerConfig(
            name="sandbox",
            url=f"{AGENT_STACK_URL}/mcp",
            headers={"Authorization": f"Bearer {AGENT_STACK_TOKEN}"},
            # `bash` is the shell the bot already has, with the
            # policy that goes with it; `delete_file` destroys. Both
            # are one word away from being imported, and neither is
            # imported until that is a decision somebody made.
            include=("read_file", "file_info"),
            exclude=("bash", "delete_file"),
            gate=GATE_DEV_CHAT,
            timeout=MCP_CALL_TIMEOUT,
            connect_timeout=MCP_CONNECT_TIMEOUT,
        ))
    return servers


#: How long a TOOL CALL to an MCP server may take. A crawl is slow on
#: purpose, so this one is generous.
MCP_CALL_TIMEOUT = float(os.environ.get("MCP_CALL_TIMEOUT", "60"))

#: How long the HANDSHAKE with an MCP server may take. This one is
#: short on purpose: the handshake happens while the model waits for
#: its tool list, and a server that cannot be reached in five seconds
#: is not going to be reached in sixty either.
MCP_CONNECT_TIMEOUT = float(os.environ.get("MCP_CONNECT_TIMEOUT", "5"))

MCP_SERVERS = _mcp_servers()

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
        log.info(w)
    return len(warnings)


# Report anything BOT_-prefixed that nothing reads. Runs at import, after the
# helpers above are defined, so it costs one scan of .env.example per start.
_ = emit_env_warnings()
