"""Chat-shape and command-argument helpers. TECH-DEBT #2.

These lived in dispatch.py, which handlers.py then imported *inside functions*
to dodge a cycle: dispatch -> handlers (for the handlers) and handlers ->
dispatch (for these). Twenty-six late imports later, the graph was unreadable
and static analysis could not see through it.

The cycle is unnecessary: none of these functions touch the dispatcher. They
take an update or a string and answer a question about it. Moving them here
gives both sides a module-level import with no cycle.

Imports nothing from the bot. That is the whole point — a leaf module that
anything may depend on.
"""

import asyncio
import re
import time as _time
from collections import deque

from logconf import get_logger
log = get_logger(__name__)

#: Sub-talk names: 1-32 chars, must start with a letter or digit, may
#: contain letters, digits, dash, underscore, dot.
_SUBTALK_NAME_RE = re.compile(r"^[^\s]{1,32}$")

#: Forum topic names are allowed to be longer — Telegram itself permits
#: 128, and an operator naming a topic "deploy pipeline notes" is not
#: making a mistake.
_TOPIC_NAME_RE = re.compile(r"^[^\s]{1,128}$")


def _is_group_chat(update) -> bool:
    """True if the message is in a forum-enabled supergroup (or any non-private chat).

    Telegram Bot API quirk: in a forum-enabled supergroup, messages in
    the General topic have message_thread_id == None (not set), while
    messages in regular topics have it set to the topic id. So
    "message_thread_id is not None" is NOT a reliable way to detect
    group mode. The reliable signal is `chat.is_forum` (PTB attribute
    on Chat) or, failing that, `chat.type != 'private'`.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    if getattr(chat, "is_forum", False):
        return True
    return chat.type != "private"


def _parse_subtalk_arg(text: str) -> str | None:
    """Extract a single sub-talk name from a /command arg string.

    Returns the first whitespace-separated token, or None if absent
    or invalid. For sub-talks we use the strict 32-char limit; for
    forum-topic names (group mode) see _parse_topic_arg() which
    accepts up to 128 chars.
    """
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return None
    name = parts[1].strip()
    if not _SUBTALK_NAME_RE.match(name):
        return None
    return name


def _parse_topic_arg(text: str) -> str | None:
    """Like _parse_subtalk_arg but for Telegram forum topic names.

    Telegram's own limit for a forum topic name is 128 characters, so a
    name that would be rejected as a sub-talk is perfectly legal as a topic.
    """
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return None
    name = parts[1].strip()
    if not _TOPIC_NAME_RE.match(name):
        return None
    return name


# === Send rate limiting ===
#
# Review finding (Qwen, 2026-10-21): chat_id keys were created here and never
# removed. Timestamps aged out of the deque, the deque and its key stayed for
# the process lifetime — a bot invited to N chats held N permanent entries, and
# leaving a group did not free the slot. The chat list is attacker-influenceable
# over time, so this is a slow leak with no upper bound you can point at.
#
# Now bounded by _SEND_WINDOW_MAX_KEYS and evicted here, where the leak would
# otherwise grow, rather than in a sweeper task.

_send_window: dict = {}  # chat_id -> deque[float]
_RATE_WINDOW_SEC = 60.0
_RATE_MAX_PER_WINDOW = 15  # conservative: 15 sends per 60s, leaves 5 for user-originated overhead
#: How many chat_ids we track at once. Above this, idle entries are dropped;
#: an active chat re-creates its deque on the next send, so the cap costs a
#: re-allocation and never correctness.
_SEND_WINDOW_MAX_KEYS = 1024


async def _rate_limit_send(chat_id: int) -> None:
    """Sleep if chat_id is over its per-60s rate. Returns when
    the next send would be within budget."""
    now = _time.monotonic()
    window = _send_window.setdefault(chat_id, deque())
    # Drop timestamps older than 60s
    while window and now - window[0] > _RATE_WINDOW_SEC:
        window.popleft()
    if len(window) >= _RATE_MAX_PER_WINDOW:
        # Sleep until oldest entry ages out + 0.5s headroom
        sleep_for = _RATE_WINDOW_SEC - (now - window[0]) + 0.5
        if sleep_for > 0:
            log.info(f"[rate_limit] chat={chat_id} window full "
                f"({len(window)} sends in {_RATE_WINDOW_SEC}s); "
                f"sleeping {sleep_for:.1f}s before next send")
            await asyncio.sleep(sleep_for)
    window.append(_time.monotonic())

    # Evict idle chats. An ACTIVE chat is never dropped: resetting its
    # rate-limit budget would be a security hole rather than a memory win.
    if len(_send_window) > _SEND_WINDOW_MAX_KEYS:
        for stale_chat in [c for c, d in _send_window.items() if not d]:
            _send_window.pop(stale_chat, None)


# --- moved verbatim from dispatch.py (TECH-DEBT #2) ---
