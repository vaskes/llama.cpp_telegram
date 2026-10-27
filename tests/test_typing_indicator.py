"""The typing indicator must not be able to cost the user a turn (2026-10-27).

Production, 19:15 UTC: a voice message arrived and the turn died before
the model was ever asked. The traceback names the cause --
`telegram.error.NetworkError: httpx.ConnectError` raised by
`update.message.chat.send_action(action='typing')`, the first thing the
handler does. The next update, 44 seconds later, was served normally, so
the hiccup was over by then: the voice message was lost to a moment of
network trouble, because the handler had asked the API for a decoration
without a guard.

Three of the four handlers had no guard at all; the fourth had one copy
of it inline. This file pins the property, not the shape:

  * a refused indicator does not stop the turn -- the model is still
    asked, and the user still gets an answer;
  * the indicator is still sent. A handler that quietly stopped asking
    would pass the first test, and the user would lose the "typing..."
    hint for good;
  * the refusal is logged, not swallowed silently: an operator who sees
    the user complain about a lost message needs to see the indicator
    fail;
  * a cancellation is not swallowed. `CancelledError` is not a network
    hiccup, and a turn that was cancelled must stay cancelled.

The harness is borrowed from `test_blockers.py` rather than copied: a
second copy of "everything a handler needs" is a second copy that
quietly stops matching the first.
"""
import ast
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from test_blockers import _common, _context, _update

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

HANDLERS = ["handle_photo", "handle_voice", "handle_document", "handle_text"]


class _Refused(Exception):
    """What Telegram's API raised in production: a network error."""


def _handlers_source() -> str:
    return (BOT / "handlers.py").read_text(encoding="utf-8")


def _func(src: str, name: str):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{name} not found in handlers.py")


def _calls(fn, want: str):
    """Line numbers of the calls to `want` inside `fn`."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name == want:
            out.append(node.lineno)
    return sorted(out)


# --- the behaviour -------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("handler", HANDLERS)
async def test_the_turn_survives_a_refused_typing_indicator(
        handler, monkeypatch, tmp_path):
    """The model is still asked after Telegram refused the indicator.

    This is the production failure, reproduced: the refusal arrives
    before anything else the handler does, and on the broken tree the
    turn ends there.
    """
    asked = []

    async def fake_call(*a, **kw):
        asked.append(1)
        return {"text": "the answer", "executed_tools": [], "aborted": False}

    handlers, sent, sem, registered = _common(monkeypatch, tmp_path, fake_call)

    async def refuse(*a, **kw):
        raise _Refused("httpx.ConnectError: ")

    u = _update()
    c = _context()
    u.message.chat.send_action = refuse

    voice = MagicMock()
    voice.file_size = 100
    voice.file_id = "v1"
    voice.duration = 1
    u.message.voice = voice if handler == "handle_voice" else None
    doc = MagicMock()
    doc.file_size = 100
    doc.file_id = "f1"
    u.message.document = doc if handler == "handle_document" else None
    photo = MagicMock()
    photo.file_size = 100
    photo.file_id = "p1"
    u.message.photo = [photo] if handler == "handle_photo" else None
    u.message.caption = "cap" if handler in ("handle_photo",
                                             "handle_document") else None

    await getattr(handlers, handler)(u, c)

    assert asked, (
        f"{handler} gave the turn up when Telegram refused the typing "
        f"indicator. The indicator is decoration; the turn is the work."
    )


@pytest.mark.asyncio
async def test_the_indicator_is_still_sent():
    """The helper asks for the indicator.

    Without this, "the turn survives a refused indicator" is also
    satisfied by a handler that stopped asking for it -- and the user
    loses the hint for good.
    """
    from handlers import _typing_indicator

    asked = []

    class Chat:
        async def send_action(self, **kw):
            asked.append(kw)

    await _typing_indicator(Chat(), label="handle_text")

    assert asked == [{"action": "typing"}], (
        f"the indicator was not asked for: {asked}"
    )


@pytest.mark.asyncio
async def test_a_refused_indicator_is_logged(capsys):
    """The refusal is logged. An operator needs to see it.

    capsys, not caplog: the repo's logger sets `propagate = False`, so
    caplog -- which attaches to the root -- can never see a line from
    it. A check that cannot fail for any code is decoration.
    """
    from handlers import _typing_indicator

    class Chat:
        async def send_action(self, **kw):
            raise _Refused("httpx.ConnectError: ")

    await _typing_indicator(Chat(), label="handle_voice")

    out = capsys.readouterr().out
    assert "send_action FAILED" in out, (
        f"the refusal was swallowed silently: {out!r}"
    )
    assert "handle_voice" in out, (
        f"the log line does not say which turn lost the indicator: {out!r}"
    )


@pytest.mark.asyncio
async def test_a_cancelled_turn_is_not_swallowed():
    """`CancelledError` is not a network hiccup.

    The guard catches `Exception`, so a cancellation propagates and the
    turn stays cancelled. A guard that caught `BaseException` would
    swallow the cancellation and leave the task running.
    """
    from handlers import _typing_indicator

    class Chat:
        async def send_action(self, **kw):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _typing_indicator(Chat(), label="handle_text")


# --- the shape, so the behaviour cannot be bought by accident ------------

@pytest.mark.parametrize("handler", HANDLERS)
def test_every_handler_asks_for_the_indicator_through_the_helper(handler):
    """Each handler asks once, and none of them talks to Telegram itself.

    Four copies of the call is how three of them ended up without a
    guard: the guard was a fix for one handler, and the other three were
    left as they were.
    """
    src = _handlers_source()
    fn = _func(src, handler)
    assert len(_calls(fn, "_typing_indicator")) == 1, (
        f"{handler} asks for the typing indicator "
        f"{len(_calls(fn, '_typing_indicator'))} times; exactly one, "
        f"through the helper"
    )
    assert not _calls(fn, "send_action"), (
        f"{handler} calls send_action itself; the guard lives in "
        f"_typing_indicator, and a second call site is a second place to "
        f"forget it"
    )


def test_the_helper_is_the_only_place_that_talks_to_the_indicator():
    """One call site in the whole file.

    A second one would be a second guard to write, and the copy that
    forgets it is the copy that loses a voice message in production.
    """
    src = _handlers_source()
    sites = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) \
                else getattr(f, "id", None)
            if name == "send_action":
                sites.append(node.lineno)
    assert len(sites) == 1, (
        f"handlers.py calls send_action at {sites}; exactly one site, "
        f"inside _typing_indicator"
    )
    helper = _func(src, "_typing_indicator")
    assert _calls(helper, "send_action") == sites, (
        "the only send_action call is not inside _typing_indicator"
    )
