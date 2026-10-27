"""The empty-answer fallback reaches the user (T-FREEZE-EMPTY-1).

The model sometimes spends its whole budget on reasoning and writes
nothing. `call_llama` then returns a diagnostic -- a technical string
about the model's internals, with the reasoning tail attached -- and the
handlers retry once. When the retry is empty too, the fallback sentence
is what the user should read.

It never did. The retry branch assigned the fallback to `bot_response`,
and then `_persist_for_reply` returned the answer's prose over it. The
assignment was thrown away in the same breath, and the chat got the
diagnostic: on the production bot, 3618 characters of it, with a
45108-character reasoning tail behind it (assistant turn 10369,
2026-10-24).

The decision (the operator delegated it on 2026-10-27, "делай с 6 по
10"): the chat gets the sentence, the record keeps the diagnostic. That
is the rule the rest of the bot already follows -- `user_notice.py`: the
chat gets a sentence the user can act on, the detail goes to the log.

These tests drive the real `handle_text` with only the engine replaced,
because the defect was in the wiring: a unit test of the resolver, or of
the fallback constant, would have been green the whole time.
"""
import ast
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

from config import EMPTY_RESPONSE_PREFIX, EMPTY_RESPONSE_USER_FALLBACK  # noqa: E402
from llm_finish import finish_without_tool_calls  # noqa: E402


class _Msg:
    """A sent message that records what happened to it."""

    def __init__(self, message_id=999):
        self.message_id = message_id
        self.edits = []
        self.replies = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return _Msg(message_id=self.message_id + 1)

    async def delete(self):
        pass


def _store_on(monkeypatch, tmp_path):
    """Fresh DB, wired to every owner that resolves it."""
    import config as _cfg
    import storage as _st
    from storage import Storage

    monkeypatch.setattr(_cfg, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(_st, "_store_instance", None)
    store = Storage(str(tmp_path / "t.db"))
    monkeypatch.setattr(_st, "get_store", lambda: store)
    import turn as _turn
    monkeypatch.setattr(_turn, "storage", _st)
    store.create_thread(1, "main")
    return store


def _update(text="hello"):
    u = MagicMock()
    u.effective_user.id = 1
    chat = MagicMock()
    chat.id = 1
    chat.type = "private"
    chat.send_action = AsyncMock()
    u.effective_chat = chat
    u.message.chat = chat
    u.message.message_thread_id = None
    u.message.message_id = 500
    u.message.text = text
    u.message.from_user = MagicMock(id=1, first_name="v", last_name=None,
                                    username=None)
    return u


def _context():
    c = MagicMock()
    c.bot = MagicMock()
    return c


def _harness(monkeypatch, tmp_path, engine):
    """Everything `handle_text` needs, so each test states only its part."""
    import handlers

    _store_on(monkeypatch, tmp_path)
    sent = _Msg()

    async def fake_route(update, context):
        return (1, "main", False)

    sem = asyncio.Semaphore(99)

    async def fake_slot(chat_id, user_id, update):
        await sem.acquire()
        return sem

    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    monkeypatch.setattr("handlers._check_user_slot", fake_slot)
    monkeypatch.setattr("handlers._is_rating_active", lambda u: False)
    monkeypatch.setattr("handlers._stop_button_markup", lambda: None)
    monkeypatch.setattr("handlers._register_abort_event",
                        lambda cid, mid, ev: None)
    monkeypatch.setattr("handlers.call_llama", engine)
    monkeypatch.setattr("handlers.Update", MagicMock())

    async def fake_reply(update, text, **kw):
        sent.replies.append(text)
        return sent

    monkeypatch.setattr("handlers._reply", fake_reply)

    async def fake_send_reply(update, text, **kw):
        sent.replies.append(text)

    monkeypatch.setattr("handlers.send_reply", fake_send_reply)
    return handlers, sent


def _diagnostic():
    """The real producer's answer, not a string written out here."""
    return finish_without_tool_calls(
        content_buf="", reasoning_buf="thinking about the schema " * 200,
        finish_reason="length", executed_tools=[])


async def _turn(monkeypatch, tmp_path, engine):
    handlers, sent = _harness(monkeypatch, tmp_path, engine)
    await handlers.handle_text(_update(), _context())
    return sent


@pytest.mark.asyncio
async def test_the_user_gets_the_fallback_and_not_the_diagnostic(monkeypatch,
                                                                  tmp_path):
    """The defect: the diagnostic reached the chat, and the fallback was
    assigned and thrown away in the same breath."""
    diagnostic = _diagnostic()
    calls = []

    async def engine(*args, **kwargs):
        calls.append(1)
        return diagnostic

    sent = await _turn(monkeypatch, tmp_path, engine)

    assert len(calls) == 2, (
        f"the empty answer was not retried ({len(calls)} call(s))")
    said = " ".join(sent.replies)
    assert EMPTY_RESPONSE_USER_FALLBACK in said, (
        f"the user did not get the fallback; they got: {said!r}")
    assert diagnostic not in said, (
        "the diagnostic is in the chat -- it is a technical string about "
        "the model's internals, and it can be three kilobytes of it")


@pytest.mark.asyncio
async def test_the_record_keeps_the_diagnostic(monkeypatch, tmp_path):
    """What happened is what the bot remembers. The fallback is what the
    user reads; it is not what happened."""
    diagnostic = _diagnostic()

    async def engine(*args, **kwargs):
        return diagnostic

    await _turn(monkeypatch, tmp_path, engine)

    import storage
    rows = storage.get_store().get_messages(1, "main", limit=20)
    assistant = [json.loads(r["content"]).get("content")
                 for r in rows if json.loads(r["content"]).get("role") == "assistant"]
    assert assistant, "the turn was not remembered at all"
    assert assistant[0] == diagnostic, (
        f"the record holds {assistant[0][:120]!r} instead of the diagnostic")


@pytest.mark.asyncio
async def test_a_normal_answer_is_untouched(monkeypatch, tmp_path):
    """A user who asked a question must not get a different answer
    because a failure path sits next to it."""
    async def engine(*args, **kwargs):
        return {"text": "the answer", "executed_tools": [], "aborted": False}

    sent = await _turn(monkeypatch, tmp_path, engine)

    said = " ".join(sent.replies)
    assert "the answer" in said, f"the answer did not reach the user: {said!r}"
    assert EMPTY_RESPONSE_USER_FALLBACK not in said, (
        "the fallback was sent to a user who got an answer")


@pytest.mark.asyncio
async def test_the_retry_saved_the_turn(monkeypatch, tmp_path):
    """The retry is still the first line of defence: when the second
    attempt works, the user gets the answer and not the fallback."""
    diagnostic = _diagnostic()
    calls = []

    async def engine(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return diagnostic
        return {"text": "second answer", "executed_tools": [], "aborted": False}

    sent = await _turn(monkeypatch, tmp_path, engine)

    said = " ".join(sent.replies)
    assert "second answer" in said, f"the retry answer never arrived: {said!r}"
    assert EMPTY_RESPONSE_USER_FALLBACK not in said, (
        "the fallback was sent even though the retry worked")


def test_the_fallback_is_a_sentence_and_not_a_diagnostic():
    """Control on the constant itself: the text the user reads must not
    carry the internal marker."""
    assert not EMPTY_RESPONSE_USER_FALLBACK.startswith(EMPTY_RESPONSE_PREFIX), (
        "the fallback carries the internal marker -- it would be the "
        "diagnostic by another name")
    assert "/reset" in EMPTY_RESPONSE_USER_FALLBACK, (
        "the fallback has to say what the user can do about it")


def test_the_fallback_is_chosen_in_one_place():
    """Structural. The retry branch used to assign the fallback to
    `bot_response`, and the next statement returned the answer's prose
    over it -- the assignment was thrown away in the same breath. That
    is how the diagnostic reached the chat for a year.

    So the fallback is chosen in exactly one place, and the name appears
    there once. A second assignment is not a harmless extra: it is the
    same defect with a different indentation.
    """
    tree = ast.parse((ROOT / "telegram-bot" / "handlers.py")
                     .read_text(encoding="utf-8"))
    uses = [node for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and node.id == "EMPTY_RESPONSE_USER_FALLBACK"]
    assert len(uses) == 1, (
        f"EMPTY_RESPONSE_USER_FALLBACK is used {len(uses)} times in "
        "handlers.py; the fallback is chosen in one place, and an "
        "assignment that is later overwritten is the defect this file "
        "exists for")

    owner = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(u is uses[0] for u in ast.walk(node)):
                owner = node.name
                break
    assert owner == "_persist_for_reply", (
        f"the fallback is chosen in {owner!r}; the seam that decides what "
        "the chat gets is _persist_for_reply")
