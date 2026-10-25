"""T70.5 — chunked summaries: the volume rule, the marker, the assembly.

The operator's decision (2026-10-24):

> сводки надо делать чанками, и помечать. т.е. если ОБЪЕМ данных для сводки
> > 200к надо сделав по ней сводку проставить в базе поле СВЕДЕНО, ссылку на
> сводку которая сделана и по этому чанку больше сводки не делать. Если объем
> данных для сводки <200к не проставлять поле сведено и не сводить, а грузить
> как есть в контекст. Ну и при загрузке соответсвенно грузить только готовые
> сводики и только сообщения которые не сведены.

(200k became 64k -- the arithmetic is in context_budget.py.)

These tests pin the three parts of that sentence separately, because they
fail differently:

  1. the VOLUME rule -- below the budget nothing is summarised and nothing
     is marked; above it, the old part is chunked and each chunk is
     summarised;
  2. the MARKER -- the link between a chunk and its summary lives in the
     database, and it is what makes "never again" true;
  3. the ASSEMBLY -- the context holds the finished summaries and the
     messages that were never summarised, in the order of the history.
"""
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
NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    from storage import Storage
    yield Storage(str(tmp_path / "t.db"))


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


def _budget(**kw):
    import context_budget as cb
    kw.setdefault("strategy", "summarize")
    kw.setdefault("anchor", NOW)
    return cb.ContextBudget(**kw)


# ---------------------------------------------------------------------------
# 1. the volume rule
# ---------------------------------------------------------------------------

def test_below_the_tail_budget_nothing_is_summarised_and_nothing_is_marked(
        store, monkeypatch):
    """«Если объем данных для сводки <200к не проставлять поле сведено и не
    сводить, а грузить как есть в контекст.»"""
    import summarize
    calls = []

    async def fake_call(url, model, old, timeout):
        calls.append(list(old))
        return "сводка"

    monkeypatch.setattr(summarize, "_call_model", fake_call)
    _add(store, 1, "t", [f"msg{i}" for i in range(10)], NOW - 30 * DAY)
    out = asyncio.run(summarize.summarise_thread(
        chat_id=1, thread_id="t", budget=_budget(summary_tail_tokens=10000),
        store=store, llama_url="http://x", model="m",
    ))
    assert out["ok"] is True
    assert out.get("skipped") is True
    assert calls == [], "the model must not be called below the budget"
    assert store.get_summaries(1, "t") == [], "nothing may be marked"
    # And the old part is still pending, i.e. loaded raw.
    assert len(store.get_pending_old(1, "t", NOW, 100)) == 10


def test_above_the_tail_budget_the_old_part_is_chunked_and_summarised(
        store, monkeypatch):
    import summarize
    calls = []

    async def fake_call(url, model, old, timeout):
        calls.append([m["content"] for m in old])
        return f"сводка {len(calls)}"

    monkeypatch.setattr(summarize, "_call_model", fake_call)
    _add(store, 1, "t", [f"msg{i}" for i in range(10)], NOW - 30 * DAY)
    out = asyncio.run(summarize.summarise_thread(
        chat_id=1, thread_id="t",
        budget=_budget(summary_tail_tokens=5, summary_chunk_tokens=5),
        store=store, llama_url="http://x", model="m",
    ))
    assert out["ok"] is True
    assert out["summarised"] >= 1
    assert len(calls) == out["summarised"]
    # Every chunk is a contiguous slice of the history, in order.
    flat = [m for call in calls for m in call]
    assert flat == [f"msg{i}" for i in range(len(flat))]
    # And every chunk got its own summary row.
    assert len(store.get_summaries(1, "t")) == out["summarised"]


def test_the_chunks_are_contiguous_and_do_not_overlap(store):
    """A chunk is a slice of the timeline. Two chunks covering the same
    message would summarise it twice."""
    import summarize
    ids = _add(store, 1, "t", [f"msg{i}" for i in range(20)], NOW - 30 * DAY)
    chunks = summarize.chunk_messages(
        [{"content": "x" * 100, "_id": i} for i in ids], chunk_tokens=200)
    seen = []
    for chunk in chunks:
        first = chunk[0]["_id"]
        last = chunk[-1]["_id"]
        assert last - first == len(chunk) - 1, "a chunk must be contiguous"
        seen.extend(m["_id"] for m in chunk)
    assert seen == ids


def test_a_message_bigger_than_the_chunk_budget_is_its_own_chunk():
    """It cannot be split. Refusing to summarise it would leave it pending
    forever."""
    import summarize
    msgs = [{"content": "x" * 10000, "_id": 1},
            {"content": "y", "_id": 2}]
    chunks = summarize.chunk_messages(msgs, chunk_tokens=10)
    assert [len(c) for c in chunks] == [1, 1]


# ---------------------------------------------------------------------------
# 2. the marker
# ---------------------------------------------------------------------------

def test_the_marker_points_at_the_summary_that_stands_for_the_chunk(store):
    ids = _add(store, 1, "t", ["a", "b", "c"], NOW - 30 * DAY)
    summary_id = store.save_summary_chunk(
        1, "t", "сводка", ids[0], ids[1], 2,
        NOW - 30 * DAY, NOW - 30 * DAY)
    summaries = store.get_summaries(1, "t")
    assert len(summaries) == 1
    assert summaries[0]["id"] == summary_id
    assert summaries[0]["n_messages"] == 2
    # The marked messages are no longer pending: the summary stands for them.
    pending = store.get_pending_old(1, "t", NOW, 100)
    assert [m["id"] for m in pending] == [ids[2]]


def test_a_summary_row_and_its_markers_are_written_in_one_transaction(store):
    """A marker without a summary is a hole in the memory: the messages
    would be neither loaded raw nor represented by a summary. A summary
    without a marker means the same history is summarised again at the next
    re-anchor. So they are written together, or not at all."""
    import sqlite3

    real_conn = store._conn()

    class FailingConn:
        """Delegates to the real connection, but the second statement fails."""
        def __init__(self, conn):
            self._conn = conn
            self.calls = 0

        def execute(self, *a, **k):
            self.calls += 1
            if self.calls > 1:
                raise sqlite3.OperationalError("disk on fire")
            return self._conn.execute(*a, **k)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    proxy = FailingConn(real_conn)
    store._local.conn = proxy
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.save_summary_chunk(1, "t", "сводка", 1, 2, 2)
    finally:
        store._local.conn = real_conn
    # Nothing was written: no summary row, no marker.
    assert store.get_summaries(1, "t") == []
    assert len(store.get_pending_old(1, "t", NOW, 100)) == 0


def test_an_orphaned_marker_brings_the_message_back_as_raw(store):
    """If the summary row is gone but the marker is not, the message must not
    disappear from the context. It comes back raw and is summarised again at
    the next re-anchor."""
    ids = _add(store, 1, "t", ["a", "b"], NOW - 30 * DAY)
    store.save_summary_chunk(1, "t", "сводка", ids[0], ids[1], 2,
                             NOW - 30 * DAY, NOW - 30 * DAY)
    store._conn().execute("DELETE FROM summaries")
    store._conn().commit()
    pending = store.get_pending_old(1, "t", NOW, 100)
    assert [m["id"] for m in pending] == ids


# ---------------------------------------------------------------------------
# 3. the assembly
# ---------------------------------------------------------------------------

def test_the_summary_stands_in_the_place_of_its_chunk():
    """The model must read the timeline in order: the summary of the old
    chunk comes before the raw messages that came after it."""
    import context_budget as cb
    b = _budget()
    old_raw = {"role": "user", "content": "сырое", "_created_at": NOW - 9 * DAY,
               "_id": 20}
    fresh = {"role": "user", "content": "свежее", "_created_at": NOW - DAY,
             "_id": 30}
    out = b.apply_summaries(
        [old_raw, fresh],
        [{"id": 1, "text": "сводка старого", "first_msg_id": 5,
          "last_created_at": NOW - 30 * DAY}],
    )
    contents = [m["content"] for m in out]
    assert contents.index("сводка старого") < contents.index("сырое")
    assert contents.index("сырое") < contents.index("свежее")


def test_a_newer_summary_comes_after_an_older_raw_chunk():
    """The partial-failure case: the OLDER chunk failed to summarise (its
    messages stay raw) and the NEWER chunk was summarised. The raw older
    messages must come first, or the model reads the timeline backwards.

    This is the case where "the summaries go at the head" would be wrong,
    and where a stable sort that ignores the position would pass the easy
    tests and fail here.
    """
    import context_budget as cb
    b = _budget()
    old_raw = {"role": "user", "content": "старое-сырое",
               "_created_at": NOW - 30 * DAY, "_id": 10}
    newer_raw = {"role": "user", "content": "новее-сырое",
                 "_created_at": NOW - 20 * DAY, "_id": 20}
    fresh = {"role": "user", "content": "свежее", "_created_at": NOW - DAY,
             "_id": 30}
    out = b.apply_summaries(
        [old_raw, newer_raw, fresh],
        [{"id": 1, "text": "сводка нового чанка", "first_msg_id": 15,
          "last_created_at": NOW - 20 * DAY}],
    )
    contents = [m["content"] for m in out]
    assert contents.index("старое-сырое") < contents.index("сводка нового чанка")
    assert contents.index("сводка нового чанка") < contents.index("новее-сырое")
    assert contents.index("новее-сырое") < contents.index("свежее")


def test_the_raw_tail_is_capped_and_the_newest_survives():
    """The tail budget is a budget: when the raw old part does not fit, the
    OLDEST raw messages are the ones that go. The recent past is the part
    worth keeping."""
    import context_budget as cb
    b = _budget(summary_tail_tokens=20, summary_context_tokens=10000)
    msgs = [{"role": "user", "content": "x" * 100, "_created_at": NOW - 9 * DAY,
             "_id": i} for i in range(5)]
    out = b.apply_summaries(msgs, [])
    contents = [m["content"] for m in out]
    # The newest message survives, the oldest does not.
    assert contents[-1] == "x" * 100
    assert len(contents) < 5


def test_the_old_part_is_capped_and_the_oldest_summary_goes():
    """Same rule for the summaries: when they do not fit, the oldest chunk's
    summary is dropped -- the distant past is the part that is worth the
    least."""
    import context_budget as cb
    b = _budget(summary_tail_tokens=10000, summary_context_tokens=50)
    summaries = [
        {"id": i, "text": "сводка " * 20, "first_msg_id": i,
         "last_created_at": NOW - 30 * DAY}
        for i in range(1, 4)
    ]
    out = b.apply_summaries([], summaries)
    bodies = [m["content"] for m in out if m["role"] == "assistant"]
    assert len(bodies) == 1, "only the newest summary fits"
    assert bodies[0] == "сводка " * 20


def test_the_fresh_window_keeps_its_message_cap():
    """The summarize strategy must not smuggle an unbounded fresh part into
    the prompt -- the window strategy caps it, so this one does too."""
    import context_budget as cb
    b = _budget(max_messages=3)
    msgs = [{"role": "user", "content": f"fresh{i}", "_created_at": NOW + 60,
             "_id": i} for i in range(10)]
    out = b.apply_summaries(msgs, [])
    assert [m["content"] for m in out] == ["fresh7", "fresh8", "fresh9"]


def test_the_estimator_over_counts_rather_than_under_counts():
    """A budget that is under-estimated is a budget the engine can refuse.
    The safety factor is what makes the estimate safe to spend."""
    import context_budget as cb
    text = "а" * 1000
    naive = len(text) / cb.CHARS_PER_TOKEN
    assert cb.estimate_tokens(text) > naive
    assert cb.estimate_tokens("") == 0
    # Multimodal: only the text parts count.
    parts = [{"type": "text", "text": "а" * 100},
             {"type": "image_url", "image_url": {"url": "data:..."}}]
    assert cb.estimate_tokens(parts) == cb.estimate_tokens("а" * 100)


# ---------------------------------------------------------------------------
# the whole path: store -> summarise -> assemble
# ---------------------------------------------------------------------------

def test_the_whole_path(store, monkeypatch):
    """One old chunk gets summarised, the rest of the old part is small
    enough to stay raw, and the context ends up with both -- in order."""
    import context_budget as cb
    import persistence
    import storage as storage_mod
    import summarize

    # 40 old messages of 100 chars each, plus 2 fresh ones.
    old_ids = _add(store, 1, "t", ["о" * 100] * 40, NOW - 30 * DAY)
    _add(store, 1, "t", ["свежее1", "свежее2"], NOW + 60)

    async def fake_call(url, model, old, timeout):
        return "сводка старого"

    monkeypatch.setattr(summarize, "_call_model", fake_call)
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)

    # 40 messages x ~31 tokens = ~1240 tokens: over the tail budget, so the
    # old part is chunked and summarised.
    budget = _budget(summary_tail_tokens=100, summary_chunk_tokens=100000)
    out = asyncio.run(summarize.summarise_thread(
        chat_id=1, thread_id="t", budget=budget,
        store=store, llama_url="http://x", model="m",
    ))
    assert out["summarised"] == 1
    assert len(store.get_summaries(1, "t")) == 1

    # Now assemble the context the way the request path does.
    monkeypatch.setattr(persistence, "get_budget", lambda: budget)
    history = asyncio.run(persistence.load_history(1, "t"))
    contents = [m.get("content") for m in history]
    assert "сводка старого" in contents
    assert "свежее1" in contents and "свежее2" in contents
    # The raw old messages are NOT there any more: the summary stands for
    # them. Saying the same thing twice is not "remembering better".
    assert "о" * 100 not in contents
    # And the summary comes before the fresh part.
    assert contents.index("сводка старого") < contents.index("свежее1")


def test_the_context_of_a_thread_with_nothing_old_is_just_the_window(store,
                                                                    monkeypatch):
    """The summarize strategy must not be worse than the window strategy when
    there is nothing to summarise."""
    import persistence
    import storage as storage_mod
    _add(store, 1, "t", ["свежее1", "свежее2"], NOW + 60)
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)
    budget = _budget()
    monkeypatch.setattr(persistence, "get_budget", lambda: budget)
    history = asyncio.run(persistence.load_history(1, "t"))
    assert [m.get("content") for m in history] == ["свежее1", "свежее2"]


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

def test_stats_shows_what_was_summarised(store, monkeypatch):
    """The operator reads /stats to answer "is it remembering". After T70.5
    the old part is no longer simply "not sent" -- part of it is compressed.
    The line has to say so."""
    import asyncio
    import storage as storage_mod
    from commands import stats_cmd
    _add(store, 1, "t", ["a", "b"], NOW - 30 * DAY)
    store.save_summary_chunk(1, "t", "сводка", 1, 2, 2, NOW - 30 * DAY,
                             NOW - 30 * DAY)
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)

    budget = _budget()
    monkeypatch.setattr(stats_cmd, "get_budget", lambda: budget)

    class FakeUpdate:
        class effective_chat:
            id = 1

        class effective_user:
            id = 1

        class message:
            message_thread_id = None

    sent = {}

    async def fake_reply(update, text, **kw):
        sent["text"] = text
        return None

    monkeypatch.setattr(stats_cmd, "_reply", fake_reply)
    monkeypatch.setattr(stats_cmd, "_is_group_chat", lambda u: False)
    monkeypatch.setattr(stats_cmd, "reject_if_unauthorized",
                        lambda u, c: _false())
    monkeypatch.setattr(stats_cmd, "_resolve_active", _resolve_active)

    asyncio.run(stats_cmd.stats(FakeUpdate(), None))
    text = sent["text"]
    assert "summarised" in text, text
    assert "not sent" not in text, (
        "the old part is no longer simply not sent -- the line would be a lie"
    )


async def _false():
    return False


async def _resolve_active(user_id):
    return "t"
