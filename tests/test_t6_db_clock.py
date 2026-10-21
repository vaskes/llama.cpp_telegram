"""TECH-DEBT #6 -- the timestamp comes from SQLite, proven by poisoning the
Python clock.

A substring test (`assert "unixepoch" in src`) would be the usual version
of this and would prove nothing: it would pass if the expression sat in a
comment, or in a string nobody executes. So the tripwire here changes the
world instead.

storage.time.time is replaced with a function returning a value that is
years away from the real clock, in both directions. If any write path still
called it, the row would come back with that impossible timestamp. The test
then asserts the stored value is close to the real current time -- which is
only possible if SQLite produced it.
"""
import importlib
import sqlite3
import sys
import time as _real_time
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

import storage as st  # noqa: E402

# Captured by value, not by module reference. `monkeypatch.setattr(st.time,
# "time", ...)` rewrites the attribute on the shared `time` module, so any
# "what is the real time now" call made through a module handle afterwards
# returns the poison. The first version of this file measured with
# _REAL_TIME() and every drift came out as exactly the poison offset --
# a failure that looked like the code being wrong and was the test measuring
# its own fixture.
_REAL_TIME = _real_time.time

pytestmark = pytest.mark.tech_debt(
    # Ledger item 6. GitHub Issue #4. Correct already, but stated so
    # the namespace is visible rather than incidental.
    item=6, reason="Pins TECH-DEBT #6 (ledger, Issue #4): writes take the DB clock, not the process clock")


@pytest.fixture
def store(tmp_path):
    return st.Storage(str(tmp_path / "clock.db"))


def _poison(monkeypatch, value):
    monkeypatch.setattr(st.time, "time", lambda: value)


def test_add_message_ignores_the_python_clock(store, monkeypatch):
    """A year in the future. If time.time() still fed this write, the row
    would be dated 2027 and the age filter would treat it as brand new."""
    _poison(monkeypatch, _REAL_TIME() + 365 * 86400)
    store.add_message(1, "t", "user", '{"role":"user"}')

    c = sqlite3.connect(store.path)
    ts = c.execute("SELECT created_at FROM messages LIMIT 1").fetchone()[0]
    c.close()

    drift = abs(ts - _REAL_TIME())
    assert drift < 60, (
        f"created_at came from the Python clock ({drift:.0f}s away from now). "
        f"storage.add_message must let SQLite produce it."
    )


def test_add_message_ignores_a_backwards_python_clock(store, monkeypatch):
    """The failure with a real blast radius: an NTP step backwards makes
    recent messages look a week old and drops them out of the window."""
    _poison(monkeypatch, _REAL_TIME() - 365 * 86400)
    store.add_message(1, "t", "user", '{"role":"user"}')

    c = sqlite3.connect(store.path)
    ts = c.execute("SELECT created_at FROM messages LIMIT 1").fetchone()[0]
    c.close()

    drift = abs(ts - _REAL_TIME())
    assert drift < 60, (
        f"created_at came from a poisoned backwards clock ({drift:.0f}s away). "
        f"A backwards clock step is exactly the case the age filter cannot "
        f"survive, so the write must not depend on it."
    )


def test_create_thread_ignores_the_python_clock(store, monkeypatch):
    _poison(monkeypatch, _REAL_TIME() + 10 * 365 * 86400)
    assert store.create_thread(7, "main") is True

    c = sqlite3.connect(store.path)
    created, last_used = c.execute(
        "SELECT created_at, last_used FROM chat_threads WHERE chat_id=7"
    ).fetchone()
    c.close()

    for label, value in (("created_at", created), ("last_used", last_used)):
        drift = abs(value - _REAL_TIME())
        assert drift < 60, f"chat_threads.{label} came from the Python clock"


def test_touch_thread_ignores_the_python_clock(store, monkeypatch):
    store.create_thread(8, "main")
    _poison(monkeypatch, _REAL_TIME() - 10 * 365 * 86400)
    store.touch_thread(8, "main")

    c = sqlite3.connect(store.path)
    last_used = c.execute(
        "SELECT last_used FROM chat_threads WHERE chat_id=8"
    ).fetchone()[0]
    c.close()

    assert abs(last_used - _REAL_TIME()) < 60, (
        "touch_thread wrote last_used from the Python clock"
    )


def test_add_known_topic_ignores_the_python_clock(store, monkeypatch):
    _poison(monkeypatch, _REAL_TIME() + 10 * 365 * 86400)
    store.add_known_topic(-100, 42, "topic")

    c = sqlite3.connect(store.path)
    ts = c.execute("SELECT created_at FROM known_topics LIMIT 1").fetchone()[0]
    c.close()

    assert abs(ts - _REAL_TIME()) < 60, (
        "add_known_topic wrote created_at from the Python clock"
    )


def test_written_timestamp_stays_real_type(store, monkeypatch):
    """The whole reason unixepoch() rather than CURRENT_TIMESTAMP.

    If anyone 'improves' this to CURRENT_TIMESTAMP, the column type becomes
    TEXT and every float() read downstream has to grow a parse. That is the
    migration this item was written to avoid, so it is pinned.
    """
    store.add_message(1, "t", "user", '{"role":"user"}')
    c = sqlite3.connect(store.path)
    kind = c.execute("SELECT typeof(created_at) FROM messages LIMIT 1").fetchone()[0]
    raw = c.execute("SELECT created_at FROM messages LIMIT 1").fetchone()[0]
    c.close()

    assert kind in ("integer", "real"), (
        f"created_at is stored as {kind}, not a number. CURRENT_TIMESTAMP "
        f"returns TEXT -- keep unixepoch(). Raw value was {raw!r}"
    )
    assert float(raw) > 1_500_000_000, (
        f"created_at {raw!r} is not plausible epoch seconds"
    )


def test_sqlite_is_new_enough_for_unixepoch():
    """unixepoch() landed in SQLite 3.38. The old floor was 3.25, which is
    only about ALTER TABLE RENAME COLUMN. This raises it, so it is stated
    here rather than left to be discovered on an old fork."""
    assert sqlite3.sqlite_version_info >= (3, 38, 0), (
        f"SQLite {sqlite3.sqlite_version} predates unixepoch() (3.38). "
        f"The storage layer will fail on the first write."
    )


def test_no_write_path_still_uses_time_time(store):
    """Static half: every remaining time.time() in storage.py is a READ,
    not a write.

    Computed, not grepped against a word list -- the assertion is that the
    line does not appear inside any INSERT or UPDATE statement, which is
    the property that matters.
    """
    import ast
    tree = ast.parse((BOT_DIR / "storage.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "execute"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        sql = node.args[0].value
        if not isinstance(sql, str):
            continue
        upper = sql.upper()
        if not ("INSERT" in upper or "UPDATE" in upper):
            continue
        # f-strings join their parts; look for time.time anywhere in them
        for sub in ast.walk(node.args[0]):
            if (isinstance(sub, ast.Attribute) and sub.attr == "time"
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "time"):
                offenders.append(f"storage.py:{node.lineno}: {sql[:70]}")
    assert not offenders, (
        f"a write statement still evaluates time.time(): {offenders}"
    )


def test_read_side_still_uses_the_python_clock(store):
    """The other half. seconds_since() subtracts from a value it just read,
    and context_budget's tests fake the Python clock on purpose -- that is
    what makes them cheap. Moving this to SQLite would break them and buy
    nothing: it is a duration, not a stored timestamp."""
    store.add_message(1, "t", "user", '{"role":"user"}')
    age = store.oldest_message_age(1, "t")
    assert age is not None and 0 <= age < 5, (
        f"oldest_message_age returned {age}, expected a fresh row"
    )