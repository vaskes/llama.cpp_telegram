# tests/test_selftest.py
import asyncio
import os
import re

import pytest


@pytest.mark.asyncio
async def test_in_tree_selftest(caplog, capsys):
    """Run the full in-tree _selftest() as a single pytest test."""
    db_path = os.environ.get("CONVERSATIONS_DB", "/tmp/pytest_conversations.db")
    if os.path.exists(db_path):
        os.unlink(db_path)

    import bot as _b

    await _b._selftest()

    captured = capsys.readouterr()
    out = captured.out

    failures = re.findall(r"\[FAIL\].*", out)
    if failures:
        pytest.fail(
            f"in-tree _selftest() reported {len(failures)} failures:\n"
            + "\n".join(failures[:10])
        )

    if "Traceback (most recent call last):" in out:
        if "ZeroDivisionError" in out or "ImportError" in out:
            pytest.fail("in-tree _selftest() had unhandled exceptions:\n" + out[-1000:])


def test_store_singleton():
    """Verify bot.store and storage.get_store() return the same instance."""
    import bot
    from storage import get_store

    assert bot.store is get_store(), (
        f"bot.store (id={id(bot.store)}) is not get_store() (id={id(get_store())})"
    )
    assert hasattr(bot.store, "create_thread")
    assert hasattr(bot.store, "add_message")
    assert hasattr(bot.store, "get_messages")


def test_constants_exposed():
    """Verify the cross-module state constants are exposed on bot."""
    import bot

    assert hasattr(bot, "_abort_events"), "bot._abort_events missing"
    assert hasattr(bot, "_bot_replies"), "bot._bot_replies missing"
    assert hasattr(bot, "_register_abort_event"), "bot._register_abort_event missing"
    assert hasattr(bot, "_GLOBAL_LLM_SEM_LIMIT"), "bot._GLOBAL_LLM_SEM_LIMIT missing"
    assert hasattr(bot, "_PER_USER_SEMAPHORE_LIMIT"), "bot._PER_USER_SEMAPHORE_LIMIT missing"


def test_f4_lru_bound():
    """F4 (v0.6.0 P0-1) regression test."""
    import asyncio
    import bot

    saved = dict(bot._abort_events)
    bot._abort_events.clear()
    try:
        for i in range(250):
            bot._register_abort_event(0, i, asyncio.Event())
        assert len(bot._abort_events) == 200, (
            f"LRU bound failed: expected 200, got {len(bot._abort_events)}"
        )
        assert (0, 0) not in bot._abort_events, "LRU should have evicted (0, 0)"
        assert (0, 200) in bot._abort_events, "LRU should preserve newest entry (0, 200)"
    finally:
        bot._abort_events.clear()
        bot._abort_events.update(saved)


def test_rating_parser_returns_body():
    """v0.6.3 T8 regression test."""
    import bot

    result = bot._parse_rating_response("plain answer")
    assert "body" in result, f"_parse_rating_response must return 'body' key, got {sorted(result.keys())}"
    assert "rest" not in result, f"_parse_rating_response must NOT return 'rest' key, got {sorted(result.keys())}"
    assert result["body"] == "plain answer"
