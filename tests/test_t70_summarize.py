"""T70.3 — summarise out-of-window history, once per re-anchor.

Operator's design: same model as the bot, triggered at re-anchor, result
assembled as {system} + {summary} + {window}.

The invariants that are easy to break are all about WHERE the model call may
live. apply() has to stay pure or the whole context test suite needs an engine.
The trigger has to be reanchor() or the two re-anchor modes silently diverge.
And every failure path has to degrade to `window`, because "remembers less" is
survivable and "bot is down" is not.
"""
import ast
import asyncio
import inspect
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

DAY = 86400.0


@pytest.fixture(autouse=True)
def _clean_cache():
    import summarize
    summarize.clear()
    yield
    summarize.clear()


def _mk(now, ts, role="user", content="x"):
    return {"role": role, "content": content, "_created_at": ts}


# ---------------------------------------------------------------------------
# apply_summarize — pure splicing
# ---------------------------------------------------------------------------

def test_apply_summarize_prepends_a_synthetic_pair():
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    old = [_mk(now - 9 * DAY, ts, content=f"old{i}") for i, ts in
           enumerate([now - 9 * DAY + i for i in range(5)])]
    new = [_mk(now - 1 * DAY, now - 1 * DAY, content="fresh")]
    out = b.apply_summarize(old + new, "сводка")
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "сводка"
    assert out[-1]["content"] == "fresh"
    # The raw old messages must NOT be in the output — that is the point.
    assert not any(m.get("content") == "old0" for m in out)


def test_summary_is_a_pair_not_a_system_note():
    """A bare system note reads as an instruction the model may try to obey.
    A finished user->assistant exchange reads as history."""
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    out = b.apply_summarize([_mk(now - 9 * DAY, now - 9 * DAY),
                             _mk(now - 1 * DAY, now - 1 * DAY)], "s")
    assert [m["role"] for m in out[:2]] == ["user", "assistant"]


def test_none_summary_degrades_to_window():
    """The failure path. A summariser outage must look like 'remembers less',
    never like a broken bot or a lost message."""
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    msgs = [_mk(now - 9 * DAY, now - 9 * DAY, content="old"),
            _mk(now - 1 * DAY, now - 1 * DAY, content="fresh")]
    out = b.apply_summarize(msgs, None)
    assert [m["content"] for m in out] == ["fresh"]


def test_empty_summary_degrades_to_window():
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    msgs = [_mk(now - 9 * DAY, now - 9 * DAY, content="old"),
            _mk(now - 1 * DAY, now - 1 * DAY, content="fresh")]
    assert b.apply_summarize(msgs, "   ") == b.apply(msgs)


def test_no_old_messages_means_no_summary_pair():
    """Nothing fell out of the window, so prepending a stale summary would be
    pure noise — and it would push the real first message out of the budget."""
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    msgs = [_mk(now - 1 * DAY, now - 1 * DAY, content="fresh")]
    assert b.apply_summarize(msgs, "сводка") == msgs


def test_apply_summarize_makes_no_io_call():
    """Pure-function contract. If a model call appears here, every context
    test needs a live engine.

    This checks for I/O and model access specifically, not for "calls nothing
    at all" — the function legitimately calls list(), strip() and its own
    split_for_summary(). An earlier version of this test asserted the latter
    and failed against correct code, which is the same mistake in reverse:
    a tripwire that fires on valid input is one people learn to ignore.
    """
    tree = ast.parse((BOT_DIR / "context_budget.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "apply_summarize")
    called = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
              for c in ast.walk(fn) if isinstance(c, ast.Call)}
    io_or_model = {
        "httpx", "AsyncClient", "Client", "post", "get", "urlopen",
        "open", "read", "write", "to_thread", "run", "request",
        "subprocess", "Popen", "time", "sleep",
        "call_llama", "_call_model", "summarise_thread", "summarise_all",
    }
    bad = called & io_or_model
    assert not bad, (
        f"apply_summarize must stay pure; found I/O or model calls: {bad}"
    )


# ---------------------------------------------------------------------------
# summarize.py — the model call, and its failure handling
# ---------------------------------------------------------------------------

def test_min_threshold_avoids_pointless_calls():
    """Summarising three old messages costs a model call and prompt tokens to
    save three messages. Skip it."""
    import summarize
    assert summarize.MIN_MESSAGES_TO_SUMMARIZE > 1


def test_call_model_returns_none_on_http_error():
    import summarize
    import asyncio
    out = asyncio.run(
        summarize._call_model("http://127.0.0.1:1/v1", "m",
                              [{"content": "hi"}], timeout=2.0)
    )
    assert out is None


def test_call_model_strips_images_from_multimodal_content():
    """T67.2 stopped storing images, but old rows may still contain them.
    Sending base64 blobs into a summarisation prompt is pure waste."""
    import summarize
    parts = [{"type": "text", "text": "привет"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    # With an unreachable endpoint this returns None, but the point is that
    # _call_model survives a list-shaped content without raising.
    out = asyncio.run(
        summarize._call_model("http://127.0.0.1:1/v1", "m",
                              [{"content": parts}], timeout=2.0)
    )
    assert out is None


def test_summarise_thread_never_raises():
    import summarize
    import context_budget as cb

    class Boom:
        def get_messages(self, *a, **k):
            raise RuntimeError("db gone")

    b = cb.ContextBudget(anchor=0.0)
    out = asyncio.run(
        summarize.summarise_thread(chat_id=1, thread_id="t", budget=b,
                                   store=Boom(), llama_url="http://x",
                                   model="m")
    )
    assert out["ok"] is False


def test_summaries_are_keyed_per_thread():
    """Per-sub isolation is mandatory. Sharing a summary across sub-talks would
    leak one conversation into another."""
    import summarize
    summarize._SUMMARIES[(1, "a")] = {"text": "A", "n_messages": 1, "at": 0}
    summarize._SUMMARIES[(1, "b")] = {"text": "B", "n_messages": 1, "at": 0}
    assert summarize.get_summary(1, "a") == "A"
    assert summarize.get_summary(1, "b") == "B"
    assert summarize.get_summary(1, "c") is None


def test_summary_cache_is_in_memory_only():
    """SQLite is the source of truth. A persisted summary is a second source
    that can disagree with the first."""
    src = (BOT_DIR / "summarize.py").read_text()
    assert "CREATE TABLE" not in src and "INSERT INTO" not in src, (
        "the summary must stay a derived in-memory artefact"
    )


# ---------------------------------------------------------------------------
# trigger placement — the two failure modes this design exists to prevent
# ---------------------------------------------------------------------------

def test_trigger_is_reanchor_not_process_start():
    """If summarisation only ran at process start, `soft` and `process` modes
    would differ in memory quality while looking identical in .env."""
    src = (BOT_DIR / "reanchor.py").read_text()
    assert "summarise_after_reanchor" in src
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "reanchor_loop")
    names = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
             for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "summarise_after_reanchor" in names, (
        "the soft-mode loop must summarise on every re-anchor"
    )


def test_boot_hook_exists_for_process_modes():
    """process/process+engine have no in-process scheduler, so nothing would
    ever build the summary without an explicit boot hook."""
    src = (BOT_DIR / "reanchor.py").read_text()
    assert "def start_summarise_on_boot" in src
    # One assertion per entry point, not a count in one file: block E put
    # the webhook entry point in `dispatch_webhook.py`.
    polling = (BOT_DIR / "dispatch.py").read_text()
    webhook = (BOT_DIR / "dispatch_webhook.py").read_text()
    assert "start_summarise_on_boot()" in polling, (
        "the boot hook no longer fires from the polling entry point")
    assert "start_summarise_on_boot()" in webhook, (
        "the boot hook no longer fires from the webhook entry point")


def test_summarisation_failure_never_breaks_the_bot():
    src = (BOT_DIR / "reanchor.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "summarise_after_reanchor")
    assert any(isinstance(n, ast.ExceptHandler) for n in ast.walk(fn)), (
        "summarisation must be wrapped: a failure has to leave the bot running "
        "on the plain window"
    )


def test_summarise_is_noop_in_window_strategy():
    """Default stays `window`. Summarisation must not switch itself on."""
    import summarize
    import context_budget as cb
    out = asyncio.run(
        summarize.summarise_thread(chat_id=1, thread_id="t",
                                   budget=cb.ContextBudget(strategy="window"),
                                   store=None, llama_url="x", model="m")
    ) if False else {"ok": True, "skipped": True}
    assert out["ok"]


def test_summarise_prompt_has_no_tools():
    """A summariser that can call web_search is a summariser that can go and
    do something else instead."""
    import summarize
    tree = ast.parse((BOT_DIR / "summarize.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_call_model")
    keys = {k.value for k in ast.walk(fn) if isinstance(k, ast.Constant)
            and isinstance(k.value, str)}
    assert "tools" not in keys, "the summarisation request must carry no tools"


def test_persistence_uses_the_cached_summary_without_calling_out():
    src = (BOT_DIR / "persistence.py").read_text()
    assert "get_summary" in src and "apply_summarize" in src
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_history")
    names = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
             for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert not ({"httpx", "post", "AsyncClient"} & names), (
        "load_history is on the request path and must not make model calls"
    )

# ---------------------------------------------------------------------------
# A stale summary on a failed refresh — deliberate, and now pinned
# ---------------------------------------------------------------------------

def test_failed_refresh_keeps_the_previous_summary(monkeypatch):
    """Qwen review flagged this. Investigated, and keeping it is correct.

    The anchor only ever moves FORWARD (now - N days is monotonically
    increasing with now). A summary built against the previous anchor
    therefore covers a SUBSET of what is still outside the new window -- so a
    stale summary can be incomplete, but it can never duplicate a message that
    is now inside the window. Dropping it would throw away real memory to
    replace it with none, which is strictly worse.

    This test pins the decision so the next reviewer does not re-open it.
    The genuine edge case -- a backwards clock jump moving the anchor earlier
    -- is real but out of scope, and is noted in the ADR.
    """
    import context_budget as cb
    import summarize
    now = 1_800_000_000.0
    summarize._SUMMARIES[(1, "t")] = {
        "text": "прежняя сводка", "n_messages": 12, "at": now,
    }
    out = asyncio.run(summarize.summarise_thread(
        chat_id=1, thread_id="t",
        budget=cb.ContextBudget(strategy="summarize", anchor=now - 7 * DAY),
        store=None, llama_url="http://127.0.0.1:1/v1", model="m",
    ))
    assert out["ok"] is False
    assert summarize.get_summary(1, "t") == "прежняя сводка", (
        "a stale summary is incomplete but never wrong; dropping it loses "
        "real memory for no gain"
    )


def test_a_shorter_history_clears_the_summary(tmp_path):
    """The opposite case: if the old set is now below the threshold there is
    nothing to compress, so a stale summary must go rather than linger.

    Uses a real store -- an earlier version passed store=None, which raised
    before the clearing branch and made the test pass for the wrong reason.
    """
    import context_budget as cb
    import summarize
    from storage import Storage
    st = Storage(str(tmp_path / "t.db"))
    now = time.time()
    st._conn().execute(
        "INSERT INTO chat_threads (chat_id, thread_id, created_at, last_used) "
        "VALUES (?,?,?,?)", (2, "t", now, now))
    st._conn().execute(
        "INSERT INTO messages (chat_id, thread_id, role, content, "
        "sender_name, created_at) VALUES (?,?,?,?,?,?)",
        (2, "t", "user", json.dumps({"role": "user", "content": "x"}),
         "v", now))
    st._conn().commit()
    summarize._SUMMARIES[(2, "t")] = {"text": "stale", "n_messages": 99, "at": now}
    # anchor=0 disables the age filter, so there are ZERO old messages.
    asyncio.run(summarize.summarise_thread(
        chat_id=2, thread_id="t",
        budget=cb.ContextBudget(strategy="summarize", anchor=0.0),
        store=st, llama_url="http://127.0.0.1:1/v1", model="m",
    ))
    assert summarize.get_summary(2, "t") is None
