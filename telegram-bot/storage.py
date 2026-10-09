"""SQLite-backed persistent storage for sub-talks and conversation history.

Threading model
---------------
SQLite is sync and serialises writes internally. We open one connection
per thread (via threading.local) and let SQLite's own locking handle
concurrency. The asyncio event loop calls into this module via
asyncio.to_thread() so DB I/O never blocks the loop.

Why a class, not module-level state
-----------------------------------
Avoids global state; makes the storage object injectable for tests;
lets the bot operator run two bot instances against the same DB
without stepping on each other's connection cache.

Schema
------
sub_talks(user_id, name, created_at, last_used)  PK(user_id, name)
messages(id, user_id, sub_talk, role, content, created_at)  idx(user_id, sub_talk, id)
active_sub_talk(user_id PK, name)

The "active" sub-talk is per-user; one user can be in 'research' while
another is in 'main' at the same time. There is no bot-wide "current
sub-talk" — that would be a UX footgun in a multi-user deployment.

Content is stored as a JSON string. For text-only messages this is
`{"text": "..."}`; for vision messages it is `{"text": "...", "image_url": "..."}`
matching the OpenAI multimodal format. Storing as JSON keeps the
schema simple and round-trips through the OpenAI Chat Completions
format without translation.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS sub_talks (
    user_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    last_used   REAL NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    sub_talk    TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_lookup
    ON messages(user_id, sub_talk, id);

CREATE TABLE IF NOT EXISTS active_sub_talk (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);
"""

# Default cap for the model's context window. Older messages stay in
# the DB but are not sent to llama-server. Env-overridable; see bot.py.
DEFAULT_CONTEXT_MESSAGES = 20


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
        import os
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self._conn().executescript(SCHEMA)
        self._conn().commit()

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

    # --- sub-talk lifecycle ---

    def create_sub_talk(self, user_id: int, name: str) -> bool:
        """Returns True if a new sub-talk was created, False if it already existed."""
        c = self._conn()
        now = time.time()
        try:
            c.execute(
                "INSERT INTO sub_talks (user_id, name, created_at, last_used) "
                "VALUES (?, ?, ?, ?)",
                (user_id, name, now, now),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_sub_talk(self, user_id: int, name: str) -> int:
        """Returns the number of messages deleted (0 if sub-talk did not exist)."""
        c = self._conn()
        cur = c.execute(
            "DELETE FROM messages WHERE user_id = ? AND sub_talk = ?",
            (user_id, name),
        )
        msg_count = cur.rowcount
        c.execute(
            "DELETE FROM sub_talks WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        # If this was the user's active sub-talk, clear the active row
        # so get_active() falls back to 'main' on the next message.
        c.execute(
            "DELETE FROM active_sub_talk WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        c.commit()
        return msg_count

    def list_sub_talks(self, user_id: int) -> list[dict]:
        """Returns [{'name', 'created_at', 'last_used', 'msg_count'}, ...]
        sorted by last_used DESC. Always includes the implicit 'main'
        sub-talk as the first entry if it exists, even with 0 messages."""
        c = self._conn()
        rows = c.execute(
            """
            SELECT s.name,
                   s.created_at,
                   s.last_used,
                   COUNT(m.id) AS msg_count
            FROM sub_talks s
            LEFT JOIN messages m
                ON m.user_id = s.user_id AND m.sub_talk = s.name
            WHERE s.user_id = ?
            GROUP BY s.name
            ORDER BY s.last_used DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sub_talk(self, user_id: int, name: str) -> Optional[dict]:
        c = self._conn()
        row = c.execute(
            "SELECT name, created_at, last_used FROM sub_talks "
            "WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
        return dict(row) if row else None

    # --- active sub-talk ---

    def get_active_sub_talk(self, user_id: int) -> Optional[str]:
        """Returns the user's active sub-talk name, or None if no sub-talks exist yet."""
        c = self._conn()
        row = c.execute(
            "SELECT name FROM active_sub_talk WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            return row["name"]
        # No active set yet — pick the most recently used sub-talk.
        # This handles the case where a user restarts the bot and we
        # want to land them back in whatever they were last working on.
        row = c.execute(
            "SELECT name FROM sub_talks WHERE user_id = ? "
            "ORDER BY last_used DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row["name"] if row else None

    def set_active_sub_talk(self, user_id: int, name: str) -> None:
        """Marks `name` as the user's active sub-talk and bumps last_used."""
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO active_sub_talk (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )
        c.execute(
            "UPDATE sub_talks SET last_used = ? WHERE user_id = ? AND name = ?",
            (time.time(), user_id, name),
        )
        c.commit()

    def touch_sub_talk(self, user_id: int, name: str) -> None:
        """Bump last_used without changing the active sub-talk."""
        c = self._conn()
        c.execute(
            "UPDATE sub_talks SET last_used = ? WHERE user_id = ? AND name = ?",
            (time.time(), user_id, name),
        )
        c.commit()

    # --- messages ---

    def add_message(self, user_id: int, sub_talk: str, role: str, content: str) -> int:
        """Append a message. `content` is a JSON string. Returns the new message id."""
        c = self._conn()
        cur = c.execute(
            "INSERT INTO messages (user_id, sub_talk, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, sub_talk, role, content, time.time()),
        )
        c.commit()
        return cur.lastrowid

    def get_messages(self, user_id: int, sub_talk: str, limit: int = DEFAULT_CONTEXT_MESSAGES) -> list[dict]:
        """Returns the last `limit` messages in chronological order.

        Each row is `{'role': str, 'content': str}` where `content` is
        the raw JSON string the caller stored. For text-only messages
        callers can json.loads() it; for multimodal the caller is
        expected to pass through the dict.
        """
        c = self._conn()
        rows = c.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, id FROM messages
                WHERE user_id = ? AND sub_talk = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (user_id, sub_talk, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_message(self, user_id: int, sub_talk: str) -> Optional[dict]:
        """Returns the most recent message in the sub-talk, or None if empty."""
        c = self._conn()
        row = c.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE user_id = ? AND sub_talk = ? ORDER BY id DESC LIMIT 1",
            (user_id, sub_talk),
        ).fetchone()
        return dict(row) if row else None

    def trim_messages(self, user_id: int, sub_talk: str, keep: int = 100) -> int:
        """Delete old messages, keeping the most recent `keep`.

        Unlike get_messages() (which only limits what we *send* to the
        model), this actually deletes from the DB. Returns the number
        of rows deleted. Used to bound disk usage; the keep threshold
        should be much larger than the model's context window.
        """
        c = self._conn()
        cur = c.execute(
            """
            DELETE FROM messages
            WHERE user_id = ? AND sub_talk = ?
              AND id NOT IN (
                  SELECT id FROM messages
                  WHERE user_id = ? AND sub_talk = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (user_id, sub_talk, user_id, sub_talk, keep),
        )
        c.commit()
        return cur.rowcount
