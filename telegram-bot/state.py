# state.py
# Cross-module state shared between handlers.py and dispatch.py.
# Centralizing here breaks the "every function needs a late
# import from bot" mess from the initial F3 shim pattern.
#
# Why a dedicated module:
#   - handlers.py needs _abort_events (insert/pop),
#     _bot_replies (insert), _get_global_llm_sem (acquire)
#   - dispatch.py needs _abort_events (lookup in cmd_callback),
#     _bot_replies (lookup in edit-replace),
#     _get_global_llm_sem (also acquire)
#   - bot.py's _selftest accesses all three
#
# Without state.py, every consumer does `from bot import ...`
# which is a circular import waiting to break.

import asyncio
import time
from collections import OrderedDict

_GLOBAL_LLM_SEM_LIMIT = 4
_PER_USER_SEMAPHORE_LIMIT = 2
_ABORT_EVENTS_MAXSIZE = 200
_USER_SEMAPHORES_MAXSIZE = 500
_BOT_REPLIES_MAXSIZE = 500

_abort_events: "OrderedDict[tuple[int, int], asyncio.Event]" = OrderedDict()
_bot_replies: "dict[tuple[int, int], int]" = {}
_global_llm_sem: "asyncio.Semaphore | None" = None


def _get_global_llm_sem() -> asyncio.Semaphore:
    global _global_llm_sem
    if _global_llm_sem is None:
        _global_llm_sem = asyncio.Semaphore(_GLOBAL_LLM_SEM_LIMIT)
    return _global_llm_sem


def _register_abort_event(chat_id: int, message_id: int, ev: asyncio.Event) -> None:
    """Insert (chat_id, message_id) -> ev into _abort_events with
    LRU eviction if at maxsize. Touch (move to end) on every
    successful lookup so active handlers don't get evicted while
    still in flight.

    F4 cleanup: caps the dict at 200 entries so a handler that
    raises between insert and pop doesn't leak forever.
    """
    key = (chat_id, message_id)
    if key in _abort_events:
        # Touch: move to end so we don't evict an active handler
        # just to re-insert at the same key.
        _abort_events.move_to_end(key)
    else:
        _abort_events[key] = ev
        while len(_abort_events) > _ABORT_EVENTS_MAXSIZE:
            _abort_events.popitem(last=False)


# === _bot_replies: edit-replace tracking (v0.10.0: LRU) ===
# Maps (chat_id, user_message_id) -> bot_message_id so when the user
# edits their message, we can delete the stale bot reply. Previously
# grew without bound; v0.10.0 adds LRU eviction at _BOT_REPLIES_MAXSIZE.

_bot_replies: "OrderedDict[tuple[int, int], int]" = OrderedDict()


def _register_bot_reply(chat_id: int, user_message_id: int, bot_message_id: int) -> None:
    """Track that the bot's reply to user_message_id is bot_message_id.
    Used by the edit-replace flow in dispatch._dispatch_update.
    """
    key = (chat_id, user_message_id)
    _bot_replies[key] = bot_message_id
    _bot_replies.move_to_end(key)
    while len(_bot_replies) > _BOT_REPLIES_MAXSIZE:
        _bot_replies.popitem(last=False)


def _pop_bot_reply(chat_id: int, user_message_id: int) -> int | None:
    """Return and remove the bot's reply id for the user message, if any.
    Returns None if no reply was tracked."""
    return _bot_replies.pop((chat_id, user_message_id), None)


# Per-(chat_id, user_id) semaphore. Keys are tuples so the same
# user in different chats, or different users in the same chat,
# each get their own slot. The semaphore is created lazily in
# _get_user_semaphore() to avoid a top-level asyncio.Semaphore
# before the event loop is running.
_user_semaphores: "OrderedDict[tuple[int, int], asyncio.Semaphore]" = OrderedDict()


def _get_user_semaphore(chat_id: int, user_id: int) -> asyncio.Semaphore:
    """Return the per-(chat, user) semaphore, creating it on first call.

    v0.10.0: LRU eviction at _USER_SEMAPHORES_MAXSIZE so a long-running
    bot in a busy group doesn't grow this dict without bound. The
    bot's ALLOWED_USER_IDS LOCKDOWN already bounds the user count
    in our deployment, but a public fork (or a future relaxation
    of LOCKDOWN) would hit this leak.
    """
    key = (chat_id, user_id)
    if key in _user_semaphores:
        # Touch: move to end so an active user's sem isn't evicted
        _user_semaphores.move_to_end(key)
        return _user_semaphores[key]
    from config import _PER_USER_SEMAPHORE_LIMIT
    sem = asyncio.Semaphore(_PER_USER_SEMAPHORE_LIMIT)
    _user_semaphores[key] = sem
    while len(_user_semaphores) > _USER_SEMAPHORES_MAXSIZE:
        _user_semaphores.popitem(last=False)
    return sem

# === _user_recent_msg: rate-based burst detection for split handling ===
# Telegram caps a single text message at 4096 characters; long
# prompts are auto-split by the client at paragraph boundaries
# (\n\n). The bot cannot predict where the split lands, so any
# size-window heuristic produces false negatives (e.g. a 6K
# prompt split as 3910 + 2498) or false positives (any
# legitimate message in a wide window blocks follow-ups for
# 30 s).
#
# New approach (T34, 2026-10-13): detect the BURST PATTERN,
# not the size. If the SAME (chat, user) sends two messages
# within BURST_WINDOW_SEC, and the first message was larger
# than BURST_MIN_FIRST_BYTES, treat the second as a probable
# follow-up of a Telegram auto-split. Drop the second
# message and ABORT the in-flight LLM call for the first
# (via the registered abort_event). Output a clear error
# so the user understands why both messages were lost.
#
# Why per-(chat, user) and not per-chat: in a group, user A
# typing a long prompt must not block user B's follow-up.
#
# Why 2 s: short enough to not interfere with normal
# back-and-forth (which is paced by human typing speed, ~3-5
# s between turns), long enough to cover the gap between two
# poll batches of a single Telegram update (which can be
# 100 ms - 2 s apart).
#
# Why 3000 bytes: excludes burst patterns of short messages
# like "ok" + "thanks" (each < 50 bytes). A genuine split
# always has at least one chunk of >1500 bytes (Telegram
# roughly halves or thirds a long text); 3000 is conservative
# (2x the minimum) to keep false positives low.
#
# Edge case: a 4095-char prompt does NOT trigger a Telegram
# split, so the burst check sees only one message. The bot
# processes it normally. This is a known limitation; users
# who hit the cap should send via Bot API directly.

import asyncio
_BURST_WINDOW_SEC = 2.0
_BURST_MIN_FIRST_BYTES = 3000
_user_recent_msg: "dict[tuple[int, int], tuple[float, int, asyncio.Event | None]]" = {}


# Minimum gap between prev_ts and now for a burst to be considered
# real. Anything tighter means we are looking at the same handle_text
# call's own EARLY-RECORD, not a real follow-up message. 10 ms is well
# below any plausible Telegram inter-message delay (the fastest a
# user can press Enter twice) and well above the time.monotonic()
# resolution on a typical Linux host (microseconds).
_BURST_SELF_GUARD_SEC = 0.010


def _check_burst_split(chat_id: int, user_id: int, current_size_bytes: int) -> "asyncio.Event | None":
    """If the current message looks like a follow-up of a Telegram
    auto-split (sent within BURST_WINDOW_SEC of a >3000-byte message
    from the same (chat, user)), return the abort_event registered
    for the in-flight LLM call of that first message. The caller
    should `await` the abort, then reply with a split-detection
    error and drop the current message.

    Returns None if this is not a burst:
    - no previous message for this (chat, user)
    - previous message was recorded in the same handle_text call
      (self-guard: EARLY-RECORD writes prev_ts, then the burst
      check in the same call would otherwise see its own write
      and falsely flag a burst)
    - previous message was too long ago
    - previous message was too short to be a split half
    """
    key = (chat_id, user_id)
    prev = _user_recent_msg.get(key)
    if prev is None:
        return None
    prev_ts, prev_size, prev_event = prev
    now = time.monotonic()
    # Self-guard: same-call EARLY-RECORD + burst check.
    if now - prev_ts < _BURST_SELF_GUARD_SEC:
        return None
    if now - prev_ts > _BURST_WINDOW_SEC:
        return None
    if prev_size < _BURST_MIN_FIRST_BYTES:
        return None
    return prev_event


def _record_user_message(chat_id: int, user_id: int, size_bytes: int, abort_event: "asyncio.Event | None" = None) -> None:
    """Remember that (chat, user) just sent a message of size_bytes.
    The abort_event is the in-flight LLM call's abort signal so a
    later burst can cancel it. Pass None if no call is in flight
    (e.g. message was rejected before the LLM was invoked).

    Important: a small follow-up message (< _BURST_MIN_FIRST_BYTES)
    does NOT overwrite a previously recorded big message. It only
    refreshes the abort_event (so the next burst check finds a
    valid event) and leaves the original size and timestamp in
    place. Without this, the 2nd chunk of a Telegram auto-split
    (typically smaller than the 1st) would clobber the 1st chunk's
    size, and the burst check on a 3rd message would see the small
    size and miss the burst.
    """
    key = (chat_id, user_id)
    now = time.monotonic()
    existing = _user_recent_msg.get(key)
    if existing is not None and size_bytes < _BURST_MIN_FIRST_BYTES and existing[1] >= _BURST_MIN_FIRST_BYTES:
        # Keep the original size and timestamp; refresh the event.
        prev_ts, prev_size, _ = existing
        _user_recent_msg[key] = (prev_ts, prev_size, abort_event)
    else:
        _user_recent_msg[key] = (now, size_bytes, abort_event)
    # Lazy eviction: keep the dict small by dropping entries
    # older than 4x the window. Cheap O(n) scan, n is bounded
    # by the operator's ALLOWED_USER_IDS cardinality.
    expired = [k for k, (ts, _, _) in _user_recent_msg.items() if now - ts > _BURST_WINDOW_SEC * 4]
    for k in expired:
        _user_recent_msg.pop(k, None)

