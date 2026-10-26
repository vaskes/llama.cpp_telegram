"""The store learns the Telegram id of every message it holds.

The operator's rule (2026-10-27): the bot must be able to find a message
it was sent -- by its text, by who wrote it -- and then take that message
back out of the chat, WITHOUT the row in the database going away. The
database is the bot's own record of what happened, and it is on its way
to becoming a training set; a row that vanishes because a message was
deleted in the chat is a hole in that record.

So the message table grows a column: `telegram_message_id`, the id of the
Telegram message the row came from. Two things are being tested here:

  * the column arrives, on a fresh database and on an old one, and the
    migration is idempotent (a bot that restarts three times must not
    end up with three columns and a warning);
  * the search finds rows by text and by sender, case-insensitively and
    in Russian -- SQLite's own lower() knows ASCII only, so the search
    registers a `casefold` function on the connection.

And one thing that is NOT being tested, on purpose: nothing here deletes
a row. The delete tool works on Telegram's side; the row stays.
"""
import json
import sqlite3

import pytest


@pytest.fixture
def store(tmp_path):
    """A Storage on a fresh database."""
    import storage as _st
    return _st.Storage(str(tmp_path / "history.db"))


def _row(store, chat_id, thread_id, text, *, sender=None, tg_id=None):
    """Persist one message the way the bot does."""
    payload = json.dumps({"role": "user", "content": text},
                         ensure_ascii=False)
    return store.add_message(chat_id, thread_id, "user", payload,
                             sender_name=sender, telegram_message_id=tg_id)


# --- the column ------------------------------------------------------------

def test_a_fresh_database_has_the_telegram_id_column(store):
    """A new deployment must not have to migrate before it can work."""
    cols = {row[1] for row in store._conn().execute(
        "PRAGMA table_info(messages)").fetchall()}
    assert "telegram_message_id" in cols, (
        f"the messages table has no telegram_message_id: {sorted(cols)}")


def test_an_old_database_gets_the_column_and_keeps_its_rows(tmp_path):
    """The migration runs on a database that was written before the column.

    The rows that were already there are the operator's history. A
    migration that dropped them would be a silent DELETE, and there is
    no undo for that.
    """
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "chat_id INTEGER NOT NULL, thread_id TEXT NOT NULL, role TEXT NOT "
        "NULL, content TEXT NOT NULL, sender_name TEXT, rating INTEGER, "
        "summary_id INTEGER, created_at REAL NOT NULL)")
    conn.execute(
        "INSERT INTO messages (chat_id, thread_id, role, content, sender_name, "
        "created_at) VALUES (1, 'main', 'user', ?, 'tester', 1.0)",
        (json.dumps({"role": "user", "content": "старое сообщение"}),))
    conn.commit()
    conn.close()

    import storage as _st
    store = _st.Storage(db)
    rows = store.get_messages(1, "main", limit=10)
    assert len(rows) == 1, "the migration threw a row away"
    stored = store._conn().execute(
        "SELECT telegram_message_id FROM messages").fetchall()
    assert stored[0][0] is None, (
        "a row that predates the column has no Telegram id, and must "
        "not be given one")

    # Idempotent: a second open runs the migration again, and finds the
    # column already there.
    again = _st.Storage(db)
    assert len(again.get_messages(1, "main", limit=10)) == 1


def test_the_id_is_stored_with_the_row(store):
    """The id travels with the message it belongs to."""
    _row(store, 1, "main", "привет", sender="Vasisualy", tg_id=4242)
    stored = store._conn().execute(
        "SELECT telegram_message_id FROM messages").fetchall()
    assert stored[0][0] == 4242


def test_a_message_without_an_id_is_stored_without_one(store):
    """NULL is a fact too: the bot's own answers are persisted before
    they are sent, so their id does not exist yet."""
    _row(store, 1, "main", "ответ бота", sender=None, tg_id=None)
    stored = store._conn().execute(
        "SELECT telegram_message_id FROM messages").fetchall()
    assert stored[0][0] is None


# --- the search ------------------------------------------------------------

def test_the_search_finds_a_message_by_its_text(store):
    _row(store, 1, "main", "какой деплой сегодня?", sender="Vasisualy")
    hits = store.search_messages(1, text="деплой")
    assert len(hits) == 1
    assert "какой деплой сегодня?" in hits[0]["content"]


def test_the_search_ignores_the_case_of_russian_text(store):
    """SQLite's lower() and LIKE know ASCII only.

    A Russian search that folds ASCII finds nothing, and the bot's
    chats are in Russian -- so the search registers `casefold` on the
    connection and uses that instead.
    """
    _row(store, 1, "main", "Привет, как дела?", sender="Дима")
    assert len(store.search_messages(1, text="привет")) == 1, (
        "the search did not fold the case of Russian text")
    assert len(store.search_messages(1, text="ПРИВЕТ")) == 1, (
        "the search did not fold the case of Russian text")


def test_the_search_finds_a_message_by_who_wrote_it(store):
    _row(store, 1, "main", "что-то", sender="Dmitriklim")
    assert len(store.search_messages(1, sender="dmit")) == 1
    assert len(store.search_messages(1, sender="DIMA")) == 0


def test_the_search_without_a_filter_returns_the_newest_first(store):
    _row(store, 1, "main", "первое")
    _row(store, 1, "main", "второе")
    hits = store.search_messages(1)
    assert [h["id"] for h in hits] == sorted(
        (h["id"] for h in hits), reverse=True)
    assert len(hits) == 2


def test_the_search_does_not_reach_into_another_chat(store):
    """The record of one chat is not the record of another."""
    _row(store, 1, "main", "секрет из первого чата")
    _row(store, 2, "main", "секрет из второго чата")
    hits = store.search_messages(1, text="секрет")
    assert len(hits) == 1
    assert "первого" in hits[0]["content"]


def test_the_search_by_ids_returns_the_rows_of_those_ids(store):
    """This is the door the delete tool's authorisation goes through:
    the bot can vouch for an id only if its own record holds it."""
    _row(store, 1, "main", "одно", tg_id=111)
    _row(store, 1, "main", "два", tg_id=222)
    _row(store, 1, "main", "три", tg_id=333)
    hits = store.search_messages(1, ids=[222])
    assert len(hits) == 1
    assert hits[0]["telegram_message_id"] == 222


def test_the_search_does_not_delete_anything(store):
    """The search is a read. The row is the record, and the record stays."""
    _row(store, 1, "main", "найди меня", tg_id=7)
    store.search_messages(1, text="найди")
    assert len(store.get_messages(1, "main", limit=10)) == 1
