# tests/test_selftest.py
import asyncio
import os
import re
from unittest.mock import MagicMock

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
    """bot.store must BE the singleton storage.get_store() hands out.

    bot.store is the one object the fork surface still exposes, because
    forks read it. So unlike the other tests in this file, this one
    deliberately goes through bot.
    """
    import bot
    from storage import get_store

    assert bot.store is get_store(), (
        f"bot.store (id={id(bot.store)}) is not get_store() (id={id(get_store())})"
    )
    assert hasattr(bot.store, "create_thread")
    assert hasattr(bot.store, "add_message")
    assert hasattr(bot.store, "get_messages")


def test_constants_exposed():
    """Verify the cross-module state constants are exposed on state.

    TECH-DEBT #1: these were asserted on `bot`, which only re-exported them.
    Reading them off the compatibility surface tested the re-export, not the
    constant -- and a re-export that silently stopped happening would have
    failed this test while production carried on.
    """
    import state

    assert hasattr(state, "_abort_events"), "state._abort_events missing"
    assert hasattr(state, "_register_abort_event"), "state._register_abort_event missing"
    assert hasattr(state, "_GLOBAL_LLM_SEM_LIMIT"), "state._GLOBAL_LLM_SEM_LIMIT missing"
    assert hasattr(state, "_PER_USER_SEMAPHORE_LIMIT"), "state._PER_USER_SEMAPHORE_LIMIT missing"


def test_f4_lru_bound():
    """F4 (v0.6.0 P0-1) regression test."""
    import asyncio
    import state

    # The mutation has to land on state, which is where the OrderedDict lives
    # and where _register_abort_event reads it. state mutates the dict in
    # place rather than rebinding the global, so bot._abort_events used to be
    # the same object -- but asserting through the facade would have kept
    # passing if state ever started rebinding.
    saved = dict(state._abort_events)
    state._abort_events.clear()
    try:
        for i in range(250):
            state._register_abort_event(0, i, asyncio.Event())
        assert len(state._abort_events) == 200, (
            f"LRU bound failed: expected 200, got {len(state._abort_events)}"
        )
        assert (0, 0) not in state._abort_events, "LRU should have evicted (0, 0)"
        assert (0, 200) in state._abort_events, "LRU should preserve newest entry (0, 200)"
    finally:
        state._abort_events.clear()
        state._abort_events.update(saved)


def test_rating_parser_returns_body():
    """v0.6.3 T8 regression test."""
    from rating import _parse_rating_response

    result = _parse_rating_response("plain answer")
    assert "body" in result, f"_parse_rating_response must return 'body' key, got {sorted(result.keys())}"
    assert "rest" not in result, f"_parse_rating_response must NOT return 'rest' key, got {sorted(result.keys())}"
    assert result["body"] == "plain answer"


def test_rating_parser_handles_unknown_type_as_question():
    """T28 (v0.9.5): 2 pre-existing in-tree selftest failures.
    The parser previously returned type='unknown' for both
    unprefixed text and an explicit '[[TYPE:unknown]]' prefix,
    but the in-tree selftest expected type='question' in both
    cases. The fix: map unknown to 'question' (sane default)
    and return the whole text as body (not just post-prefix).
    """

    from rating import _parse_rating_response

    # Case 1: unprefixed text -> question, whole text as body
    r1 = _parse_rating_response("plain answer")
    assert r1["type"] == "question", f"unprefixed: expected 'question', got {r1['type']!r}"
    assert r1["body"] == "plain answer"
    assert r1["rating"] is None

    # Case 2: explicit '[[TYPE:unknown]]' -> question, whole text as body
    r2 = _parse_rating_response("[[TYPE:unknown]]")
    assert r2["type"] == "question", f"unknown type: expected 'question', got {r2['type']!r}"
    assert r2["body"] == "[[TYPE:unknown]]", f"expected whole text as body, got {r2['body']!r}"
    assert r2["rating"] is None

    # Case 3: empty string -> question, empty body (no crash)
    r3 = _parse_rating_response("")
    assert r3["type"] == "question"
    assert r3["body"] == ""
    assert r3["rating"] is None

    # Case 4: valid prefix still works (regression)
    r4 = _parse_rating_response("[[TYPE:info]] [[RATE:7]]")
    assert r4["type"] == "info"
    assert r4["rating"] == 7
    assert r4["emoji"]  # non-empty

# === test_delete_last_user_message ===
@pytest.mark.asyncio
async def test_delete_last_user_message_removes_persisted_image(tmp_path):
    """v0.9.2 regression test: write user message, delete it, assert gone."""
    from storage import Storage
    db = str(tmp_path / "test.db")
    s = Storage(db)
    s.create_thread(1, "main")
    s.add_message(1, "main", "user", "What's the weather?", sender_name="test")
    rows = s.get_messages(1, "main", limit=10)
    assert len(rows) == 1
    assert rows[0]["content"] == "What's the weather?"
    ok = s.delete_last_user_message(1, "main")
    assert ok is True
    rows = s.get_messages(1, "main", limit=10)
    assert rows == [], f"row not deleted, got {rows}"


@pytest.mark.asyncio
async def test_delete_last_user_message_returns_false_when_empty(tmp_path):
    """Empty thread: delete returns False (no row to delete)."""
    from storage import Storage
    db = str(tmp_path / "test.db")
    s = Storage(db)
    s.create_thread(1, "main")
    ok = s.delete_last_user_message(1, "main")
    assert ok is False


@pytest.mark.asyncio
async def test_delete_last_user_message_only_removes_user_role(tmp_path):
    """The function must NOT remove a 'bot' or 'tool' row even if it's
    the most recent message. This protects the LLM response from being
    accidentally deleted by an abort rollback."""
    from storage import Storage
    db = str(tmp_path / "test.db")
    s = Storage(db)
    s.create_thread(1, "main")
    s.add_message(1, "main", "user", "user msg", sender_name="test")
    s.add_message(1, "main", "bot", "bot reply", sender_name="bot")
    s.add_message(1, "main", "tool", "tool output", sender_name="tool")
    ok = s.delete_last_user_message(1, "main")
    assert ok is True
    rows = s.get_messages(1, "main", limit=10)
    # The 'tool' row stays (most recent but not 'user' role)
    # The 'user' row is gone
    # The 'bot' row stays
    assert len(rows) == 2
    roles = {r["role"] for r in rows}
    assert "user" not in roles
    assert "bot" in roles
    assert "tool" in roles


# === test_webhook_check_secret ===
def _make_request(headers):
    """Build a minimal aiohttp.web.Request mock for _check_secret."""
    import aiohttp.web
    request = MagicMock(spec=aiohttp.web.Request)
    request.headers.get = lambda name, default="": headers.get(name, default)
    return request


def test_webhook_check_secret_accepts_valid_token(monkeypatch):
    """v0.9.2 regression test: when X-Telegram-Bot-Api-Secret-Token
    matches WEBHOOK_SECRET_TOKEN, _check_secret returns True."""
    monkeypatch.setattr("config.WEBHOOK_SECRET_TOKEN", "deadbeef-1234")
    import webhook
    req = _make_request({"X-Telegram-Bot-Api-Secret-Token": "deadbeef-1234"})
    assert webhook._check_secret(req) is True


def test_webhook_check_secret_rejects_invalid_token(monkeypatch):
    """v0.9.2 regression test: when the header doesn't match,
    _check_secret returns False."""
    monkeypatch.setattr("config.WEBHOOK_SECRET_TOKEN", "deadbeef-1234")
    import webhook
    req = _make_request({"X-Telegram-Bot-Api-Secret-Token": "not-the-right-token"})
    assert webhook._check_secret(req) is False


def test_webhook_check_secret_rejects_missing_token(monkeypatch):
    """v0.9.2 regression test: when the header is absent, returns False."""
    monkeypatch.setattr("config.WEBHOOK_SECRET_TOKEN", "deadbeef-1234")
    import webhook
    req = _make_request({})  # no headers at all
    assert webhook._check_secret(req) is False


def test_webhook_check_secret_rejects_when_unconfigured(monkeypatch):
    """v0.9.2 regression test: when WEBHOOK_SECRET_TOKEN is empty
    (misconfigured bot), every request is refused."""
    monkeypatch.setattr("config.WEBHOOK_SECRET_TOKEN", "")
    import webhook
    req = _make_request({"X-Telegram-Bot-Api-Secret-Token": "anything"})
    assert webhook._check_secret(req) is False
