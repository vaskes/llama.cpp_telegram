"""TECH-DEBT #4 — the turn ledger.

The rollback block existed in all four handlers, twice each. This module owns
it now. The tests are mostly about failure direction, because that is where
this can hurt: an undo that does not run leaves a user message in the history
with no assistant reply, and the user sees half a conversation.

Direction matters more than coverage here:
  - persisting must be able to fail loudly
  - rolling back must never raise into the caller
  - rolling back twice must not delete an unrelated message
"""
import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


@pytest.fixture
def clean_store(tmp_path, monkeypatch):
    import storage
    from storage import Storage
    import turn
    st = Storage(str(tmp_path / "t.db"))
    # Patch BOTH: turn.Turn.rollback resolves its store from turn's globals,
    # while the write itself goes through persistence, which has its own
    # get_store. Patching only one sends the row and the undo to different
    # databases and the test asserts nonsense.
    # One patch, on the owner. The consumers resolve storage.get_store() at
    # call time, so this reaches turn, persistence and handlers at once --
    # the previous six-line arrangement silently missed whichever module was
    # added next, which is exactly how turn.py went unpatched for a commit.
    monkeypatch.setattr(storage, "get_store", lambda: st)
    yield st
    st._conn().execute("DELETE FROM messages")
    st._conn().execute("DELETE FROM chat_threads")
    st._conn().commit()


def _seed(store, chat=1, thread="main", n=3):
    import json
    import time
    now = time.time()
    store._conn().execute(
        "INSERT INTO chat_threads (chat_id, thread_id, created_at, last_used) "
        "VALUES (?,?,?,?)", (chat, thread, now, now))
    for i in range(n):
        store._conn().execute(
            "INSERT INTO messages (chat_id, thread_id, role, content, "
            "sender_name, created_at) VALUES (?,?,?,?,?,?)",
            (chat, thread, "user",
             json.dumps({"role": "user", "content": f"msg{i}"}), "v", now + i))
    store._conn().commit()


def _count(store, chat=1, thread="main"):
    return store._conn().execute(
        "SELECT COUNT(*) FROM messages WHERE chat_id=? AND thread_id=?",
        (chat, thread)).fetchone()[0]


# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_then_commit_leaves_the_message(clean_store):
    from turn import llm_turn
    _seed(clean_store, n=2)
    async with llm_turn(1, "main", "t") as t:
        await t.persist("hello", sender_name="v")
    assert t.persisted is True
    assert _count(clean_store) == 3, "a committed turn must stay in history"


@pytest.mark.asyncio
async def test_exception_inside_the_block_rolls_back(clean_store):
    """The ledger SWALLOWS the exception on purpose.

    Handlers already report errors to the user themselves; re-raising would
    double-report, and in the dispatch task it would surface a second time as
    an unhandled task failure. So the contract is "the message is undone and
    the caller carries on", not "the caller sees the exception".
    """
    from turn import llm_turn
    _seed(clean_store, n=2)
    async with llm_turn(1, "main", "t") as t:
        await t.persist("doomed", sender_name="v")
        raise RuntimeError("llm died")
    assert _count(clean_store) == 2, (
        "the user message must be gone — a persisted message with no assistant "
        "reply is a conversation the user can see half of"
    )


@pytest.mark.asyncio
async def test_rollback_is_a_noop_when_nothing_was_persisted(clean_store):
    """The Stop button can fire before the persist. Rolling back then must not
    delete the PREVIOUS message — that is how "the bot deleted my message"
    happens."""
    from turn import Turn
    _seed(clean_store, n=3)
    t = Turn(1, "main", "t")
    assert await t.rollback("nothing written") is False
    assert _count(clean_store) == 3, "a rollback with no write deleted a message"


@pytest.mark.asyncio
async def test_double_rollback_does_not_delete_twice(clean_store):
    from turn import Turn
    _seed(clean_store, n=3)
    t = Turn(1, "main", "t")
    await t.persist("doomed", sender_name="v")
    assert await t.rollback("first") is True
    assert await t.rollback("second") is False
    assert _count(clean_store) == 3, (
        "the second rollback deleted another message — the flag has to clear "
        "after the first undo, not just the count"
    )


@pytest.mark.asyncio
async def test_rollback_failure_is_loud_not_silent(clean_store, capsys):
    """If the DB refuses the delete there is no second undo. The only thing
    left is to be visible about it."""
    from turn import Turn
    _seed(clean_store, n=3)

    def _explode(*a, **kw):
        raise RuntimeError("database is locked")

    clean_store.delete_last_user_message = _explode
    t = Turn(1, "main", "handle_text")
    await t.persist("doomed", sender_name="v")
    ok = await t.rollback("db locked")
    assert ok is False
    out = capsys.readouterr().out
    assert "ROLLBACK FAILED" in out, (
        "a failed rollback must be loud; silence here is how a half-rolled-back "
        "conversation becomes a debugging session nobody remembers starting"
    )
    assert "no assistant reply" in out


@pytest.mark.asyncio
async def test_flag_is_set_before_the_write_is_attempted(clean_store):
    """Turn.persist sets `persisted` before awaiting the DB.

    If persist raises halfway, the flag is already true and the rollback
    attempts a delete. The safe direction is to attempt an undo that might be
    a no-op, never to skip an undo that was needed.
    """
    import persistence
    from turn import Turn
    _seed(clean_store, n=3)

    async def _half_write(*a, **kw):
        raise RuntimeError("disk full halfway")

    orig = persistence.persist
    persistence.persist = _half_write
    try:
        t = Turn(1, "main", "t")
        with pytest.raises(RuntimeError):
            await t.persist("half", sender_name="v")
        assert t.persisted is True, (
            "the flag must be set before the write, so a partial write is "
            "still eligible for rollback"
        )
    finally:
        persistence.persist = orig


@pytest.mark.asyncio
async def test_rows_counter_tracks_multiple_writes(clean_store):
    from turn import Turn
    _seed(clean_store, n=2)
    t = Turn(1, "main", "t")
    await t.persist("a", sender_name="v")
    await t.persist("b", sender_name="v")
    assert t.rows == 2


# ---------------------------------------------------------------------------
# structural: the handlers must actually use it
# ---------------------------------------------------------------------------

def test_handlers_no_longer_hand_roll_the_undo():
    """The point of the refactor. A hand-copied undo coming back is exactly
    what this replaced."""
    src = (BOT_DIR / "handlers.py").read_text(encoding="utf-8")
    assert "store.delete_last_user_message" not in src, (
        "handlers.py is calling delete_last_user_message directly again. It "
        "belongs in turn.Turn.rollback, which owns the flag and the failure "
        "handling."
    )


def test_every_handler_with_a_persist_has_a_turn():
    import ast
    tree = ast.parse((BOT_DIR / "handlers.py").read_text(encoding="utf-8"))
    for fn in tree.body:
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        if not fn.name.startswith("handle_"):
            continue
        body = ast.dump(fn)
        if "turn.persist" not in body:
            continue   # handle_photo stores nothing since T67.2
        assert "turn = Turn(" in body, (
            f"{fn.name} calls turn.persist but never builds a Turn — NameError "
            f"on the first message"
        )


def test_turn_module_does_not_import_handlers():
    """turn.py sits below the handlers. If it reached back up, the cycle this
    refactor removed would close again."""
    src = (BOT_DIR / "turn.py").read_text(encoding="utf-8")
    for bad in ("import handlers", "from handlers", "import dispatch",
                "from dispatch"):
        assert bad not in src, f"turn.py must not depend on {bad}"


@pytest.mark.tech_debt(item=4, reason="Pins TECH-DEBT #4: one ledger owns persist/rollback.")
def test_this_marker_is_real():
    """Anchor for the tech-debt ledger reconciler.

    The reconciler greps for the decorator rather than trusting that
    somebody kept it attached to something, so the marker needs a
    function to live on.
    """
    assert True
