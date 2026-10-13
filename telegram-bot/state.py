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


# === _split_pending: Telegram 4096-char split workaround ===
# Telegram caps single text messages at 4096 characters; long
# prompts are auto-split by the client at sentence boundaries
# (typically 4060-4096 bytes per chunk). The bot handles each
# chunk as an independent LLM turn, which produces 2 unrelated
# half-answers instead of one full answer.
#
# Workaround: when the first message from a (chat, user) has
# length in [4060, 4096] bytes, mark (chat_id, user_id) as
# "pending split" and refuse to dispatch the message and any
# subsequent consecutive messages from the same (chat, user)
# to the LLM. Instead reply with a "your prompt may have been
# split by Telegram" error and discard. The pending state has
# a TTL of 30 s so a user typing a single long message and not
# following up does not stay locked out forever.
#
# This is a heuristic, not a guarantee. False positives are
# possible (a user just happened to write a 4070-char message
# with a follow-up). The probability is low because the window
# 4060-4096 only fires on messages close to Telegram's hard
# 4096-byte per-message ceiling, and the operator's golden
# prompts that triggered the fix were in that range. Documented
# in docs/KNOWN-LIMITATIONS.md.
import time
_SPLIT_TTL_SECONDS = 30.0
_SPLIT_WINDOW_MIN = 4060
_SPLIT_WINDOW_MAX = 4096
_split_pending: "OrderedDict[tuple[int, int], float]" = OrderedDict()


def _mark_split_pending(chat_id: int, user_id: int) -> float:
    """Record that (chat, user) just sent a near-4096-char message
    that looks like the first half of a Telegram auto-split. Returns
    the timestamp recorded.

    Also lazily evicts ALL expired entries in _split_pending
    (excluding the just-marked key). This is O(n) where n is the
    number of distinct (chat_id, user_id) pairs currently in the
    dict; bounded by the operator's ALLOWED_USER_IDS cardinality.
    Without the eviction sweep, a stale entry written by an
    expired test or a long-idle user would never be cleaned up
    by _is_split_pending() if no one queries that key again.
    """
    key = (chat_id, user_id)
    ts = time.monotonic()
    _split_pending[key] = ts
    expired = [k for k, t in _split_pending.items() if k != key and t + _SPLIT_TTL_SECONDS < ts]
    for k in expired:
        _split_pending.pop(k, None)
    return ts


def _is_split_pending(chat_id: int, user_id: int) -> bool:
    """True if (chat, user) recently sent a 4060-4096 byte message
    and the TTL has not yet expired. Caller should refuse to
    dispatch any further messages from this (chat, user) until
    the pending state expires.

    Lazily evicts ALL expired entries from _split_pending (O(n)
    scan, n is small because the LRU insert in _mark_split_pending
    is bounded by chat/user cardinality).
    """
    key = (chat_id, user_id)
    ts = _split_pending.get(key)
    if ts is None:
        # Even if our key is not present, evict expired entries
        # so a flood of short messages does not grow the dict.
        now = time.monotonic()
        expired = [k for k, t in _split_pending.items() if t + _SPLIT_TTL_SECONDS < now]
        for k in expired:
            _split_pending.pop(k, None)
        return False
    now = time.monotonic()
    if ts + _SPLIT_TTL_SECONDS < now:
        _split_pending.pop(key, None)
        return False
    return True


def _clear_split_pending(chat_id: int, user_id: int) -> None:
    """Drop the pending-split state for (chat, user). Called when
    the user runs /reset or /start, or any time we want to
    explicitly un-stick them.
    """
    _split_pending.pop((chat_id, user_id), None)
