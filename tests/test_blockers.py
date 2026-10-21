"""Review 2026-10-22 (third wave): the three blockers, as behaviour.

The reviewer measured each of these and then measured the same thing
about my fixes: adding the dict unwrap at all three retry sites left the
suite at 1336 passed. A fix the suite cannot see is not a fix, it is a
wish. So each test here is behavioural and each was checked by reverting
the fix and watching it go red.

  1. the retry path never unwrapped call_llama's dict
  2. _reply returns None after three timeouts and all four handlers
     then read thinking.message_id
  3. Turn.rollback deleted "the newest user row in this thread", which
     is not its own row when two people share a topic

Plus the coverage gap the reviewer found independently, which no test
in the repo covered at all: a response that arrives *after* the user
pressed Stop must be discarded. test_handle_text_rollback_on_abort
passes because its fake call_llama returns None, which reaches the
abort branch by a different route and checks the rollback rather than
the discard.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

EMPTY = "[model returned an empty response.]"


# --------------------------------------------------------------- harness --

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


class _GaveUp:
    """Sentinel: _reply exhausted its timeouts and returned None."""


def _common(monkeypatch, tmp_path, call_llama, thinking=None,
            reply_gives_up=False, caplog=None):
    """Everything handle_* needs, so each test states only its own part."""
    import handlers
    _store_on(monkeypatch, tmp_path)

    async def fake_route(update, context):
        return (1, "main", False)

    sem = asyncio.Semaphore(99)

    async def fake_slot(chat_id, user_id, update):
        # Acquire, because the real _check_user_slot does. Returning an
        # un-acquired semaphore made the handler release a slot it never
        # took, so the "no leak" assertion compared against a baseline
        # that was never true -- true under any handler behaviour, which
        # is the definition of a control that cannot fail.
        await sem.acquire()
        return sem

    # Patch the READER. handlers.py:75 does `from dispatch import
    # _check_user_slot`, which copies the value at import; patching
    # dispatch._check_user_slot changes nothing handlers calls. Patching
    # it there made the real per-user semaphore run, the second test
    # queued behind the first one's slot, and the file hung for 300s.
    #
    # And it made the release assertion below meaningless: sem belonged
    # to fake_slot, which was never called, so sem._value == 99 was true
    # no matter what the handler did. A control whose assertions hold
    # under any behaviour is not a control.
    monkeypatch.setattr("handlers._route_to_thread", fake_route)
    monkeypatch.setattr("handlers._check_user_slot", fake_slot)
    monkeypatch.setattr("handlers._is_rating_active", lambda u: False)
    monkeypatch.setattr("handlers._stop_button_markup", lambda: None)
    # Prove the stub is the one in use, so the release check below is
    # about the handler and not about an object nobody touched.
    assert handlers._check_user_slot is fake_slot

    sent = thinking if thinking is not None else _Msg()

    async def fake_reply(update, text, **kw):
        # T48: after three TimedOuts _reply returns None.
        # Record the text: the handlers report their own failures by
        # sending "❌ Error: ..." through _reply, so a fake that returns a
        # message without recording what was said makes every swallowed
        # crash invisible. That is why removing the guard from
        # handle_photo/voice/document left the suite green -- the
        # AttributeError was raised, swallowed, and reported to the user,
        # and the probe had no way to see it.
        if not reply_gives_up:
            sent.replies.append(text)
        return None if reply_gives_up else sent

    monkeypatch.setattr("handlers._reply", fake_reply)

    # The answer itself goes out through send_reply, not _reply, so a probe
    # that only watches _reply sees the error messages and nothing else.
    async def fake_send_reply(update, text, **kw):
        sent.replies.append(text)

    monkeypatch.setattr("handlers.send_reply", fake_send_reply)

    # Recorded so a test can assert what the handler attempted, not only
    # what it logged. The crash this file exists for is raised and then
    # swallowed by the handler's own except, so log-scraping and exception
    # checks both come back clean on the broken tree -- but on the broken
    # tree the handler still gets as far as registering an abort event for
    # a thinking message that does not exist.
    registered = []
    monkeypatch.setattr("handlers._register_abort_event",
                        lambda cid, mid, ev: registered.append((cid, mid)))

    # Downloads must SUCCEED, or handle_photo/document/voice return before
    # ever reaching the thinking message -- and a test that never reaches
    # the line it is about passes with the guard removed. The real helper
    # gets a MagicMock from bot.download and gives up. Returning bytes
    # keeps the handler on the path under test.
    async def fake_download(file, max_bytes, kind, update):
        return b"payload"

    monkeypatch.setattr("handlers._download_with_limit", fake_download)

    async def fake_transcribe(voice_bytes):
        return "transcript"

    monkeypatch.setattr("handlers.transcribe_voice", fake_transcribe)

    class FakeSem:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr("handlers._get_global_llm_sem", lambda: FakeSem())
    monkeypatch.setattr("handlers.call_llama", call_llama)
    monkeypatch.setattr("handlers.Update", MagicMock())
    return handlers, sent, sem, registered


def _errors(capsys):
    """What the handler logged on its own error path.

    Necessary because each handler swallows its own exceptions: an
    AttributeError on thinking.message_id is caught, logged as "[ERR
    text]" and shown to the user as an error string. The handler does
    not re-raise, so "the await returned" is true on the crashing code
    too, and a test asserting only that passes on the bug.

    capsys, not caplog. logconf.py:123 sets propagate = False on the
    module logger, so caplog -- which attaches to the root -- can never
    see a line from it. The first version of this helper used caplog and
    was therefore a check that could not fail for any code whatsoever,
    which is the exact failure mode this file exists to prevent.
    The repo's own _LazyStreamHandler is built for capsys.
    """
    return [ln for ln in capsys.readouterr().out.splitlines()
            if "[ERR" in ln]


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
    # get_file's return value has to be a real-ish object: the download
    # helper reads file.file_size and compares it to a max, and a MagicMock
    # return value raises on the comparison.
    remote = MagicMock()
    remote.file_size = 100
    c.bot.get_file = AsyncMock(return_value=remote)
    c.bot.download = AsyncMock()
    return c


# ------------------------------------------------------- 1. retry dict --

@pytest.mark.asyncio
async def test_retry_unwraps_the_dict_call_llama_returns(monkeypatch, tmp_path,
                                                        capsys):
    """The retry called .startswith on what is a dict.

    call_llama returns {"text", "executed_tools", "aborted"} on a normal
    answer. The primary path unwraps it; the retry did not, so the one
    branch that exists to be reached -- a model that burned tokens on
    reasoning and produced no content -- raised AttributeError every
    time. The user got the error string and, in handle_text, their
    message was rolled back, so the next turn's context was missing it.
    """
    calls = []

    async def fake_call(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            return EMPTY                     # triggers the retry
        return {"text": "second answer", "executed_tools": [], "aborted": False}

    handlers, sent, _, _reg = _common(monkeypatch, tmp_path, fake_call)

    await handlers.handle_text(_update(), _context())

    assert not _errors(capsys), (
        f"the retry died on its error path instead of answering: "
        f"{_errors(capsys)}")
    assert len(calls) == 2, "the retry should have been taken"
    delivered = sent.edits + sent.replies
    assert any("second answer" in t for t in delivered), (
        f"the retried answer never reached the user: {delivered}"
    )
    # A substring check passes when the dict is stringified on its way
    # out, which is what happens if the unwrap is skipped. The delivered
    # value has to be the plain text, not a repr of the dict.
    assert not any("dict" in t and "executed_tools" in t for t in delivered), (
        f"call_llama's dict was delivered instead of its text: {delivered}"
    )


@pytest.mark.asyncio
async def test_retry_keeps_the_executed_tools_preamble(monkeypatch, tmp_path,
                                                       capsys):
    """Dropping the unwrap's preamble would answer without the tool trace.

    A second, quieter bug in the same three lines: an answer that ran
    tools and then returned empty, retried and answered, would lose the
    record of what it did.
    """
    calls = []

    async def fake_call(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            return EMPTY
        return {"text": "done", "aborted": False,
                "executed_tools": [{"name": "bash_command",
                                    "args": {"command": "ls -la"},
                                    "result": "ok"}]}

    handlers, sent, _, _reg = _common(monkeypatch, tmp_path, fake_call)
    await handlers.handle_text(_update(), _context())

    assert not _errors(capsys), f"retry took the error path: {_errors(capsys)}"
    joined = " ".join(sent.edits + sent.replies)
    assert "```bash" in joined and "$ ls -la" in joined, (
        f"the tool trace was dropped from the retry: {joined!r}")


# --------------------------------------------------- 2. _reply returns None --

@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["handle_text", "handle_photo",
                                     "handle_document", "handle_voice"])
async def test_handler_survives_reply_returning_none(monkeypatch, tmp_path,
                                                     handler, capsys):
    """_reply gives up after three TimedOuts and returns None.

    All four handlers then read thinking.message_id on the next line.
    The AttributeError was already fixed once in this file -- in the
    finally block, where the semaphore leak was noticed -- so the first
    read stayed unguarded. A fix in cleanup is not a guard.
    """
    called = []

    async def fake_call(*a, **kw):
        called.append(1)
        return {"text": "must not happen", "executed_tools": [],
                "aborted": False}

    handlers, sent, sem, registered = _common(
        monkeypatch, tmp_path, fake_call, reply_gives_up=True)

    u = _update()
    c = _context()
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

    # must not raise
    await getattr(handlers, handler)(u, c)

    assert not called, "the handler called the LLM with no thinking message"
    assert not _errors(capsys), (
        f"{handler} raised and swallowed it instead of returning cleanly: "
        f"{_errors(capsys)}"
    )
    assert not registered, (
        f"{handler} got as far as registering an abort event "
        f"{registered} for a thinking message that does not exist -- the "
        f"guard did not return"
    )
    surfaced = [t for t in sent.replies if "❌ Error" in t]
    assert not surfaced, (
        f"{handler} told the user it failed instead of returning quietly: "
        f"{surfaced}"
    )
    assert sem._value == 99, (
        f"the per-user semaphore did not balance: {sem._value}, expected 99 "
        f"(99 - one acquired + one released)"
    )


# ------------------------------------------------- 3. rollback is not "newest" --

@pytest.mark.asyncio
async def test_rollback_does_not_delete_a_concurrent_turn(monkeypatch,
                                                           tmp_path):
    """Rollback deleted the newest user row in the thread, not its own.

    Semaphores are per (chat, user), so in a group two people can
    persist into the same topic at once. A persists, B persists, A
    aborts: A's rollback deleted B's message. A's question stayed with
    no answer and B's vanished, and the next request to the model was
    about the wrong thing.
    """
    from turn import Turn
    store = _store_on(monkeypatch, tmp_path)

    a = Turn(1, "main", "A")
    b = Turn(1, "main", "B")
    await a.persist("A's question", sender_name="a")
    await b.persist("B's question", sender_name="b")

    assert len(store.get_messages(1, "main")) == 2

    assert await a.rollback("aborted") is True

    left = [m for m in store.get_messages(1, "main")]
    bodies = [str(m.get("content")) for m in left]
    assert not any("A's question" in b_ for b_ in bodies), (
        "A's own row survived its own rollback")
    assert any("B's question" in b_ for b_ in bodies), (
        f"A's rollback ate B's message: {bodies}"
    )


@pytest.mark.asyncio
async def test_rollback_is_still_idempotent(monkeypatch, tmp_path):
    """The docstring promises a second call is a no-op, not a fresh
    unrelated delete. Ids make that true by construction."""
    from turn import Turn
    store = _store_on(monkeypatch, tmp_path)

    a = Turn(1, "main", "A")
    b = Turn(1, "main", "B")
    await a.persist("A's question", sender_name="a")
    await b.persist("B's question", sender_name="b")

    assert await a.rollback("aborted") is True
    assert await a.rollback("again") is False, (
        "the second rollback deleted something; it must match nothing"
    )
    assert any("B's question" in str(m.get("content"))
               for m in store.get_messages(1, "main"))


@pytest.mark.asyncio
async def test_rollback_falls_back_when_no_row_id_came_back(monkeypatch,
                                                            tmp_path):
    """persist that never returned an id still has to roll back.

    This is the path taken when persist itself is what went wrong, so
    it is the path where leaving an orphan row is worst.
    """
    from turn import Turn
    store = _store_on(monkeypatch, tmp_path)

    real = store.add_message
    store.add_message = lambda *a, **kw: None
    t = Turn(1, "main", "T")
    await t.persist("doomed", sender_name="v")
    store.add_message = real
    assert t._row_ids == []

    assert await t.rollback("no id") is True
    assert store.get_messages(1, "main") == []


@pytest.mark.asyncio
async def test_delete_message_ids_will_not_reach_across_threads(monkeypatch,
                                                                 tmp_path):
    """The predicate is redundant with the ids and stays anyway."""
    store = _store_on(monkeypatch, tmp_path)
    store.create_thread(2, "other")
    store.add_message(1, "main", "user", '{"role":"user","content":"mine"}')
    store.add_message(2, "other", "user", '{"role":"user","content":"theirs"}')

    # get_messages does not expose the row id, so ask for it directly.
    def _ids(chat, thread):
        rows = store._conn().execute(
            "SELECT id FROM messages WHERE chat_id=? AND thread_id=?",
            (chat, thread)).fetchall()
        return [r[0] for r in rows]

    assert store.delete_message_ids(1, "main", _ids(1, "main")) == 1
    assert store.get_messages(1, "main") == []

    # an id that belongs to another thread must delete nothing
    assert store.delete_message_ids(1, "main", _ids(2, "other")) == 0
    assert len(store.get_messages(2, "other")) == 1, (
        "an id from another thread was deleted"
    )


# ------------------------------------- the gap: abort discards a real answer --

@pytest.mark.asyncio
async def test_stop_discards_a_response_that_already_has_text(monkeypatch,
                                                              tmp_path, capsys):
    """Pressing Stop must throw away the answer, not deliver it.

    No test covered this. test_handle_text_rollback_on_abort is the test
    that looks like it does, and it passes for the wrong reason: its
    fake call_llama returns None, which reaches the abort branch
    because the response is None -- not because the user pressed Stop
    while a real answer was being produced. Mutating the abort check to
    `if False:` leaves the whole suite green.

    This one returns a non-empty answer with abort_event already set,
    which is the case that had no coverage at all.
    """
    handlers, sent, _, _reg = _common(monkeypatch, tmp_path, AsyncMock(
        return_value={"text": "SHOULD BE DISCARDED", "executed_tools": [],
                      "aborted": False}))

    # Simulate Stop having been pressed before the answer arrived.
    import handlers as _h
    real_register = _h._register_abort_event

    def register_and_set(chat_id, message_id, ev):
        real_register(chat_id, message_id, ev)
        ev.set()

    monkeypatch.setattr("handlers._register_abort_event", register_and_set)

    await handlers.handle_text(_update(), _context())

    # Positive control. Without this the test is vacuous: it passes just
    # as well on a tree where the abort branch was never entered, which is
    # the failure mode that let the reviewer mutate the check to `if False`
    # and watch the whole suite stay green.
    abort_logs = [ln for ln in capsys.readouterr().out.splitlines()
                  if "ABORTED" in ln]
    assert abort_logs, (
        "the abort branch never ran -- this test is measuring nothing"
    )

    joined = " ".join(sent.edits + sent.replies)
    assert "SHOULD BE DISCARDED" not in joined, (
        "a response produced after Stop was delivered to the user"
    )