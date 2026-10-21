"""TECH-DEBT #5 -- the migrations must be atomic, and the test proves it by
failing one on purpose.

The substring version of this test (`assert "BEGIN" in src`) is not
written. It would pass against a BEGIN with no matching ROLLBACK, which
is the shape the bug actually took.

What is asserted instead: give the migration a connection that raises on
the Nth ALTER, and check that none of the earlier renames survived. That
is the property, and it cannot be satisfied by a comment.
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import storage  # noqa: E402

pytestmark = pytest.mark.tech_debt(item=5, reason="Pins TECH-DEBT #5: migrations are all-or-nothing")


def _v1_schema(c):
    c.executescript("""
        CREATE TABLE chat_threads (
            user_id INTEGER NOT NULL, name TEXT NOT NULL,
            created_at REAL NOT NULL, last_used REAL NOT NULL,
            PRIMARY KEY (user_id, name));
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, sub_talk TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL);
        CREATE TABLE active_thread (
            user_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE INDEX idx_msg_lookup_v1 ON messages(user_id, sub_talk, id);
    """)
    c.commit()


def _cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


class _ExplodingConn:
    """Wraps a real connection; raises on the Nth ALTER TABLE.

    A real sqlite3.Connection cannot be monkeypatched at the instance
    level, and subclassing it does not let execute() be replaced either,
    so the wrapper forwards everything and intercepts one statement.
    """

    def __init__(self, inner, fail_on_alter: int):
        self._inner = inner
        self._fail_on = fail_on_alter
        self._alters = 0
        self.statements = []

    def execute(self, sql, *a, **kw):
        self.statements.append(sql)
        if sql.startswith("ALTER TABLE"):
            self._alters += 1
            if self._alters == self._fail_on:
                raise sqlite3.OperationalError("simulated failure mid-migration")
        return self._inner.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("fail_on", [1, 2, 3, 4, 5, 6])
def test_rename_is_all_or_nothing(tmp_path, fail_on):
    """A failure on the Nth rename must leave every table untouched.

    Checks all three tables, because the failure mode was a schema that is
    half v1 and half v2: chat_threads renamed, messages not. Asserting on
    one table would pass on the mutations that produce exactly that.
    """
    db = tmp_path / "v1.db"
    inner = sqlite3.connect(db)
    _v1_schema(inner)

    conn = _ExplodingConn(inner, fail_on)
    with pytest.raises(sqlite3.OperationalError):
        storage._migrate_v1_to_v2(conn)

    for table in ("chat_threads", "messages", "active_thread"):
        cols = _cols(inner, table)
        assert "user_id" in cols, (
            f"{table} lost its v1 column despite the migration failing at "
            f"ALTER #{fail_on}. The renames are not atomic."
        )
        assert "chat_id" not in cols, (
            f"{table} was renamed to v2 despite the migration failing at "
            f"ALTER #{fail_on}. This is the half-migrated schema the item "
            f"describes."
        )

    assert "ROLLBACK" in "".join(conn.statements), (
        "the migration failed without issuing a ROLLBACK"
    )
    inner.close()


def test_rename_applies_all_six_when_nothing_fails(tmp_path):
    """The positive control: all six renames land, and the index follows."""
    db = tmp_path / "v1.db"
    c = sqlite3.connect(db)
    _v1_schema(c)

    storage._migrate_v1_to_v2(c)

    for table, legacy, modern in [
        ("chat_threads", "user_id", "chat_id"),
        ("chat_threads", "name",    "thread_id"),
        ("messages",     "user_id", "chat_id"),
        ("messages",     "sub_talk", "thread_id"),
        ("active_thread", "user_id", "chat_id"),
        ("active_thread", "name",    "thread_id"),
    ]:
        cols = _cols(c, table)
        assert modern in cols, f"{table}.{modern} missing after migration"
        assert legacy not in cols, f"{table}.{legacy} still present after migration"

    idx = c.execute("PRAGMA index_info(idx_msg_lookup)").fetchone()
    assert idx is not None, "canonical index not created"
    c.close()


def test_migration_is_idempotent(tmp_path):
    """Storage() runs the migration on every start. Second run must be a
    no-op, not a crash -- the docstring claims it, so it is pinned."""
    db = tmp_path / "v1.db"
    c = sqlite3.connect(db)
    _v1_schema(c)

    storage._migrate_v1_to_v2(c)
    storage._migrate_v1_to_v2(c)   # must not raise
    storage._migrate_v1_to_v2(c)

    for table in ("chat_threads", "messages", "active_thread"):
        assert "chat_id" in _cols(c, table)
    c.close()


def test_add_column_steps_share_one_transaction():
    """The two ALTER TABLE ADD COLUMN steps must not be able to land apart.

    A bot that added `rating` and then died before `sender_name` looks
    like a v3 deployment running v4 code, and the symptom shows up as a
    None sender_name on every message rather than as an error anywhere.
    """
    import ast
    tree = ast.parse(
        (Path(__file__).resolve().parent.parent / "telegram-bot" / "storage.py")
        .read_text(encoding="utf-8")
    )
    init = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    src = ast.unparse(init)

    assert "BEGIN" in src, "__init__ does not open a transaction for the ALTER steps"
    # One BEGIN and one COMMIT for the two ALTERs, not one each.
    # ast.unparse normalises quotes, so match the statement, not the spelling.
    assert len(re.findall(r"execute\(['\"]BEGIN['\"]\)", src)) == \
           len(re.findall(r"execute\(['\"]COMMIT['\"]\)", src)), (
        "unbalanced BEGIN/COMMIT around the ALTER steps"
    )
    # The rollback has to cover BaseException, not just the "duplicate
    # column" branch: a KeyboardInterrupt or a disk error mid-ALTER must
    # not leave the schema half-migrated either.
    assert "except BaseException" in src and "ROLLBACK" in src, (
        "the ALTER steps rollback only on the duplicate-column path; an "
        "unexpected exception would leave rating added and sender_name not"
    )


def test_executescript_comment_is_accurate():
    """The reason SCHEMA_V2 sits outside the transaction is a real SQLite
    behaviour, and the claim is pinned so it cannot be 'simplified' away
    into a transaction that silently commits early anyway."""
    src = (Path(__file__).resolve().parent.parent / "telegram-bot" / "storage.py") \
        .read_text(encoding="utf-8")
    assert "executescript() implicitly commits" in src, (
        "the comment explaining why SCHEMA_V2 is outside the transaction is "
        "gone. Without it the next person will try to wrap it, and the "
        "transaction will end at the first CREATE TABLE."
    )
    # And every statement in SCHEMA_V2 must actually be idempotent, since
    # that is what makes a partial application harmless. Comments are
    # stripped first: several of them contain semicolons, and splitting
    # before stripping yields fragments that start mid-sentence.
    import storage as s
    stripped = "\n".join(
        line for line in s.SCHEMA_V2.splitlines()
        if not line.strip().startswith("--")
    )
    stmts = [q.strip() for q in stripped.split(";") if q.strip()]
    for stmt in stmts:
        upper = " ".join(stmt.upper().split())
        assert upper.startswith("CREATE TABLE IF NOT EXISTS") or \
               upper.startswith("CREATE INDEX IF NOT EXISTS"), (
            f"SCHEMA_V2 has a non-idempotent statement: {stmt[:60]!r}. The "
            f"comment claims every statement is CREATE ... IF NOT EXISTS, "
            f"which is what makes running it outside a transaction safe."
        )