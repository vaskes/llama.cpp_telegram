"""The database FILE must be the truth -- not the WAL alone.

Measured on production, 2026-10-28 22:30 MSK: the database file's last
write was 01:32:59 while `conversations.db-wal` carried every message
since -- 111 more rows, up to 17:01. `PRAGMA integrity_check` said
"ok" the whole time, the history had not collapsed, and the bot read
538 messages. But the deploy's backup copied the FILE (`cp
"$DB" "$BACKUP/conversations.db.snapshot"`), so it captured a 15-hour-
old snapshot and called it a backup. A restore from it would have
rolled the history back to 01:32.

So these tests read the file the way a plain copy reads it: with no
WAL next to it. If the last write is only in the WAL, they fail.

The cause was SQLite's default checkpoint threshold (1000 WAL pages,
4 MB) sitting above the WAL's real size, so the checkpoint never ran.
The threshold now lives in limits.py (WAL_AUTOCHECKPOINT_PAGES) and
storage.py folds the WAL in after every write.
"""
import os
import shutil
import sqlite3

import pytest


def _file_alone(db_path, tmp_path, name="copy.db"):
    """A copy of the database FILE, with no WAL next to it.

    This is what `cp conversations.db somewhere/` produces, and what the
    deploy's backup produced.
    """
    copy = tmp_path / name
    shutil.copy2(db_path, copy)
    for suffix in ("-wal", "-shm"):
        stray = tmp_path / (name + suffix)
        if stray.exists():
            stray.unlink()
    return copy


def _rows_in_file(copy_path):
    con = sqlite3.connect(f"file:{copy_path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def store(tmp_path):
    import storage as _st
    db = str(tmp_path / "test.db")
    s = _st.Storage(db)
    s.create_thread(1, "main")
    return s


def test_the_file_alone_carries_the_last_message(store, tmp_path):
    """One message, and the file alone has it -- not only the WAL."""
    store.add_message(1, "main", "user", "hello", sender_name="tester")

    copy = _file_alone(store.path, tmp_path)
    assert _rows_in_file(copy) == 1, (
        "the last message is only in the WAL: a copy of the database "
        "file would not carry it"
    )


def test_a_burst_of_messages_all_land_in_the_file(store, tmp_path):
    """A whole turn's worth of messages, all in the file."""
    for i in range(50):
        store.add_message(1, "main", "user", f"message {i}",
                          sender_name="tester")

    copy = _file_alone(store.path, tmp_path)
    assert _rows_in_file(copy) == 50, (
        "the file lags the WAL: a copy of it would be missing messages"
    )


def test_a_copy_of_the_file_has_what_the_database_has(store, tmp_path):
    """The property the deploy's backup needs: the copy is not behind.

    This is the check the deploy was missing: it compared nothing, so a
    stale snapshot passed for a backup.
    """
    for i in range(10):
        store.add_message(1, "main", "user", f"message {i}",
                          sender_name="tester")

    live = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    try:
        live_rows = live.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        live.close()

    copy = _file_alone(store.path, tmp_path)
    assert _rows_in_file(copy) == live_rows


def test_the_checkpoint_threshold_is_not_sqlites_default(tmp_path):
    """The default (1000 pages) is what let the file fall hours behind."""
    import limits
    assert limits.WAL_AUTOCHECKPOINT_PAGES <= 256, (
        "the checkpoint threshold is back at SQLite's default scale; the "
        "database file may fall hours behind the WAL again"
    )


def test_the_wal_checkpoint_runs_and_the_file_advances(store, tmp_path):
    """The checkpoint is not only configured -- it ran.

    Without this, a change that sets the threshold but never triggers a
    checkpoint would look fine on the first test and lie in production.
    """
    store.add_message(1, "main", "user", "hello", sender_name="tester")

    db = store.path
    assert os.path.exists(db + "-wal"), "the database is not in WAL mode"

    con = sqlite3.connect(db)
    try:
        busy, log_frames, checkpointed = con.execute(
            "PRAGMA wal_checkpoint(PASSIVE)"
        ).fetchone()
    finally:
        con.close()

    assert busy == 0, "the checkpoint could not run at all"
    assert log_frames > 0, "the WAL is empty: there was nothing to fold in"
    assert checkpointed > 0, (
        "the checkpoint folded nothing back into the database file"
    )
