"""Entry point and fork compatibility surface.

This file is NOT the library. Every name below is defined somewhere else:
config.py, prompts.py, storage.py, dispatch.py, handlers.py, state.py,
call_llama.py, persistence.py, rating.py, selftest.py. They are imported
here because `python -u bot.py` is how the container starts the bot, and
because forks patch some of them through `bot.<name>`.

That is the whole reason this file exists. TECH-DEBT #1 tracked how much of
it was residue; as of this commit it is explicit instead of accidental:

  * `__all__` below is the declared fork surface. Anything not listed there
    is not a contract, and tests/test_facade_ownership.py fails if a new
    re-export appears without being declared.
  * `import bot` from another module in telegram-bot/ is an error, pinned by
    the same test file. Import the owning module instead -- the six patches
    selftest.py used to write against this module proved the point, because
    nothing here is read by the code that uses those names.

The re-exports stay. Removing them would break forks for no runtime gain:
an `import` of an existing symbol costs a dict entry, whereas the ~100 lines
they occupy were what the review kept misreading as "logic". The cost that
did exist was the opposite one -- names looked public because nothing said
otherwise, so people patched them and the patch did nothing. `__all__` is
the fix; deletion is not.

TECH-DEBT #1, remaining scope: none. The entry point is the job, the
compatibility surface is now declared rather than implied, and the module
singleton `store` is reached through storage.get_store() which is the single
patch point (a0fde76).
"""

import asyncio
import signal
import socket
import urllib.parse

# === Force IPv4: docker container has no IPv6 routing, but Telegram DNS returns AAAA first ===
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, *args, **kwargs):
    """Filter out IPv6 results -- the Telegram bot lib otherwise tries IPv6
    and gets Network unreachable."""
    results = _orig_getaddrinfo(host, *args, **kwargs)
    if not results:
        return results
    # If any family explicitly requested, pass through; else filter to IPv4
    family = kwargs.get('family', socket.AF_UNSPEC)
    if family == socket.AF_UNSPEC:
        return [r for r in results if r[0] == socket.AF_INET] or results
    return results


socket.getaddrinfo = _ipv4_only_getaddrinfo

# === Conversation store ===
# One Storage instance for the process, reached through the single patch
# point. Callers inside the bot use storage.get_store() directly; `store` is
# here because forks read bot.store. Accessing it here is what guarantees the
# connection is opened rather than a module being imported.
import storage

store = storage.get_store()

# === Re-exports: the declared fork surface ===
# Grouped by owner. Do not reorder or reformat without reading
# tests/test_facade_ownership.py, which pins the shape of this block.

# config.py
from config import (
    ALLOWED_USER_IDS, ALLOWED_USER_IDS_RAW, ALLOWED_USERNAMES,
    ALLOWED_USERNAMES_RAW, API_KEY, BOT_TOKEN, BOT_USERNAME,
    CONTEXT_MESSAGES, DB_PATH, DISABLED_TOOLS, DONSETCH_SESSION_ID,
    DONSETCH_URL, LLAMA_URL, MAX_DOC_BYTES, MAX_PHOTO_BYTES,
    MAX_VIDEO_NOTE_BYTES, MAX_VOICE_BYTES, MODEL,
    TELEGRAM_API, WHISPER_URL, LLAMABOT_SELFTEST,
)

# prompts.py -- BOT_COMMANDS is patched by forks (see the note in prompts.py)
from prompts import (
    BLOAT_EMOJI, BOT_COMMANDS, GROUP_CONTEXT, RATING_EMOJI, RATING_MODE,
    RATING_RULES, WELCOME_TEXT,
)

# selftest.py -- older docs and scripts/ reference bot._selftest
from selftest import SELFTEST_KEY, SELFTEST_USER_ID, _selftest

# dispatch.py
import dispatch
from dispatch import main

# Module-level shutdown signal. Set by main._run() when SIGINT/SIGTERM fires.
# call_llama() reads its own copy between iterations to bail out fast (faster
# than docker stop's 10s SIGKILL grace). Kept as a module global because the
# polling loop has no way to pass per-update context into the PTB handlers;
# threading it through handler signatures would touch every dispatcher, which
# was out of scope when this was written.
SHUTDOWN_EVENT: "asyncio.Event | None" = None

__all__ = [
    # entry point
    "main",
    # the store, for forks that read bot.store
    "store",
    # selftest entry points
    "SELFTEST_KEY", "SELFTEST_USER_ID", "_selftest",
    # config
    "ALLOWED_USER_IDS", "ALLOWED_USER_IDS_RAW", "ALLOWED_USERNAMES",
    "ALLOWED_USERNAMES_RAW", "API_KEY", "BOT_TOKEN", "BOT_USERNAME",
    "CONTEXT_MESSAGES", "DB_PATH", "DISABLED_TOOLS", "DONSETCH_SESSION_ID",
    "DONSETCH_URL", "LLAMA_URL", "LLAMABOT_SELFTEST", "MAX_DOC_BYTES",
    "MAX_PHOTO_BYTES", "MAX_VIDEO_NOTE_BYTES", "MAX_VOICE_BYTES", "MODEL",
    "TELEGRAM_API", "WHISPER_URL",
    # prompts
    "BLOAT_EMOJI", "BOT_COMMANDS", "GROUP_CONTEXT", "RATING_EMOJI",
    "RATING_MODE", "RATING_RULES", "WELCOME_TEXT",
    # the shutdown event dispatch sets and call_llama mirrors
    "SHUTDOWN_EVENT",
]


if __name__ == '__main__':
    main()
