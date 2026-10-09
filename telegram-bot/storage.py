"""SQLite-backed persistent storage for conversation threads.

Threading model
---------------
SQLite is sync and serialises writes internally. We open one connection
per thread (via threading.local) and let SQLite's own locking handle
concurrency. The asyncio event loop calls into this module via
asyncio.to_thread() so DB I/O never blocks the loop.

Why a class, not module-level state
----------------------------------
Avoids global state; makes the storage object injectable for tests;
lets the bot operator run two bot instances against the same DB
without stepping on each other's connection cache.

Schema (post v2)
----------------
The schema is intentionally mode-agnostic: a "thread" is a named
conversation stream. It works for both deployment modes of the bot:

  Private chat mode (legacy)
    chat_id   == user_id (Telegram numeric user id)
    thread_id == sub-talk name (string the user picks, e.g. "main")

  Group with Telegram Topics mode (new)
    chat_id   == Telegram group chat id (negative number for groups)
    thread_id == Telegram message_thread_id (numeric)

The same SQL queries and indices serve both modes; only the meaning
of the two key columns changes. Bot code picks the right values from
update.effective_chat.id and update.message.message_thread_id.

Schema columns:
  chat_threads(chat_id, thread_id, created_at, last_used)
      PK(chat_id, thread_id)
      Index of "known threads" for both modes:
        - private mode: explicit sub-talks the user /newsub'd
        - group mode: not strictly needed (Telegram is the source of
          truth for thread existence), but kept for the active_thread
          table below and for analytics. Lazily populated.

  messages(id, chat_id, thread_id, role, content, created_at)
      idx(chat_id, thread_id, id) for the hot lookup
      The single source of truth for conversation history.

  active_thread(chat_id PK, thread_id)
      Only meaningful in private mode (where "active" is not
      implicit). In group mode, the active thread is the one the
      user is currently typing in (read from message_thread_id),
      and this table is unused.

Migration from v1
------------------
The v1 schema used (user_id, sub_talk). On startup we detect the old
columns and rename them. SQLite 3.25+ supports ALTER TABLE ... RENAME
COLUMN, and Ubuntu 24.04 ships 3.46+. No data loss: the row values
become (chat_id, thread_id) verbatim, since "user_id" was just a
narrower special case of "chat_id" (private chats have chat_id ==
user_id for the same int).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Optional


SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS chat_threads (
    chat_id     INTEGER NOT NULL,
    thread_id   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    last_used   REAL NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    thread_id   TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_lookup
    ON messages(chat_id, thread_id, id);

CREATE TABLE IF NOT EXISTS active_thread (
    chat_id     INTEGER PRIMARY KEY,
    thread_id   TEXT NOT NULL
);
"""

# Default cap for the model's context window. Older messages stay in
# the DB but are not sent to llama-server. Env-overridable; see bot.py.
DEFAULT_CONTEXT_MESSAGES = 20


def _migrate_v1_to_v2(c: sqlite3.Connection) -> None:
    """Rename legacy (user_id, sub_talk) columns to (chat_id, thread_id).

    Safe to call multiple times — if columns are already renamed, the
    rename is a no-op error which we swallow. SQLite 3.25+ required
    for ALTER TABLE RENAME COLUMN; raises sqlite3.OperationalError
    on older versions (caller should handle).
    """
    for table, legacy, modern in [
        ("chat_threads", "user_id", "chat_id"),
        ("chat_threads", "name",    "thread_id"),
        ("messages",     "user_id", "chat_id"),
        ("messages",     "sub_talk", "thread_id"),
        ("active_thread", "user_id", "chat_id"),
        ("active_thread", "name",    "thread_id"),
    ]:
        cur = c.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in cur.fetchall()}
        if legacy in cols and modern not in cols:
            c.execute(f"ALTER TABLE {table} RENAME COLUMN {legacy} TO {modern}")
    # Also rename the index if it kept the v1 name.
    cur = c.execute("PRAGMA index_info(idx_msg_lookup)")
    if cur.fetchone() is None:
        # v1 may have used a different index name. Drop the old one
        # and create the canonical one — DROP IF EXISTS to be safe.
        c.execute("DROP INDEX IF EXISTS idx_msg_lookup_v1")
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_lookup "
                  "ON messages(chat_id, thread_id, id)")


class Storage:
    """Persistent conversation store backed by a single SQLite file.

    All methods are sync. Callers running inside an asyncio event loop
    should wrap them in asyncio.to_thread() to avoid blocking the loop.
    """

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        # Initialise the schema once. If the directory doesn't exist
        # yet (first run after a fresh deploy), create it.
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        c = self._conn()
        # Migration: rename legacy v1 columns to v2 names. Done
        # BEFORE running the v2 CREATE statements so the indices
        # are created against the new column names.
        try:
            _migrate_v1_to_v2(c)
        except sqlite3.OperationalError as e:
            print(f"[storage] WARN: migration step failed: {e}")
        c.executescript(SCHEMA_V2)
        c.commit()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            c = sqlite3.connect(self.path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            # WAL gives concurrent readers while a writer is active,
            # which matters once the polling loop and the handler
            # are both touching the DB on the same process.
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return self._local.conn

    # --- thread lifecycle ---

    def create_thread(self, chat_id: int, thread_id: str) -> bool:
        """Returns True if a new thread was created, False if it already existed."""
        c = self._conn()
        now = time.time()
        try:
            c.execute(
                "INSERT INTO chat_threads (chat_id, thread_id, created_at, last_used) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, thread_id, now, now),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_thread(self, chat_id: int, thread_id: str) -> int:
        """Returns the number of messages deleted (0 if thread did not exist)."""
        c = self._conn()
        cur = c.execute(
            "DELETE FROM messages WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        )
        msg_count = cur.rowcount
        c.execute(
            "DELETE FROM chat_threads WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        )
        # If this was the active thread, clear the active row so
        # get_active_thread() falls back to most-recent.
        c.execute(
            "DELETE FROM active_thread WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        )
        c.commit()
        return msg_count

    def list_threads(self, chat_id: int) -> list[dict]:
        """Returns [{'thread_id', 'created_at', 'last_used', 'msg_count'}, ...]
        sorted by last_used DESC."""
        c = self._conn()
        rows = c.execute(
            """
            SELECT s.thread_id,
                   s.created_at,
                   s.last_used,
                   COUNT(m.id) AS msg_count
            FROM chat_threads s
            LEFT JOIN messages m
                ON m.chat_id = s.chat_id AND m.thread_id = s.thread_id
            WHERE s.chat_id = ?
            GROUP BY s.thread_id
            ORDER BY s.last_used DESC
            """,
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_thread(self, chat_id: int, thread_id: str) -> Optional[dict]:
        c = self._conn()
        row = c.execute(
            "SELECT thread_id, created_at, last_used FROM chat_threads "
            "WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        return dict(row) if row else None

    # --- active thread (private mode only) ---

    def get_active_thread(self, chat_id: int) -> Optional[str]:
        """Returns the active thread_id, or None if no threads exist yet.

        Falls back to most-recently-used thread if no explicit
        active row exists, so a user returning to the bot lands
        back in whatever they were last working on.
        """
        c = self._conn()
        row = c.execute(
            "SELECT thread_id FROM active_thread WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            return row["thread_id"]
        row = c.execute(
            "SELECT thread_id FROM chat_threads WHERE chat_id = ? "
            "ORDER BY last_used DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return row["thread_id"] if row else None

    def set_active_thread(self, chat_id: int, thread_id: str) -> None:
        """Marks `thread_id` as active and bumps last_used."""
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO active_thread (chat_id, thread_id) "
            "VALUES (?, ?)",
            (chat_id, thread_id),
        )
        c.execute(
            "UPDATE chat_threads SET last_used = ? "
            "WHERE chat_id = ? AND thread_id = ?",
            (time.time(), chat_id, thread_id),
        )
        c.commit()

    def touch_thread(self, chat_id: int, thread_id: str) -> None:
        """Bump last_used without changing the active thread."""
        c = self._conn()
        c.execute(
            "UPDATE chat_threads SET last_used = ? "
            "WHERE chat_id = ? AND thread_id = ?",
            (time.time(), chat_id, thread_id),
        )
        c.commit()

    # --- messages ---

    def add_message(self, chat_id: int, thread_id: str, role: str, content: str) -> int:
        """Append a message. `content` is a JSON string. Returns the new message id."""
        c = self._conn()
        cur = c.execute(
            "INSERT INTO messages (chat_id, thread_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, thread_id, role, content, time.time()),
        )
        c.commit()
        return cur.lastrowid

    def get_messages(self, chat_id: int, thread_id: str,
                     limit: int = DEFAULT_CONTEXT_MESSAGES) -> list[dict]:
        """Returns the last `limit` messages in chronological order.

        Each row is `{'role': str, 'content': str}` where `content`
        is the raw JSON string the caller stored. The OpenAI Chat
        Completions format (`{"role", "content"}`) round-trips
        because both text-only and multimodal messages are stored
        as JSON.
        """
        c = self._conn()
        rows = c.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, id FROM messages
                WHERE chat_id = ? AND thread_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, thread_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_message(self, chat_id: int, thread_id: str) -> Optional[dict]:
        """Returns the most recent message in the thread, or None if empty."""
        c = self._conn()
        row = c.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE chat_id = ? AND thread_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id, thread_id),
        ).fetchone()
        return dict(row) if row else None

    def trim_messages(self, chat_id: int, thread_id: str, keep: int = 100) -> int:
        """Delete old messages, keeping the most recent `keep`."""
        c = self._conn()
        cur = c.execute(
            """
            DELETE FROM messages
            WHERE chat_id = ? AND thread_id = ?
              AND id NOT IN (
                  SELECT id FROM messages
                  WHERE chat_id = ? AND thread_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (chat_id, thread_id, chat_id, thread_id, keep),
        )
        c.commit()
        return cur.rowcount
