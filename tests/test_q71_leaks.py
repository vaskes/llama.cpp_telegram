"""Two leaks from the Qwen review round — both shaped "looks fine, is not".

Neither is a crash. That is exactly why they survived: no traceback, no failed
request, nothing in the logs that says a number is quietly growing or a task is
quietly dying.
"""
import ast
import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


# ===========================================================================
# 1. _send_window grew a key per chat and never released it
# ===========================================================================

def _rate_limit_send():
    import dispatch
    return dispatch


@pytest.mark.asyncio
async def test_idle_chats_do_not_stay_in_the_rate_window(monkeypatch):
    """The leak.

    A bot invited to 5000 groups held 5000 deque keys forever, including
    after it left. Timestamps aged out; the keys did not. Nothing printed an
    error, because nothing was wrong from the code's point of view.

    The state below is the shape production produces: a chat that sent at
    least once keeps its last timestamp until its next send. An earlier
    version of this test filled the dict with EMPTY deques -- a state the
    code can never produce, because every key is created by a send and every
    send appends a timestamp. Against the emptiness-based eviction that test
    was green while the leak was still there.
    """
    import chat_utils
    from collections import deque
    chat_utils._send_window.clear()

    # Far past the cap, so the eviction branch is guaranteed to run.
    monkeypatch.setattr(chat_utils, "_SEND_WINDOW_MAX_KEYS", 16)
    stale = chat_utils._time.monotonic() - (chat_utils._RATE_WINDOW_SEC + 30)
    for i in range(64):
        chat_utils._send_window[i] = deque([stale])

    await chat_utils._rate_limit_send(0)
    assert len(chat_utils._send_window) == 1, (
        f"idle chat_ids must be evicted, still holding "
        f"{len(chat_utils._send_window)} keys"
    )
    chat_utils._send_window.clear()


@pytest.mark.asyncio
async def test_eviction_never_drops_a_live_chat(monkeypatch):
    """The cap must not cost correctness.

    An ACTIVE chat has timestamps inside the window. Evicting it would
    silently reset its rate-limit budget, which is a security hole, not a
    memory win. The idle fillers here are stale, so they are the ones that
    must go -- the live one must stay.
    """
    import chat_utils
    from collections import deque
    chat_utils._send_window.clear()
    monkeypatch.setattr(chat_utils, "_SEND_WINDOW_MAX_KEYS", 4)
    stale = chat_utils._time.monotonic() - (chat_utils._RATE_WINDOW_SEC + 30)
    for i in range(32):
        chat_utils._send_window[i] = deque([stale])
    chat_utils._send_window[999] = deque([chat_utils._time.monotonic()])

    await chat_utils._rate_limit_send(0)
    assert 999 in chat_utils._send_window, (
        "eviction removed a chat that still has live timestamps -- that would "
        "reset its rate-limit budget"
    )
    chat_utils._send_window.clear()


@pytest.mark.asyncio
async def test_rate_limiting_still_actually_limits(monkeypatch):
    """Guard against 'fixing' the leak by neutering the limiter."""
    import chat_utils
    from collections import deque
    chat_utils._send_window.clear()
    monkeypatch.setattr(chat_utils, "_RATE_WINDOW_SEC", 60.0)
    monkeypatch.setattr(chat_utils, "_RATE_MAX_PER_WINDOW", 3)
    monkeypatch.setattr(chat_utils, "_SEND_WINDOW_MAX_KEYS", 1024)

    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(chat_utils.asyncio, "sleep", _fake_sleep)
    for _ in range(6):
        await chat_utils._rate_limit_send(7)
    assert slept, "a 6th send inside a 3-per-60s window must sleep"
    chat_utils._send_window.clear()


def test_the_eviction_is_keyed_on_the_window_not_on_emptiness():
    """The criterion stays "expired", not "empty container".

    Pinned in the source as well as the behaviour, so deleting the runtime
    guard without deleting this test is visible in review. The behavioural
    test above is the real guard; this one says which notion of stale the
    implementation is allowed to use.
    """
    tree = ast.parse((BOT_DIR / "chat_utils.py").read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_evict_expired_windows"
    )
    body = ast.dump(fn)
    assert "_RATE_WINDOW_SEC" in body, (
        "the eviction must compare the newest timestamp against the window, "
        "not ask whether the container is empty"
    )
    assert "_send_window" in body


# ===========================================================================
# 2. Dispatch tasks were created and forgotten
# ===========================================================================

def test_polling_loop_tracks_dispatch_tasks():
    """Qwen finding: `asyncio.create_task(_dispatch_update(...))` dropped the
    reference.

    Two consequences, neither of which shows up as a failure:

      - nothing holds the task, so on interpreter shutdown an in-flight turn can
        be cancelled mid-write: user message persisted, assistant reply never
        made. A truncated conversation, not an error.
      - an unhandled exception inside the task dies to stderr with no update
        id attached. The bot goes quiet on that chat and there is no way to
        correlate the traceback to what the user sent.

    Checked against the AST rather than the raw text: the comment explaining
    this very finding quotes the old call, so a grep would match its own
    explanation.
    """
    # Block D moved the task factory to `dispatch_polling.py`, where it lives
    # with the loop that owns the tracking set.
    tree = ast.parse((BOT_DIR / "dispatch_polling.py").read_text())
    # Scope to the polling loop ONLY. _spawn_dispatch legitimately creates the
    # task -- that is the one call site that keeps a reference, so a global
    # search would flag the fix as the bug.
    untracked = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name == "_spawn_dispatch":
            continue
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            callee = n.func
            name = getattr(callee, "attr", None) or getattr(callee, "id", None)
            if name != "create_task":
                continue
            arg = n.args[0] if n.args else None
            if isinstance(arg, ast.Call) and \
                    getattr(arg.func, "id", None) == "_dispatch_update":
                untracked.append(f"{fn.name}:{n.lineno}")
    assert not untracked, (
        "these call sites create a _dispatch_update task without keeping a "
        f"reference: {untracked}"
    )
    src = (BOT_DIR / "dispatch_polling.py").read_text()
    assert "_dispatch_tasks" in src, "dispatch tasks must be tracked"
    assert "add_done_callback" in src, (
        "the tracking set needs a done-callback, or it only grows -- a second "
        "leak in the fix for the first"
    )


def test_task_failure_is_logged_with_the_update_id():
    """Without the id, a traceback in stderr cannot be tied to a conversation."""
    src = (BOT_DIR / "dispatch_polling.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_spawn_dispatch")
    body = ast.dump(fn)
    assert "update_id" in body, (
        "a failed dispatch task must be logged WITH its update id, otherwise "
        "the traceback is unactionable"
    )
    assert "exception" in body, "the done-callback must inspect t.exception()"


@pytest.mark.asyncio
async def test_spawn_dispatch_discards_the_task_when_done():
    """A set that only accumulates is the same leak wearing a fix's clothes."""
    import dispatch
    dispatch._dispatch_tasks.clear()

    async def _instant(upd):
        return None

    # Since TECH-DEBT #10 block D the callee is a keyword argument rather
    # than a module-level name: `dispatch_polling` imports nothing from
    # `dispatch`, so there is no global left to rebind here. Passing the
    # stub in is the supported way to drive it.
    try:
        task = dispatch._spawn_dispatch({"update_id": 1}, dispatch_update=_instant)
        assert task in dispatch._dispatch_tasks
        await task
        await asyncio.sleep(0)   # let the done-callback run
        assert task not in dispatch._dispatch_tasks, (
            "finished task was not discarded -- the tracking set leaks"
        )
    finally:
        dispatch._dispatch_tasks.clear()


@pytest.mark.asyncio
async def test_failed_task_is_discarded_and_logged(capsys):
    """A raising task must still leave the set, or one bad update wedges the
    tracking set forever."""
    import dispatch
    dispatch._dispatch_tasks.clear()

    async def _boom(upd):
        raise RuntimeError("kaboom")

    try:
        task = dispatch._spawn_dispatch({"update_id": 4242}, dispatch_update=_boom)
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
        assert task not in dispatch._dispatch_tasks
        out = capsys.readouterr().out
        assert "4242" in out and "kaboom" in out, (
            "a failed dispatch must be logged with its update id and the error"
        )
    finally:
        dispatch._dispatch_tasks.clear()