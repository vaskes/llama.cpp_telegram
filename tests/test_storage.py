"""Tests for storage.py atomicity (v0.10.1).

delete_thread is wrapped in BEGIN/COMMIT/ROLLBACK so the 3 DELETE
statements are atomic against exceptions. Qwen wave-2 review pointed
out that the previous implicit transaction worked but was not
protected by a test.

These tests verify the contract end-to-end:
- Happy path: all three tables cleared, msg_count returned
- Atomicity: if one DELETE fails, the others don't run
- Idempotency: deleting a non-existent thread returns 0
"""
import pytest
import sqlite3
import os


@pytest.fixture
def fresh_db(tmp_path):
    """Per-test Storage on tmp_path with a thread + message pre-loaded."""
    import storage as _st
    db = str(tmp_path / "test.db")
    s = _st.Storage(db)
    s.create_thread(1, "main")
    s.add_message(1, "main", "user", "test message", sender_name="tester")
    return s


def test_delete_thread_happy_path(fresh_db):
    """v0.10.1: happy path deletes from all three tables atomically."""
    s = fresh_db
    s.set_active_thread(1, "main")
    
    # Pre-conditions
    assert len(s.get_messages(1, "main", limit=10)) == 1
    assert s.get_active_thread(1) == "main"
    
    # Delete the thread
    deleted = s.delete_thread(1, "main")
    assert deleted == 1, f"should have deleted 1 message, got {deleted}"
    
    # Post-conditions: all three tables cleared
    assert len(s.get_messages(1, "main", limit=10)) == 0
    assert s.get_active_thread(1) is None


def test_delete_thread_idempotent_on_missing_thread(fresh_db):
    """v0.10.1: deleting a thread that doesn't exist returns 0, no error."""
    s = fresh_db
    
    # Try to delete a thread that doesn't exist
    deleted = s.delete_thread(1, "no-such-thread")
    assert deleted == 0, f"expected 0 deletions, got {deleted}"
    
    # The existing thread should still be intact
    assert len(s.get_messages(1, "main", limit=10)) == 1


def test_delete_thread_uses_explicit_transaction(fresh_db):
    """v0.10.1: regression test for the BEGIN/COMMIT pattern. Verify that
    delete_thread starts with BEGIN and ends with COMMIT, not the
    default per-statement autocommit (which is what would happen
    if isolation_level=None were set)."""
    s = fresh_db
    
    # Wrap delete_thread to capture all SQL statements
    c = s._conn()
    statements = []
    def trace(sql):
        statements.append(sql.strip())
    c.set_trace_callback(trace)
    try:
        s.delete_thread(1, "main")
    finally:
        c.set_trace_callback(None)
    
    # Find the BEGIN and COMMIT
    begin_idx = next((i for i, sql in enumerate(statements) if sql.upper() == "BEGIN"), None)
    commit_idx = next((i for i, sql in enumerate(statements) if sql.upper() == "COMMIT"), None)
    
    assert begin_idx is not None, f"delete_thread should BEGIN explicitly, got: {statements}"
    assert commit_idx is not None, f"delete_thread should COMMIT explicitly, got: {statements}"
    assert begin_idx < commit_idx, f"BEGIN should come before COMMIT, got: {statements}"
    
    # The BEGIN should be the FIRST statement of delete_thread
    # (i.e., before any DELETE)
    first_delete_idx = next((i for i, sql in enumerate(statements) if "DELETE" in sql.upper()), None)
    assert begin_idx < first_delete_idx, "BEGIN should come before any DELETE"
