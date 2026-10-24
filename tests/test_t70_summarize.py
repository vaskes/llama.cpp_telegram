"""T70.3 / T70.5 — summarise out-of-window history, in chunks, once per chunk.

The invariants that are easy to break are all about WHERE the model call may
live. apply_summaries() has to stay pure or the whole context test suite
needs an engine. The trigger has to be reanchor() or the two re-anchor modes
silently diverge. And every failure path has to degrade to the raw tail,
because "remembers less" is survivable and "bot is down" is not.

T70.5 changed one of the old rules on purpose: the summary used to be an
in-memory artefact rebuilt at every re-anchor. The operator's decision
(2026-10-24) is that each chunk is summarised ONCE, ever, and the link
between a chunk and its summary lives in the database:

    > проставить в базе поле СВЕДЕНО, ссылку на сводку которая сделана и по
    > этому чанку больше сводки не делать

So the summary is persisted, and the marker is what makes "never again"
true. The tests below pin that: a marked chunk is not summarised a second
time, and a failed chunk stays unmarked so it is retried.
"""
import ast
import asyncio
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


@pytest.fixture
def store(tmp_path):
    from storage import Storage
    s = Storage(str(tmp_path / "t.db"))
    yield s


def _mk(now, ts, role="user", content="x", mid=None, summary_id=None):
    m = {"role": role, "content": content, "_created_at": ts}
    if mid is not None:
        m["_id"] = mid
    if summary_id is not None:
        m["_summary_id"] = summary_id
    return m


def _summary(sid, text, first_msg_id, last_created_at=1_700_000_000.0):
    return {"id": sid, "text": text, "first_msg_id": first_msg_id,
            "last_created_at": last_created_at}


def _add(store, chat, thread, texts, created_at):
    """Insert messages, return their row ids in order."""
    c = store._conn()
    ids = []
    for t in texts:
        cur = c.execute(
            "INSERT INTO messages (chat_id, thread_id, role, content, "
            "sender_name, created_at) VALUES (?,?,?,?,?,?)",
            (chat, thread, "user", json.dumps({"role": "user", "content": t}),
             "u", created_at),
        )
        ids.append(int(cur.lastrowid))
    c.commit()
    return ids


# ---------------------------------------------------------------------------
# apply_summaries — pure splicing
# ---------------------------------------------------------------------------

def test_apply_summaries_prepends_a_synthetic_pair():
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    old = [_mk(now - 9 * DAY, now - 9 * DAY, content=f"old{i}", mid=i,
               summary_id=1) for i in range(5)]
    new = [_mk(now - 1 * DAY, now - 1 * DAY, content="fresh", mid=99)]
    out = b.apply_summaries(old + new, [_summary(1, "сводка", 0)])
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
    out = b.apply_summaries(
        [_mk(now - 9 * DAY, now - 9 * DAY, mid=1, summary_id=1),
         _mk(now - 1 * DAY, now - 1 * DAY, mid=2)],
        [_summary(1, "s", 1)],
    )
    assert [m["role"] for m in out[:2]] == ["user", "assistant"]


def test_no_summary_means_the_old_part_is_loaded_raw():
    """T70.5, the operator's rule: below the tail budget the old part is
    NOT summarised and NOT dropped — "не сводить, а грузить как есть".

    This is a change of behaviour against the old design, where anything
    outside the window was simply not sent.
    """
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    msgs = [_mk(now - 9 * DAY, now - 9 * DAY, content="old", mid=1),
            _mk(now - 1 * DAY, now - 1 * DAY, content="fresh", mid=2)]
    out = b.apply_summaries(msgs, [])
    assert [m["content"] for m in out] == ["old", "fresh"]


def test_an_empty_summary_text_is_not_a_summary():
    """A summary row with no text is not a summary. It must not become an
    empty assistant turn in the prompt."""
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    msgs = [_mk(now - 9 * DAY, now - 9 * DAY, content="old", mid=1)]
    out = b.apply_summaries(msgs, [_summary(1, "   ", 1)])
    assert [m["content"] for m in out] == ["old"]


def test_a_summary_whose_chunk_is_inside_the_window_is_not_used():
    """The messages themselves are in the context then. Adding the summary
    too would say the same stretch of history twice."""
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    msgs = [_mk(now - 1 * DAY, now - 1 * DAY, content="fresh", mid=1)]
    out = b.apply_summaries(msgs, [_summary(1, "сводка", 1,
                                            last_created_at=now - DAY)])
    assert [m["content"] for m in out] == ["fresh"]


def test_a_fully_compressed_old_part_is_still_in_the_context():
    """When every old message is covered by a summary, the summaries ARE the
    old part of the history. The caller passes no raw old message, and the
    summary must still be there -- otherwise the bot silently forgets the
    whole compressed past.

    (The old design had the opposite rule: a summary with no old messages
    behind it was "stale" and was dropped. That was a symptom of the summary
    being an in-memory artefact rebuilt every re-anchor. Now the summary
    points at the messages it stands for, so "stale" cannot happen -- and
    the case where it COULD happen (the chunk is inside the window) is
    covered by the test above.)
    """
    import context_budget as cb
    now = 1_800_000_000.0
    b = cb.ContextBudget(anchor=now - 7 * DAY)
    msgs = [_mk(now - 1 * DAY, now - 1 * DAY, content="fresh", mid=1)]
    out = b.apply_summaries(msgs, [_summary(1, "сводка", 0,
                                            last_created_at=now - 9 * DAY)])
    assert [m["content"] for m in out] == [
        "Сводка разговора вне окна памяти (часть 1):", "сводка", "fresh",
    ]


def test_apply_summaries_makes_no_io_call():
    """Pure-function contract. If a model call appears here, every context
    test needs a live engine.

    This checks for I/O and model access specifically, not for "calls nothing
    at all" — the function legitimately calls list(), .get(), strip() and its
    own split_for_summary(). An earlier version of this test asserted the
    latter and failed against correct code, which is the same mistake in
    reverse: a tripwire that fires on valid input is one people learn to
    ignore.

    So the check is split in two: the FREE names (a module or a function
    called by name) and the ATTRIBUTE names (a method called on something).
    `dict.get` is an attribute call, not I/O, and a name-based ban list
    cannot tell the two apart -- which is why "get" is not on the list.
    """
    tree = ast.parse((BOT_DIR / "context_budget.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "apply_summaries")
    free = {c.func.id for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    attrs = {c.func.attr for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    banned_names = {
        "httpx", "asyncio", "open", "urlopen", "to_thread", "run_in_executor",
        "subprocess", "Popen", "time", "sleep", "requests", "urllib",
        "socket", "os", "io",
    }
    banned_attrs = {
        "urlopen", "to_thread", "run_in_executor", "AsyncClient", "Client",
        "Popen", "open", "read_text", "write_text", "urlretrieve",
    }
    bad = (free & banned_names) | (attrs & banned_attrs)
    assert not bad, (
        f"apply_summaries must stay pure; found I/O or model calls: {bad}"
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

        def get_pending_old(self, *a, **k):
            raise RuntimeError("db gone")

    # A non-zero anchor, so the age filter does not short-circuit before the
    # store is touched.
    b = cb.ContextBudget(anchor=1.0)
    out = asyncio.run(
        summarize.summarise_thread(chat_id=1, thread_id="t", budget=b,
                                   store=Boom(), llama_url="http://x",
                                   model="m")
    )
    assert out["ok"] is False


def test_summaries_are_keyed_per_thread(store):
    """Per-sub isolation is mandatory. Sharing a summary across sub-talks would
    leak one conversation into another."""
    store.save_summary_chunk(1, "a", "A", 1, 1, 1)
    store.save_summary_chunk(1, "b", "B", 2, 2, 1)
    a = store.get_summaries(1, "a")
    b = store.get_summaries(1, "b")
    assert [s["text"] for s in a] == ["A"]
    assert [s["text"] for s in b] == ["B"]
    assert store.get_summaries(1, "c") == []


def test_the_summary_is_persisted_with_a_link_to_its_chunk(store):
    """T70.5, the operator's rule: "проставить в базе поле СВЕДЕНО, ссылку на
    сводку которая сделана".

    The old rule was the opposite — the summary was an in-memory artefact,
    because "a persisted summary is a second source of truth that can
    disagree with the first". The link is what makes persistence safe: the
    summary says which messages it stands for, so the two can be checked
    against each other instead of merely believed.
    """
    ids = _add(store, 1, "t", ["a", "b", "c"], 1_700_000_000.0)
    summary_id = store.save_summary_chunk(1, "t", "сводка", ids[0], ids[1], 2)
    marked = store.get_pending_old(1, "t", 1_800_000_000.0, 100)
    # The marked messages are NOT pending any more: the summary stands for
    # them, so they are not loaded raw.
    assert [m["id"] for m in marked] == [ids[2]]
    summaries = store.get_summaries(1, "t")
    assert len(summaries) == 1
    assert summaries[0]["id"] == summary_id
    assert summaries[0]["text"] == "сводка"
    assert summaries[0]["first_msg_id"] == ids[0]
    assert summaries[0]["last_msg_id"] == ids[1]
    assert summaries[0]["n_messages"] == 2


def test_a_marked_chunk_is_never_summarised_again(store, monkeypatch):
    """The point of the marker. A second re-anchor must not summarise the
    same history again — that was the whole reason for persisting the
    summary."""
    import context_budget as cb
    import summarize
    ids = _add(store, 1, "t", ["a", "b", "c"], 1_700_000_000.0)
    store.save_summary_chunk(1, "t", "сводка", ids[0], ids[2], 3)
    called = []

    async def fake_call(url, model, old, timeout):
        called.append(list(old))
        return "НЕ ДОЛЖНО ВЫЗЫВАТЬСЯ"

    monkeypatch.setattr(summarize, "_call_model", fake_call)
    out = asyncio.run(summarize.summarise_thread(
        chat_id=1, thread_id="t",
        budget=cb.ContextBudget(strategy="summarize",
                                anchor=1_800_000_000.0),
        store=store, llama_url="http://127.0.0.1:1/v1", model="m",
    ))
    assert out["ok"] is True
    assert called == [], "a marked chunk must not be summarised again"
    assert len(store.get_summaries(1, "t")) == 1


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


def test_persistence_uses_the_stored_summaries_without_calling_out():
    src = (BOT_DIR / "persistence.py").read_text()
    assert "get_summaries" in src and "apply_summaries" in src
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_history")
    names = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
             for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert not ({"httpx", "post", "AsyncClient"} & names), (
        "load_history is on the request path and must not make model calls"
    )


# ---------------------------------------------------------------------------
# A failed chunk — deliberate, and now pinned
# ---------------------------------------------------------------------------

def test_a_failed_chunk_stays_unmarked_and_is_retried(store):
    """A summarisation outage must not lose history.

    The chunk whose model call failed is left unmarked, so it is loaded raw
    into the context and summarised again at the next re-anchor. The chunks
    that DID succeed keep their summaries.
    """
    import context_budget as cb
    import summarize
    now = 1_800_000_000.0
    _add(store, 1, "t", ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"],
         now - 30 * DAY)
    calls = []

    async def fake_call(url, model, old, timeout):
        calls.append(len(old))
        return None  # the engine is down

    original = summarize._call_model
    summarize._call_model = fake_call
    try:
        out = asyncio.run(summarize.summarise_thread(
            chat_id=1, thread_id="t",
            budget=cb.ContextBudget(strategy="summarize", anchor=now,
                                    summary_tail_tokens=5,
                                    summary_chunk_tokens=5),
            store=store, llama_url="http://127.0.0.1:1/v1", model="m",
        ))
    finally:
        summarize._call_model = original
    assert out["ok"] is True
    assert out["failed"] >= 1
    assert store.get_summaries(1, "t") == []
    # Nothing was marked, so the whole old part is still pending and will be
    # retried -- nothing is lost, and nothing is silently dropped.
    pending = store.get_pending_old(1, "t", now, 100)
    assert len(pending) == 10
