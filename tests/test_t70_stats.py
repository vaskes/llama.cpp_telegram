"""T70.4 — /stats observability for the context window.

The operator's question was "is it remembering, and what is it throwing away".
Until this, the only way to answer it was to open the SQLite file by hand on
the host. These tests pin the two things that make /stats answerable:

* storage.count_since / oldest_message_age report what the DB actually holds
  vs what falls inside the window;
* /stats renders that block in both the group and the private branch.
"""
import ast
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"

# TECH-DEBT #10: `stats` moved out of dispatch.py. Every test that
# walks the `stats` function reads the file that holds it now.
STATS_PY = BOT_DIR / "commands" / "stats_cmd.py"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def store(tmp_path):
    # The class is `Storage`; `Store` is only the module-level accessor
    # `get_store()` returns.
    from storage import Storage
    s = Storage(str(tmp_path / "t.db"))
    yield s
    # Storage caches its connection per-thread; there is no close() helper,
    # and tmp_path cleanup handles the file.


def _add(store, chat, thread, n, created_at=None):
    import json
    import time
    c = store._conn()
    for i in range(n):
        ts = time.time() if created_at is None else created_at
        c.execute(
            "INSERT INTO messages (chat_id, thread_id, role, content, "
            "sender_name, created_at) VALUES (?,?,?,?,?,?)",
            (chat, thread, "user", json.dumps({"role": "user", "content": str(i)}),
             "u", ts),
        )
    c.commit()


# ---------------------------------------------------------------------------
# storage helpers
# ---------------------------------------------------------------------------

def test_count_since_counts_only_newer_rows(store):
    """Anchor relative to real now — an absolute constant here would be a
    trap: NOW = 1.8e9 is a 2027 timestamp, so "old" rows would count as
    newer than any present-day anchor and the assertion would pass for the
    wrong reason."""
    import time
    now = time.time()
    _add(store, 1, "main", 3, created_at=now - 10 * DAY)
    _add(store, 1, "main", 4, created_at=now - 1 * DAY)
    assert store.count_since(1, "main", now - 2 * DAY) == 4


def test_count_since_anchor_in_the_future_returns_nothing(store):
    """A future anchor means "nothing has been sent yet". This is what a
    clock-skewed row would otherwise turn into a silent miscount."""
    import time
    _add(store, 1, "main", 5)
    assert store.count_since(1, "main", time.time() + DAY) == 0


def test_count_since_zero_anchor_equals_total(store):
    _add(store, 1, "main", 5)
    assert store.count_since(1, "main", 0) == 5


def test_count_since_empty_thread(store):
    assert store.count_since(999, "nope", NOW) == 0


def test_count_since_is_per_sub(store):
    import time
    _add(store, 1, "a", 3)
    _add(store, 1, "b", 7)
    assert store.count_since(1, "a", 0) == 3
    assert store.count_since(1, "b", 0) == 7


def test_count_since_no_update_during_read(store):
    """A read-only report must not mutate. If it did, /stats would be
    changing the very numbers it reports."""
    _add(store, 1, "main", 4)
    before = store.count_since(1, "main", 0)
    store.count_since(1, "main", 0)
    assert store.count_since(1, "main", 0) == before


def test_oldest_message_age_none_when_empty(store):
    assert store.oldest_message_age(42, "void") is None


def test_oldest_message_age_uses_the_oldest_row(store):
    import time
    _add(store, 1, "main", 1, created_at=time.time() - 3 * DAY)
    _add(store, 1, "main", 1, created_at=time.time() - 10 * DAY)
    age = store.oldest_message_age(1, "main")
    assert 9.5 * DAY < age < 10.5 * DAY


def test_oldest_message_age_never_negative(store):
    """A row with a created_at slightly in the future (clock skew between
    the writer and this check) must not yield a negative age — it would
    render as a nonsense value in /stats."""
    import time
    _add(store, 1, "main", 1, created_at=time.time() + 500)
    assert store.oldest_message_age(1, "main") >= 0


# ---------------------------------------------------------------------------
# /stats wiring
# ---------------------------------------------------------------------------

def test_the_stats_handler_imports_the_budget():
    """It moved with /stats, so the assertion moved with it.

    Reading dispatch.py here would have kept passing for a reason that
    had nothing to do with /stats: the module still imports the budget
    for other code. The check has to follow the handler.
    """
    src = STATS_PY.read_text(encoding="utf-8")
    assert "from context_budget import" in src, (
        "/stats needs the live ContextBudget to describe the active window"
    )


def test_stats_renders_the_memory_block():
    src = STATS_PY.read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "stats"
    )
    body = ast.dump(fn)
    assert "count_since" in body, (
        "/stats must call count_since — without it the block cannot show how "
        "many messages the model actually sees"
    )
    assert "oldest_message_age" in body
    # Both branches must render it, not just the private one.
    assert body.count("memory") >= 2, (
        "the memory block must appear in both the group and private branches "
        "of /stats"
    )


def test_stats_reports_the_anchor_as_non_sliding():
    """The operator reads this in Telegram and should not have to wonder
    whether the window moves under him."""
    src = STATS_PY.read_text(encoding="utf-8")
    assert "does not slide" in src, (
        "/stats should state that the window is anchored at bot start, not "
        "sliding — that is the whole point of the anchored design"
    )


def test_stats_explains_history_is_kept():
    """The operator is keeping history for LoRA training. /stats showing a "
    "big 'not sent' count could read as data loss; say plainly that it is "
    "stored."""
    src = STATS_PY.read_text()
    assert "training" in src, (
        "/stats must tell the user that out-of-window history is still "
        "stored, not discarded"
    )

def test_memory_lines_helper_is_a_coroutine():
    """Shipped broken once: `_memory_lines` awaited a future but was declared
    `def`, so the failure was a SyntaxError at import — and py_compile on the
    module did not surface it in the deploy step, because the module is only
    imported lazily inside the webhook request handler. 22 tests failed, all
    in test_webhook.py, a file that has nothing to do with context memory.

    So: a nested helper that awaits must be `async def`. Assert the AST shape
    rather than trusting py_compile.
    """
    tree = ast.parse(STATS_PY.read_text(encoding="utf-8"))
    stats = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "stats"
    )
    helper = next(
        (n for n in stats.body
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and getattr(n, "name", None) == "_memory_lines"),
        None,
    )
    assert helper is not None, "_memory_lines disappeared from /stats"
    assert isinstance(helper, ast.AsyncFunctionDef), (
        "_memory_lines awaits futures, so it must be `async def`. A plain `def` "
        "here is a SyntaxError at import time — invisible to py_compile, and it "
        "only explodes when the webhook handler first imports dispatch at "
        "request time."
    )
