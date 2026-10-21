"""W3-H1 -- the window AFTER persist, BEFORE the main try.

Three injection points, three different guarantees:

  test_slot_release.py          send_action / _reply fail
                                -> the window guard releases the slot
  test_slot_release_finally.py  first statement inside the main try
                                -> the finally releases with thinking None
  THIS FILE                     _load_history fails
                                -> the early window ROLLS BACK

The third was uncovered, and the mutation proved it. Disabling the early
rollback survived every test on both interpreters:

    ROLLBACK-OFF  rows in DB after=1  ORPHANED
    FIXED         rows in DB after=0  ROLLED BACK

A user message in SQLite with no assistant reply is exactly what turn.py
exists to prevent. The code was correct; nothing watched it.

Structure mirrors the standalone probe that demonstrated both directions,
because an earlier version of this file asserted on a store the handler
never wrote to -- it passed on the mutated tree as well, which is the same
vacuous-green failure this repo keeps finding.
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

CHAT = -1002
THREAD = "w3-h1"


class _Boom(RuntimeError):
    pass


def _returns(value):
    """Synchronous factory. An `async def` here returns a coroutine, which
    is then assigned as the handler and blows up with TypeError -- the first
    version of this file did exactly that and 'passed'."""
    async def _f(*a, **kw):
        return value
    return _f


class _Thinking:
    message_id = 4242

    async def delete(self):
        return None


def _update():
    class Chat:
        id, type = CHAT, "private"

        async def send_action(self, *a, **kw):
            return None

    user = types.SimpleNamespace(id=1, username="u", first_name="A",
                                 last_name="B")
    msg = types.SimpleNamespace(
        chat=Chat(), from_user=user, message_id=555, message_thread_id=THREAD,
        text="hi there", caption=None, caption_entities=[], entities=[],
        date=None, reply_to_message=None, reply_text=None, photo=None,
        document=None, voice=None, audio=None, video_note=None, sticker=None,
    )
    return types.SimpleNamespace(message=msg, effective_user=user,
                                 effective_chat=msg.chat,
                                 callback_query=None,
                                 message_thread_id=THREAD)


def _context():
    return types.SimpleNamespace(bot=types.SimpleNamespace(data=None))


async def _drive(handlers, storage_mod, store, monkeypatch):
    """Run handle_text up to the _load_history failure. Returns (rows, exc)."""
    seen = {"load_history": 0}

    async def _boom(*a, **kw):
        seen["load_history"] += 1
        raise _Boom("history unavailable")

    # Module-attribute DI: turn.py and persistence.py both resolve the store
    # as storage.get_store() at call time, so replacing the attribute is
    # enough. No patching of a `from storage import get_store` binding.
    # monkeypatch, not plain assignment. The first version bound
    # handlers._route_to_thread and friends permanently and broke four
    # tests in test_handlers.py -- pytest runs modules in one process, so
    # the pollution outlived the test that caused it.
    monkeypatch.setattr(storage_mod, "get_store", lambda: store,
                        raising=False)
    monkeypatch.setattr(handlers, "_route_to_thread",
                        _returns((CHAT, THREAD, False)), raising=False)
    monkeypatch.setattr(handlers, "_reply", _returns(_Thinking()),
                        raising=False)
    monkeypatch.setattr(handlers, "_load_history", _boom, raising=False)
    monkeypatch.setattr(handlers, "_is_rating_active", lambda u: False,
                        raising=False)

    escaped = None
    try:
        await handlers.handle_text(_update(), _context())
    except BaseException as exc:  # noqa: BLE001 - seeing it is the point
        escaped = exc

    assert seen["load_history"] == 1, (
        f"_load_history was called {seen['load_history']} times; the handler "
        f"never reached the window this test is about"
    )
    return len(store.get_messages(CHAT, THREAD, limit=100)), escaped


@pytest.fixture
def store_and_handlers(monkeypatch):
    import handlers
    import storage as storage_mod

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
        db_path = fh.name

    store = storage_mod.Storage(db_path)

    def _slot():
        sem = asyncio.Semaphore(2)

        async def _acquire(chat_id, user_id, update):
            await sem.acquire()
            return sem

        handlers._check_user_slot = _acquire
        return sem

    yield handlers, storage_mod, store, _slot, db_path

    del store
    Path(db_path).unlink(missing_ok=True)


def test_persisted_message_is_rolled_back_when_the_window_fails(
        store_and_handlers, monkeypatch):
    handlers, storage_mod, store, _slot, _ = store_and_handlers
    sem = _slot()
    rows, escaped = asyncio.run(_drive(handlers, storage_mod, store, monkeypatch))

    assert rows == 0, (
        f"{rows} orphaned row(s) left in SQLite after the window failed. A "
        f"user message with no assistant reply is exactly the failure "
        f"turn.py exists to prevent."
    )
    assert not isinstance(escaped, AttributeError), (
        f"a finally masked the real error: {escaped!r}"
    )
    assert sem._value == 2, f"slot leaked: {sem._value}/2"


def test_the_probe_can_see_a_row_when_one_is_left_behind(
        store_and_handlers, monkeypatch):
    """The control has to be capable of failing.

    The first version asserted `rows_after_null == 0` against a
    _NullStore whose get_messages returns [] by declaration, plus
    `rows >= 0`. Both hold whatever the probe does. Mutation-tested by
    neutering the probe so it always reports zero: still 2 passed.

    This drives the real handler on the real store with the rollback
    neutralised and requires the orphan to be visible. If this fails,
    rows == 0 in the test above proves nothing.
    """
    import turn as turn_mod

    handlers, storage_mod, store, _slot, _ = store_and_handlers
    _slot()

    # Turn.rollback becomes a no-op -- exactly the mutation
    # test_early_rollback exists to catch, applied from the inside
    # without editing a line of production code.
    monkeypatch.setattr(turn_mod.Turn, "rollback", _returns(None),
                        raising=False)

    rows, _exc = asyncio.run(_drive(handlers, storage_mod, store, monkeypatch))

    assert rows >= 1, (
        f"the probe cannot see an orphan row (got {rows}), so rows == 0 in "
        f"test_persisted_message_is_rolled_back_when_the_window_fails is "
        f"indistinguishable from 'nothing was ever written'"
    )

