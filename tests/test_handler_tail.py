"""Rating dispatch, persist, and the last chance to not send it.

TECH-DEBT #13, block E. The tail of a turn was the same three steps four
times: run the rating dispatch, write the turn, send the answer.

The piece that is not mechanical is the order. `_apply_rating_and_persist`
runs BEFORE the reply, and the abort check sits between them. That window
is the whole reason the guard exists: a Stop that lands after the
transcript is written but before the message goes out would otherwise put
a cancelled turn on screen with a transcript entry behind it.

Only the two streaming handlers pass an `abort_event` to the helper, and
that is not a shortcut. Photo and document do not stream -- there is no
await between the persist and the reply that Stop could land in -- so
their guard was always `if bot_response`, and giving them an event would
add a check that can never fire.

The >4000-char chunking used to be deliberately left in the handlers:
the streaming handlers sent long answers in slices, photo and document
sent one `send_reply`, and that difference in what the user receives was
not something an extraction could quietly change.

It changed on purpose on 2026-10-24 (T-FREEZE-CHUNK-1). Telegram
rejects a message over 4096 characters, so a long answer to a photo
came back as `Message_too_long` and the user saw the error instead of
the answer. The split now lives in `_send_answer_text`, and all four
handlers use it.
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

STREAMING = ["handle_voice", "handle_text"]
NON_STREAMING = ["handle_photo", "handle_document"]


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found")


def _tree():
    return ast.parse((BOT / "handlers.py").read_text(encoding="utf-8"))


def _answer(text, files=(), raw=None):
    """An answer as `unwrap_answer` returns it: the prose, the files, and
    the answer as the model wrote it."""
    from llm_result import Answer
    return Answer(text, list(files), text if raw is None else raw)


def _patch(monkeypatch, persisted, abort=False):
    async def apply_rating_and_persist(context, update, chat_id, thread_id,
                                       bot_response, rating_active):
        return persisted
    calls = []

    class _Event:
        def is_set(self):
            return abort
    monkeypatch.setattr(handlers, "_apply_rating_and_persist",
                        apply_rating_and_persist)
    return calls, _Event()


# --- what comes back -----------------------------------------------------

async def test_the_persisted_answer_comes_back(monkeypatch):
    _patch(monkeypatch, "the answer")
    got = await handlers._persist_for_reply(
        None, None, 1, "main", _answer("the answer"), False)
    assert got == "the answer"


async def test_the_answer_is_persisted_whole_and_the_chat_gets_the_prose(monkeypatch):
    """Two different strings since 2026-10-24.

    The code blocks the model wrote are delivered as documents, so the
    chat gets the prose. The bot remembers the whole answer, because the
    model wrote that code, and a follow-up question about the file it
    just sent needs it to still be there. Persisting only the prose
    would make the model forget what it just sent.
    """
    seen = {}

    async def apply_rating_and_persist(context, update, chat_id, thread_id,
                                       bot_response, rating_active):
        seen["persisted"] = bot_response
        return bot_response

    monkeypatch.setattr(handlers, "_apply_rating_and_persist",
                        apply_rating_and_persist)

    from llm_result import Answer
    answer = Answer("Here it is:", [],
                    "Here it is:\n\n```json\n{\"a\": 1}\n```")
    got = await handlers._persist_for_reply(None, None, 1, "main", answer, False)
    assert got == "Here it is:"
    assert "```json" in seen["persisted"]


async def test_a_rating_only_turn_sends_nothing(monkeypatch):
    """The rating path answered with an emoji reaction and consumed the
    text. Returning the empty string would make the handler send an
    empty message."""
    _patch(monkeypatch, "")
    assert await handlers._persist_for_reply(
        None, None, 1, "main", _answer("text"), True) is None


async def test_the_persist_happens_before_the_abort_check(monkeypatch):
    """Both orderings "work" in a test that only looks at the return
    value. This one checks that a Stop set during the persist does not
    skip the write -- the transcript entry has to exist for the rollback
    to mean anything."""
    seen = {}

    async def apply_rating_and_persist(context, update, chat_id, thread_id,
                                       bot_response, rating_active):
        seen["persisted"] = True
        return "answer"

    monkeypatch.setattr(handlers, "_apply_rating_and_persist",
                        apply_rating_and_persist)

    class _Event:
        def is_set(self):
            seen["checked"] = True
            return True

    assert await handlers._persist_for_reply(
        None, None, 1, "main", _answer("text"), False,
        abort_event=_Event()) is None
    assert seen == {"persisted": True, "checked": True}


# --- the abort window ----------------------------------------------------

async def test_a_stop_between_persist_and_reply_sends_nothing(monkeypatch):
    _patch(monkeypatch, "answer", abort=True)

    class _Event:
        def is_set(self):
            return True
    assert await handlers._persist_for_reply(
        None, None, 1, "main", _answer("answer"), False,
        abort_event=_Event()) is None


async def test_no_event_means_no_abort_check(monkeypatch):
    """Photo and document pass nothing. The helper must not invent a
    source for it, and must not treat "no event" as "aborted" -- that
    would silence two of the four handlers entirely."""
    _patch(monkeypatch, "answer", abort=True)
    got = await handlers._persist_for_reply(
        None, None, 1, "main", _answer("answer"), False)
    assert got == "answer"


# --- the callers ---------------------------------------------------------

@pytest.mark.parametrize("handler", STREAMING + NON_STREAMING)
def test_every_handler_calls_the_helper_once(handler):
    fn = _fn(_tree(), handler)
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_persist_for_reply"]
    assert len(calls) == 1, f"{handler}: {len(calls)} calls, expected 1"


@pytest.mark.parametrize("handler", STREAMING)
def test_streaming_handlers_pass_their_abort_event(handler):
    fn = _fn(_tree(), handler)
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_persist_for_reply")
    kws = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert kws.get("abort_event") == "abort_event", (
        f"{handler}: does not pass abort_event. It streams, so Stop can "
        f"land between the persist and the reply -- which is exactly the "
        f"window the helper exists to close"
    )


@pytest.mark.parametrize("handler", NON_STREAMING)
def test_non_streaming_handlers_do_not_pass_an_abort_event(handler):
    fn = _fn(_tree(), handler)
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_persist_for_reply")
    kws = {k.arg for k in call.keywords}
    assert "abort_event" not in kws, (
        f"{handler}: passes abort_event. It does not stream, so there is "
        f"no await between the persist and the reply for Stop to land in. "
        f"If that changed, say so here -- otherwise the next reader cannot "
        f"tell a deliberate omission from a missed one"
    )


@pytest.mark.parametrize("handler", NON_STREAMING)
def test_non_streaming_handlers_keep_the_plain_guard(handler):
    fn = _fn(_tree(), handler)
    assert not [n for n in ast.walk(fn)
                if isinstance(n, ast.If)
                and "abort_event.is_set()" in ast.unparse(n.test)
                and n.lineno > _call_line(fn)], (
        f"{handler}: still guards the reply on abort_event even though the "
        f"helper does it"
    )


@pytest.mark.parametrize("handler", STREAMING + NON_STREAMING)
def test_every_handler_sends_the_answer_through_the_chunking_helper(handler):
    """The split is on all four paths since 2026-10-24.

    Before that, only the two streaming handlers split their answer and
    photo and document sent one `send_reply` -- so a long answer to a
    photo came back as `Message_too_long` (T-FREEZE-CHUNK-1). The
    helper is the one place that splits, and a handler that sends the
    answer on its own is one long answer away from the same error.
    """
    fn = _fn(_tree(), handler)
    assert [n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_send_answer_text"], \
        f"{handler}: does not send the answer through _send_answer_text"
    assert not [n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "send_reply"], \
        f"{handler}: sends the answer itself, unsplit"


@pytest.mark.parametrize("handler", STREAMING + NON_STREAMING)
def test_the_reply_still_comes_after_the_persist(handler):
    fn = _fn(_tree(), handler)
    persist = min(n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name)
                  and n.func.id == "_persist_for_reply")
    reply = min(n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "_send_answer_text")
    assert persist < reply, f"{handler}: replies at {reply}, persists at {persist}"


def test_no_handler_persists_on_its_own_anymore():
    """`_apply_rating_and_persist` moved into the helper. A direct call
    in a handler is a second path that would skip the abort check."""
    fns = {n.name: n for n in ast.walk(_tree())
           if isinstance(n, ast.AsyncFunctionDef)}
    stray = []
    for name in STREAMING + NON_STREAMING:
        stray += [f"{name}:{n.lineno}" for n in ast.walk(fns[name])
                  if isinstance(n, ast.Call)
                  and getattr(n.func, "id", None) == "_apply_rating_and_persist"]
    assert not stray, stray


def _call_line(fn):
    return min(n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)
               and n.func.id == "_persist_for_reply")