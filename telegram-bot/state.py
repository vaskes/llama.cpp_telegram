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


# Per-(chat_id, user_id) semaphore. Keys are tuples so the same
# user in different chats, or different users in the same chat,
# each get their own slot. The semaphore is created lazily in
# _get_user_semaphore() to avoid a top-level asyncio.Semaphore
# before the event loop is running.
_user_semaphores: "dict[tuple[int, int], asyncio.Semaphore]" = {}


def _get_user_semaphore(chat_id: int, user_id: int) -> asyncio.Semaphore:
    """Return the per-(chat, user) semaphore, creating it on first call."""
    key = (chat_id, user_id)
    sem = _user_semaphores.get(key)
    if sem is None:
        from config import _PER_USER_SEMAPHORE_LIMIT
        sem = asyncio.Semaphore(_PER_USER_SEMAPHORE_LIMIT)
        _user_semaphores[key] = sem
    return sem
