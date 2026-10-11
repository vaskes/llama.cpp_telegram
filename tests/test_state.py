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
    """v0.10.0 (regression on Qwen review P0-3): prompts.GROUP_CONTEXT is
    the LONG version. The long version has critical rules (the [llm] tag
    requirement, TRUTH > STYLE, react_to_message tool description) that
    the short version was missing.
    
    Previously, bot.py had the long version as dead code, and prompts.py
    had a shorter version. Production code (call_llama.py) imports from
    prompts.py, so the LLM was getting the short version.
    """
    from prompts import GROUP_CONTEXT
    assert len(GROUP_CONTEXT) >= 4000, f"GROUP_CONTEXT too short ({len(GROUP_CONTEXT)} chars), expected long version"
    # These are critical rules that must be in the long version
    assert "[llm]" in GROUP_CONTEXT, "missing [llm] tag requirement"
    assert "TRUTH > STYLE" in GROUP_CONTEXT or "TRUTH" in GROUP_CONTEXT, "missing TRUTH > STYLE rule"
    assert "react_to_message" in GROUP_CONTEXT, "missing react_to_message tool description"
    assert "rating" in GROUP_CONTEXT.lower(), "missing rating system description"


def test_prompts_has_long_rating_rules():
    """v0.10.0: prompts.RATING_RULES is the long version with the
    six message types, rating scale, and rules of judgment."""
    from prompts import RATING_RULES
    assert len(RATING_RULES) >= 2000, f"RATING_RULES too short ({len(RATING_RULES)} chars)"
    assert "question" in RATING_RULES
    assert "request" in RATING_RULES
    assert "info" in RATING_RULES
    assert "bloat" in RATING_RULES
    assert "[[TYPE:" in RATING_RULES


def test_prompts_has_long_welcome_text():
    """v0.10.0: prompts.WELCOME_TEXT is the long version with the
    rating scale emoji row and the multi-LLM explanation."""
    from prompts import WELCOME_TEXT
    assert len(WELCOME_TEXT) >= 800, f"WELCOME_TEXT too short ({len(WELCOME_TEXT)} chars)"
    assert "Welcome to LlmChatPlace" in WELCOME_TEXT
    assert "TRUTH" in WELCOME_TEXT
    # The emoji row is in the long version
    assert "💩" in WELCOME_TEXT
    assert "🔥" in WELCOME_TEXT
