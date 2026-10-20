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
    """
    import dispatch
    dispatch._send_window.clear()

    # Far past the cap, so the eviction branch is guaranteed to run.
    monkeypatch.setattr(dispatch, "_SEND_WINDOW_MAX_KEYS", 16)
    for i in range(64):
        dispatch._send_window[i] = __import__("collections").deque()

    async def _one_send():
        await dispatch._rate_limit_send(0)

    await _one_send()
    assert len(dispatch._send_window) <= 17, (
        f"idle chat_ids must be evicted, still holding "
        f"{len(dispatch._send_window)} keys"
    )
    dispatch._send_window.clear()


@pytest.mark.asyncio
async def test_eviction_never_drops_a_live_chat(monkeypatch):
    """The cap must not cost correctness.

    An ACTIVE chat has non-empty timestamps. Evicting it would silently reset
    its rate-limit budget, which is a security hole, not a memory win.
    """
    import dispatch
    from collections import deque
    dispatch._send_window.clear()
    monkeypatch.setattr(dispatch, "_SEND_WINDOW_MAX_KEYS", 4)
    for i in range(32):
        dispatch._send_window[i] = deque()
    dispatch._send_window[999] = deque([dispatch._time.monotonic()])

    await dispatch._rate_limit_send(0)
    assert 999 in dispatch._send_window, (
        "eviction removed a chat that still has live timestamps -- that would "
        "reset its rate-limit budget"
    )
    dispatch._send_window.clear()


@pytest.mark.asyncio
async def test_rate_limiting_still_actually_limits(monkeypatch):
    """Guard against 'fixing' the leak by neutering the limiter."""
    import dispatch
    from collections import deque
    dispatch._send_window.clear()
    monkeypatch.setattr(dispatch, "_RATE_WINDOW_SEC", 60.0)
    monkeypatch.setattr(dispatch, "_RATE_MAX_PER_WINDOW", 3)
    monkeypatch.setattr(dispatch, "_SEND_WINDOW_MAX_KEYS", 1024)

    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(dispatch.asyncio, "sleep", _fake_sleep)
    for _ in range(6):
        await dispatch._rate_limit_send(7)
    assert slept, "a 6th send inside a 3-per-60s window must sleep"
    dispatch._send_window.clear()


def test_rate_window_source_has_eviction():
    """Pinned in the source as well as the behaviour, so deleting the runtime
    guard without deleting this test is visible in review."""
    src = (BOT_DIR / "dispatch.py").read_text()
    assert "_SEND_WINDOW_MAX_KEYS" in src
    assert "stale_chat" in src


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
    tree = ast.parse((BOT_DIR / "dispatch.py").read_text())
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
    src = (BOT_DIR / "dispatch.py").read_text()
    assert "_dispatch_tasks" in src, "dispatch tasks must be tracked"
    assert "add_done_callback" in src, (
        "the tracking set needs a done-callback, or it only grows -- a second "
        "leak in the fix for the first"
    )


def test_task_failure_is_logged_with_the_update_id():
    """Without the id, a traceback in stderr cannot be tied to a conversation."""
    src = (BOT_DIR / "dispatch.py").read_text()
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

    original = dispatch._dispatch_update
    dispatch._dispatch_update = _instant
    try:
        task = dispatch._spawn_dispatch({"update_id": 1})
        assert task in dispatch._dispatch_tasks
        await task
        await asyncio.sleep(0)   # let the done-callback run
        assert task not in dispatch._dispatch_tasks, (
            "finished task was not discarded -- the tracking set leaks"
        )
    finally:
        dispatch._dispatch_update = original
        dispatch._dispatch_tasks.clear()


@pytest.mark.asyncio
async def test_failed_task_is_discarded_and_logged(capsys):
    """A raising task must still leave the set, or one bad update wedges the
    tracking set forever."""
    import dispatch
    dispatch._dispatch_tasks.clear()

    async def _boom(upd):
        raise RuntimeError("kaboom")

    original = dispatch._dispatch_update
    dispatch._dispatch_update = _boom
    try:
        task = dispatch._spawn_dispatch({"update_id": 4242})
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
        assert task not in dispatch._dispatch_tasks
        out = capsys.readouterr().out
        assert "4242" in out and "kaboom" in out, (
            "a failed dispatch must be logged with its update id and the error"
        )
    finally:
        dispatch._dispatch_update = original
        dispatch._dispatch_tasks.clear()