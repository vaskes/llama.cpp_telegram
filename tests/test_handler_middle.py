"""The thinking message, the display name, history and rating mode.

TECH-DEBT #13, block F -- the middle of the handler skeleton, which
blocks A-E deliberately left alone.

They left it because the parameters genuinely differ: `max_tokens` is
CAPTION for photo and document and DEFAULT for text, `user_text` is a
caption / a transcript / a user message, and `use_stream` is False for
photo only. Those are arguments, not variations. Two things around them
turned out not to be, and are here.

**`_open_thinking`** posts the thinking message, refuses to hand back a
half-built turn if it did not send, and names the sender. Three handlers
computed the name first and photo second; nothing reads one between the
two, so the order was free and is now fixed in one place.

None is the return that matters. `_reply` gives up after three TimedOuts
(T48), and every handler read `thinking.message_id` on the next line --
AttributeError on None, inside the try, where it looks like Telegram
being flaky. The guard moved to where the message was asked for, not to
a `finally`, which is cleanup and not a guard.

**`_turn_context`** loads the history and reads rating mode together,
for the three handlers that persist a turn first. Photo does not persist
one: it reads rating mode BEFORE the history, and appends the image turn
to the history afterwards. Handing it a flag would have meant a
parameter exactly one caller never uses -- so it does not call this, and
a test says why.
"""
import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=13)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import handlers  # noqa: E402

ALL = ["handle_photo", "handle_voice", "handle_document", "handle_text"]
PERSISTING = ["handle_voice", "handle_document", "handle_text"]


def _fn(name):
    src = (BOT / "handlers.py").read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in handlers.py")


class _NoReply(Exception):
    pass


def _patch(monkeypatch, thinking="T", sender="Ann", history=None, rating=False):
    seen = {"reply": 0, "sender": 0, "history": 0, "rating": 0}

    async def reply(update, text, **kw):
        seen["reply"] += 1
        if isinstance(thinking, BaseException):
            raise thinking
        return thinking

    monkeypatch.setattr(handlers, "_reply", reply)
    monkeypatch.setattr(handlers, "_stop_button_markup", lambda: None)
    monkeypatch.setattr(handlers, "_sender_display_name",
                        lambda user: (seen.__setitem__("sender", seen["sender"] + 1)
                                      or sender))

    async def load(chat_id, thread_id):
        seen["history"] += 1
        return history if history is not None else ["a", "b"]

    monkeypatch.setattr(handlers, "_load_history", load)
    monkeypatch.setattr(handlers, "_is_rating_active",
                        lambda update: (seen.__setitem__("rating", seen["rating"] + 1)
                                        or rating))
    return seen


def _update():
    from types import SimpleNamespace
    # effective_user is read by the helper's own log line, not only by
    # the handlers -- a fixture that omits it fails in the log, not in
    # the behaviour under test
    return SimpleNamespace(
        message=SimpleNamespace(from_user=object()),
        effective_user=SimpleNamespace(id=7))


# --- _open_thinking ------------------------------------------------------

async def test_it_returns_the_message_and_the_name(monkeypatch):
    _patch(monkeypatch, thinking="MSG", sender="Ann")
    got = await handlers._open_thinking(_update(), label="handle_text")
    assert got == ("MSG", "Ann")


async def test_a_thinking_message_that_never_sent_gives_none(monkeypatch, caplog):
    """`_reply` returns None after three TimedOuts. Handing that back as a
    thinking message reaches `thinking.message_id` one line later and
    raises AttributeError inside the try."""
    _patch(monkeypatch, thinking=None)
    assert await handlers._open_thinking(_update(), label="handle_photo") is None
    assert "[handle_photo] could not send the thinking message" in caplog.text


async def test_the_sender_name_is_not_computed_when_the_message_failed(monkeypatch):
    """Cheap on purpose: the name is not needed for a turn that will not
    happen, and computing it before the guard is how the four handlers
    ended up with two different orders."""
    seen = _patch(monkeypatch, thinking=None)
    await handlers._open_thinking(_update(), label="handle_text")
    assert seen["sender"] == 0


async def test_the_label_names_the_handler_in_the_warning(monkeypatch, caplog):
    _patch(monkeypatch, thinking=None)
    await handlers._open_thinking(_update(), label="handle_document")
    assert "[handle_document]" in caplog.text
    assert "[handle_text]" not in caplog.text


# --- _turn_context -------------------------------------------------------

async def test_it_returns_history_and_rating(monkeypatch):
    _patch(monkeypatch, history=["h1", "h2"], rating=True)
    got = await handlers._turn_context(_update(), 7, "main", label="handle_text")
    assert got == (["h1", "h2"], True)


async def test_it_reads_the_history_before_the_rating_flag(monkeypatch):
    """Not cosmetic: `call_llama` receives both, and history is the one
    that is expensive. Reading the flag first would only matter if a
    later change moved work between them, so the order is pinned."""
    order = []

    async def load(chat_id, thread_id):
        order.append("history")
        return []
    monkeypatch.setattr(handlers, "_load_history", load)
    monkeypatch.setattr(handlers, "_is_rating_active",
                        lambda u: order.append("rating") or False)
    await handlers._turn_context(_update(), 1, "main", label="handle_text")
    assert order == ["history", "rating"]


async def test_the_history_length_is_logged_with_the_label(monkeypatch, caplog):
    _patch(monkeypatch, history=["a", "b", "c"])
    await handlers._turn_context(_update(), 1, "main", label="handle_voice")
    assert "[handle_voice]" in caplog.text and "history_len=3" in caplog.text


# --- the callers ---------------------------------------------------------

@pytest.mark.parametrize("handler", ALL)
def test_every_handler_opens_the_thinking_turn_once(handler):
    fn = _fn(handler)
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_open_thinking"]
    assert len(calls) == 1, f"{handler}: {len(calls)} calls, expected 1"
    label = next(k.value for k in calls[0].keywords if k.arg == "label")
    assert ast.unparse(label) == repr(handler), (
        f"{handler} passes label={ast.unparse(label)} -- the log line is "
        f"how an abort is attributed to a handler"
    )


@pytest.mark.parametrize("handler", ALL)
def test_no_handler_posts_its_own_thinking_message(handler):
    """`_reply(update, '💭 думаю…')` written out again means a second
    thinking message and a second stop button."""
    fn = _fn(handler)
    stray = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_reply"
             and "думаю" in ast.unparse(n)]
    assert not stray, f"{handler}: {stray}"


@pytest.mark.parametrize("handler", ALL)
def test_the_none_guard_is_still_a_return_in_the_handler(handler):
    """The helper cannot return for its caller. If this guard goes, the
    unpacking on the next line raises on None."""
    fn = _fn(handler)
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If)
              and ast.unparse(n.test) in ("opened is None", "not opened")]
    assert guards, f"{handler}: no `opened is None` guard"
    assert all(isinstance(g.body[0], ast.Return) for g in guards)


@pytest.mark.parametrize("handler", ALL)
def test_both_results_are_unpacked(handler):
    fn = _fn(handler)
    unpacks = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and isinstance(n.value, ast.Name) and n.value.id == "opened"
               and isinstance(n.targets[0], ast.Tuple)]
    assert len(unpacks) == 1, f"{handler}: {len(unpacks)} unpacks of `opened`"
    names = [e.id for e in unpacks[0].targets[0].elts]
    assert names == ["thinking", "sender_name"], (
        f"{handler} unpacks {names} -- the values are swapped and "
        f"nothing downstream would say so"
    )


@pytest.mark.parametrize("handler", PERSISTING)
def test_the_persisting_handlers_use_the_shared_context(handler):
    fn = _fn(handler)
    assert len([n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_turn_context"]) == 1


def test_photo_does_not_call_the_context_helper():
    """Photo reads rating mode before the history and appends the image
    turn to it afterwards, so the helper does not apply. Not a gap: a
    flag parameter exactly one caller never uses is worse."""
    assert not [n for n in ast.walk(_fn("handle_photo"))
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_turn_context"]


@pytest.mark.parametrize("handler", PERSISTING)
def test_the_persisting_handlers_still_do_not_load_history_twice(handler):
    """`_turn_context` returns the history; a handler that also calls
    `_load_history` pays for it twice and can disagree with itself."""
    fn = _fn(handler)
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_load_history"]
    assert not calls, f"{handler}: loads history again at {[c.lineno for c in calls]}"