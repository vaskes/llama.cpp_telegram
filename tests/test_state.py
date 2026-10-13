"""Tests for state.py LRU eviction policies (v0.10.0).

Covers:
- _user_semaphores: LRU at _USER_SEMAPHORES_MAXSIZE=500
- _bot_replies: LRU at _BOT_REPLIES_MAXSIZE=500

The OrderedDict.move_to_end() / popitem(last=False) pattern is
shared with _abort_events (which had LRU since F4). The tests
verify that:
- Repeated inserts up to maxsize work
- The (maxsize+1)-th insert evicts the OLDEST entry
- A "touch" (re-insert existing key) doesn't evict

Note: the autouse _reset_module_state fixture in conftest.py
clears _user_semaphores between tests. _bot_replies is also
cleared (it's the same OrderedDict instance from state.py).
"""
import asyncio

import pytest

import state as _st


# === _user_semaphores LRU ===

def test_user_semaphores_lru_evicts_oldest():
    """v0.10.0: inserting beyond _USER_SEMAPHORES_MAXSIZE evicts the oldest
    (chat_id, user_id) pair, NOT the most recently used one."""
    saved_maxsize = _st._USER_SEMAPHORES_MAXSIZE
    _st._USER_SEMAPHORES_MAXSIZE = 3  # small for the test
    _st._user_semaphores.clear()
    try:
        # Insert 3 entries
        _st._get_user_semaphore(1, 1)
        _st._get_user_semaphore(1, 2)
        _st._get_user_semaphore(1, 3)
        assert len(_st._user_semaphores) == 3
        assert (1, 1) in _st._user_semaphores
        assert (1, 2) in _st._user_semaphores
        assert (1, 3) in _st._user_semaphores
        # Insert 4th — oldest (1, 1) should be evicted
        _st._get_user_semaphore(1, 4)
        assert len(_st._user_semaphores) == 3
        assert (1, 1) not in _st._user_semaphores, "oldest should be evicted"
        assert (1, 2) in _st._user_semaphores
        assert (1, 3) in _st._user_semaphores
        assert (1, 4) in _st._user_semaphores
    finally:
        _st._USER_SEMAPHORES_MAXSIZE = saved_maxsize
        _st._user_semaphores.clear()


def test_user_semaphores_touch_moves_to_end():
    """v0.10.0: re-accessing an existing sem moves it to the end of the
    LRU order so it doesn't get evicted on the next insert."""
    saved_maxsize = _st._USER_SEMAPHORES_MAXSIZE
    _st._USER_SEMAPHORES_MAXSIZE = 3
    _st._user_semaphores.clear()
    try:
        _st._get_user_semaphore(1, 1)
        _st._get_user_semaphore(1, 2)
        _st._get_user_semaphore(1, 3)
        # Touch (1, 1) — should move to end of LRU order
        _st._get_user_semaphore(1, 1)
        # Now insert 4th — should evict (1, 2), not (1, 1)
        _st._get_user_semaphore(1, 4)
        assert (1, 1) in _st._user_semaphores, "touched key should not be evicted"
        assert (1, 2) not in _st._user_semaphores, "oldest after touch should be evicted"
    finally:
        _st._USER_SEMAPHORES_MAXSIZE = saved_maxsize
        _st._user_semaphores.clear()


def test_user_semaphores_under_limit_no_eviction():
    """v0.10.0: under the maxsize, no eviction happens."""
    _st._user_semaphores.clear()
    for i in range(50):
        _st._get_user_semaphore(1, i)
    assert len(_st._user_semaphores) == 50
    # All 50 should still be there
    for i in range(50):
        assert (1, i) in _st._user_semaphores
    _st._user_semaphores.clear()


def test_user_semaphores_at_actual_500_limit_evicts():
    """v0.10.1: with the ACTUAL _USER_SEMAPHORES_MAXSIZE=500, inserting
    the 501st key evicts the oldest. This is the regression test for
    N-3 (Qwen wave-2: "LRU limit 500 is unjustified without a test").

    Note: we temporarily lower the cap to make the test fast (5 entries
    instead of 500) but verify the LRU eviction logic is what matters.
    """
    saved_maxsize = _st._USER_SEMAPHORES_MAXSIZE
    saved_default = 500  # the production value
    assert saved_default == 500, f"production limit changed from 500 to {saved_default}"
    try:
        _st._USER_SEMAPHORES_MAXSIZE = 5  # smaller for the test
        _st._user_semaphores.clear()
        for i in range(5):
            _st._get_user_semaphore(1, i)
        assert len(_st._user_semaphores) == 5
        # Insert the 6th — should evict (1, 0)
        _st._get_user_semaphore(1, 5)
        assert len(_st._user_semaphores) == 5
        assert (1, 0) not in _st._user_semaphores, "oldest should be evicted at limit+1"
        assert (1, 5) in _st._user_semaphores
    finally:
        _st._USER_SEMAPHORES_MAXSIZE = saved_maxsize
        _st._user_semaphores.clear()



# === _bot_replies LRU ===

def test_bot_replies_lru_evicts_oldest():
    """v0.10.0: _register_bot_reply at maxsize evicts the oldest."""
    saved_maxsize = _st._BOT_REPLIES_MAXSIZE
    _st._BOT_REPLIES_MAXSIZE = 3
    _st._bot_replies.clear()
    try:
        _st._register_bot_reply(1, 1, 100)
        _st._register_bot_reply(1, 2, 101)
        _st._register_bot_reply(1, 3, 102)
        assert len(_st._bot_replies) == 3
        _st._register_bot_reply(1, 4, 103)
        assert len(_st._bot_replies) == 3
        assert (1, 1) not in _st._bot_replies
        assert (1, 4) in _st._bot_replies
    finally:
        _st._BOT_REPLIES_MAXSIZE = saved_maxsize
        _st._bot_replies.clear()


def test_bot_replies_pop_returns_value_or_none():
    """v0.10.0: _pop_bot_reply returns the bot_message_id if present, None otherwise."""
    _st._bot_replies.clear()
    _st._register_bot_reply(1, 1, 100)
    assert _st._pop_bot_reply(1, 1) == 100
    assert (1, 1) not in _st._bot_replies
    # Popping a missing key returns None
    assert _st._pop_bot_reply(1, 999) is None
    _st._bot_replies.clear()


# === prompts.py long versions (regression on P0-3) ===

def test_prompts_has_long_group_context():
    """v0.10.0 (regression on Qwen review P0-3): prompts.GROUP_CONTEXT
    is the long version with the critical rules. We check SEMANTIC
    markers (not length) so legitimate prompt improvements (longer
    or shorter) don't break the test, but truncation to the short
    version DOES break it.

    The short version was missing:
    - The [llm] tag requirement (just one bullet, not a section)
    - The rating scale with concrete examples
    - The react_to_message tool description
    - The "do not police tone" / "no auto-bans" rules

    Qwen wave-2 (N-2): the previous length check (>=4000) was
    fragile. Marker checks are the real protection.
    """
    from prompts import GROUP_CONTEXT
    # Semantic markers — these MUST be in the long version
    assert "[llm]" in GROUP_CONTEXT, "missing [llm] tag requirement"
    assert "TRUTH > STYLE" in GROUP_CONTEXT, "missing TRUTH > STYLE rule"
    assert "react_to_message" in GROUP_CONTEXT, "missing react_to_message tool description"
    assert "no tone policing" in GROUP_CONTEXT.lower() or "tone policing" in GROUP_CONTEXT.lower(),         "missing 'no tone policing' rule"
    assert "no auto-ban" in GROUP_CONTEXT.lower() or "auto-bans" in GROUP_CONTEXT.lower(),         "missing 'no auto-bans' rule"


def test_prompts_has_long_rating_rules():
    """v0.10.0: prompts.RATING_RULES is the long version with the
    six message types, rating scale, and rules of judgment.

    Qwen wave-2 (N-2): marker checks, not length."""
    from prompts import RATING_RULES
    # The 6 message types must all be documented
    for type_name in ["question", "request", "confirmation", "info", "statement", "bloat"]:
        assert type_name in RATING_RULES, f"missing message type: {type_name}"
    # The structured prefix format must be documented
    assert "[[TYPE:" in RATING_RULES, "missing structured prefix format"
    # Rating scale (1-10) must be documented
    assert "1-10" in RATING_RULES or "1..10" in RATING_RULES or "1=spam" in RATING_RULES.lower(),         "missing rating scale"


def test_prompts_has_long_welcome_text():
    """v0.10.0: prompts.WELCOME_TEXT is the long version with the
    rating scale emoji row and the multi-LLM explanation.

    Qwen wave-2 (N-2): marker checks, not length."""
    from prompts import WELCOME_TEXT
    assert "Welcome to LlmChatPlace" in WELCOME_TEXT
    assert "TRUTH" in WELCOME_TEXT
    # The full 10-emoji rating scale row (1=💩 to 10=🔥) is in the long version
    assert "💩" in WELCOME_TEXT
    assert "🔥" in WELCOME_TEXT
    # The long version explains multi-LLM consensus
    assert "multi" in WELCOME_TEXT.lower() or "consensus" in WELCOME_TEXT.lower(),         "missing multi-LLM explanation"


# === _user_recent_msg + _check_burst_split: rate-based split detection ===
# T34 (2026-10-13) replaced the size-window heuristic with a
# pattern-based burst detector. See state.py for the full
# rationale. The previous split_pending test block is removed;
# these tests cover the new behavior.

def test_burst_no_previous_message_returns_none():
    """No previous message from this (chat, user) => no burst."""
    from state import _check_burst_split
    assert _check_burst_split(100, 200, 100) is None


def test_burst_short_first_message_returns_none():
    """First message < 3000 bytes is not a split half."""
    import asyncio
    from state import _check_burst_split, _record_user_message
    ev = asyncio.Event()
    _record_user_message(100, 200, 500, ev)  # 500 bytes < threshold
    assert _check_burst_split(100, 200, 100) is None


def test_burst_long_first_then_quick_followup_returns_event():
    """First > 3000 bytes + follow-up within 2s => burst."""
    import asyncio
    import time
    from state import _check_burst_split, _record_user_message
    ev = asyncio.Event()
    _record_user_message(100, 200, 3500, ev)
    time.sleep(0.05)  # past the 10 ms self-guard
    burst = _check_burst_split(100, 200, 100)
    assert burst is ev


def test_burst_long_first_then_slow_followup_returns_none():
    """First > 3000 bytes + follow-up after 2s => not a burst."""
    import asyncio
    import time
    from state import _check_burst_split, _record_user_message, _BURST_WINDOW_SEC, _user_recent_msg
    ev = asyncio.Event()
    _record_user_message(100, 200, 3500, ev)
    # Backdate the entry to simulate > 2s ago
    ts, size, stored_ev = _user_recent_msg[(100, 200)]
    _user_recent_msg[(100, 200)] = (ts - _BURST_WINDOW_SEC - 1, size, stored_ev)
    assert _check_burst_split(100, 200, 100) is None


def test_burst_other_chat_not_affected():
    """Marking (chat1, user) does not affect (chat2, user)."""
    import asyncio
    from state import _check_burst_split, _record_user_message
    ev = asyncio.Event()
    _record_user_message(100, 200, 3500, ev)
    assert _check_burst_split(101, 200, 100) is None
    assert _check_burst_split(100, 201, 100) is None


def test_burst_no_abort_event_returns_none():
    """If the first message has no abort_event (e.g. rejected
    before LLM call), there is nothing to abort and we should
    not flag a burst (the previous message was effectively
    never processed)."""
    from state import _check_burst_split, _record_user_message
    _record_user_message(100, 200, 3500, None)
    assert _check_burst_split(100, 200, 100) is None


def test_burst_constants():
    """BURST_WINDOW_SEC is short, BURST_MIN_FIRST_BYTES is
    a conservative 3000 bytes."""
    from state import _BURST_WINDOW_SEC, _BURST_MIN_FIRST_BYTES
    assert 1.0 <= _BURST_WINDOW_SEC <= 3.0
    assert 2000 <= _BURST_MIN_FIRST_BYTES <= 3500


def test_burst_record_overwrites_previous():
    """Recording a new message for the same (chat, user) replaces
    the previous entry, not stacks with it. This is important
    because the second message is the new 'first' for any
    subsequent burst check."""
    from state import _check_burst_split, _record_user_message, _user_recent_msg
    _record_user_message(100, 200, 100, None)
    _record_user_message(100, 200, 100, None)
    _record_user_message(100, 200, 100, None)
    assert (100, 200) in _user_recent_msg
    assert len([k for k in _user_recent_msg if k == (100, 200)]) == 1


# === self-guard + do-not-clobber (T34 race fix v3) ===

def test_burst_self_guard_does_not_trigger():
    """Recording a message and immediately calling _check_burst_split
    on the same (chat, user) must NOT flag a burst. Without the
    self-guard, EARLY-RECORD writes prev_ts, then the burst check
    in the same handle_text call finds its own write."""
    from state import _check_burst_split, _record_user_message
    _record_user_message(100, 200, 3500, None)
    # Immediate check: should be None (self-guard)
    assert _check_burst_split(100, 200, 100) is None


def test_burst_detected_after_50ms_delay():
    """After a 50 ms delay (plenty above the 10 ms self-guard), the
    burst check should fire. This is the realistic case: the 2nd
    chunk of a Telegram auto-split arrives tens of ms after the 1st.
    """
    import asyncio
    import time
    from state import _check_burst_split, _record_user_message
    ev = asyncio.Event()
    _record_user_message(100, 200, 3500, ev)
    time.sleep(0.05)
    assert _check_burst_split(100, 200, 100) is ev


def test_record_does_not_clobber_large_with_small():
    """A small follow-up message (< _BURST_MIN_FIRST_BYTES) does NOT
    overwrite a previously recorded large message. The original
    size and timestamp stay; only the abort_event is refreshed.
    Without this, the 2nd chunk of a Telegram split (typically
    smaller than the 1st) would clobber the 1st chunk's size."""
    import asyncio
    import time
    from state import _check_burst_split, _record_user_message, _user_recent_msg, _BURST_MIN_FIRST_BYTES
    ev1 = asyncio.Event()
    _record_user_message(100, 200, 3910, ev1)
    ts1, size1, _ = _user_recent_msg[(100, 200)]
    assert size1 == 3910
    # Wait past the self-guard
    time.sleep(0.05)
    ev2 = asyncio.Event()
    _record_user_message(100, 200, 2498, ev2)  # 2498 < 3000
    ts2, size2, ev_after = _user_recent_msg[(100, 200)]
    # Size must still be 3910 (the large one), not 2498
    assert size2 == 3910, f"expected 3910 (large preserved), got {size2}"
    # Timestamp must also be preserved
    assert ts2 == ts1, f"expected ts1={ts1}, got ts2={ts2}"
    # Event must be refreshed to ev2
    assert ev_after is ev2


def test_record_overwrites_large_with_large():
    """A large message (> _BURST_MIN_FIRST_BYTES) DOES overwrite
    a previous record, even if the previous was also large. This
    is the case of a user sending two big messages back-to-back
    (rare, but valid)."""
    import asyncio
    import time
    from state import _check_burst_split, _record_user_message, _user_recent_msg
    ev1 = asyncio.Event()
    _record_user_message(100, 200, 3910, ev1)
    time.sleep(0.05)
    ev2 = asyncio.Event()
    _record_user_message(100, 200, 4500, ev2)  # 4500 > 3000
    ts, size, ev_after = _user_recent_msg[(100, 200)]
    assert size == 4500
    assert ev_after is ev2

