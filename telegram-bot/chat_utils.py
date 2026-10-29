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


# === Telegram message limits ===
#
# Telegram's own limit for a text message is 4096 characters. A reply
# over that comes back as `Message_too_long`, and the user sees the
# API error instead of the answer.
#
# 4000, not 4096: the chunk has to fit with room for whatever the
# transport adds, and a boundary that lands exactly on the limit is one
# character away from a rejected message.

#: The size of one text message the bot sends.
TELEGRAM_TEXT_CHUNK = 4000


def split_text(text: str, limit: int = TELEGRAM_TEXT_CHUNK) -> list[str]:
    """Split `text` into pieces of at most `limit` characters.

    Empty input gives an empty list: there is nothing to send, and an
    empty message is rejected by Telegram ("Message text is empty").

    A chunk that is only whitespace is dropped. It carries no
    information, and on the path that falls back to a friendly message
    it would put that message in the middle of an answer.

    The split is on character boundaries, not on words. A code block cut
    in the middle is still readable as text, and a half-sentence would
    be worse than a half-code-block.
    """
    if not text:
        return []
    pieces = [text[i:i + limit] for i in range(0, len(text), limit)]
    return [piece for piece in pieces if piece.strip()]


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
#
# Correction (2026-10-29, external review): the first fix evicted only chats
# whose deque was EMPTY, and a deque here is never empty — every key is
# created by a send and every send appends a timestamp. So the eviction never
# fired, the cap was decoration, and the leak it was written for was still
# there. An idle chat keeps its last timestamp until its next send, which is
# exactly the case the eviction was supposed to catch. The eviction now drops
# windows that have EXPIRED, where "expired" is a property of the timestamps
# rather than of the container.

_send_window: dict = {}  # chat_id -> deque[float]
_RATE_WINDOW_SEC = 60.0
_RATE_MAX_PER_WINDOW = 15  # conservative: 15 sends per 60s, leaves 5 for user-originated overhead
#: How many chat_ids we track at once. Above this, expired windows are dropped;
#: a chat re-creates its deque on the next send, so the cap costs a
#: re-allocation and never correctness.
_SEND_WINDOW_MAX_KEYS = 1024


def _evict_expired_windows(now: float) -> int:
    """Drop the windows whose budget has aged out. Returns how many went.

    A window is expired when its NEWEST timestamp is older than the window:
    nothing it holds can throttle anything any more, so dropping it resets no
    budget. A chat still inside its window is never touched — evicting it
    would silently hand it a fresh budget, which is a security hole rather
    than a memory win.
    """
    cutoff = now - _RATE_WINDOW_SEC
    expired = [
        chat_id for chat_id, window in _send_window.items()
        if not window or window[-1] <= cutoff
    ]
    for chat_id in expired:
        _send_window.pop(chat_id, None)
    return len(expired)


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

    # Evict windows that have aged out. An ACTIVE chat is never dropped:
    # resetting its rate-limit budget would be a security hole rather than
    # a memory win.
    if len(_send_window) > _SEND_WINDOW_MAX_KEYS:
        _evict_expired_windows(now)


# --- moved verbatim from dispatch.py (TECH-DEBT #2) ---
