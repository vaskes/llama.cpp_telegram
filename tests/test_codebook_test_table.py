"""The CODEBOOK's tripwire table must name files that exist.

Added 2026-10-24, right after the table named `test_edit_replace_live_path.py`
— a file that was never in the repository. The table is the list of guards
that protect the rules in `docs/CODEBOOK.md`, and it was claiming a guard
that did not exist, with a test count (7) that nothing had ever verified.

That is the same class of rot the document itself warns about, wearing the
document's own clothes: a promise with a number next to it, in the place a
reader looks first when they ask "what is this rule checked by?".

So the table is checked against the filesystem:

* every `test_*.py` name in the table is a real file in `tests/`;
* every test file named in the table has at least one test in it;
* the "N test files in total" sentence agrees with the tree.

The last one is a count, and counts drift -- so it is checked with a margin
rather than exactly, and it is checked against the number of *test files*,
not the number of tests. A document that quotes a count has to be able to
say what it counts.

Deliberately narrow: it checks the names and the counts, not the prose
next to them. A sentence can still be wrong with a right-sized table
beside it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
CODEBOOK = ROOT / "docs" / "CODEBOOK.md"

# | `test_foo.py` | 12 | what it protects |
ROW = re.compile(
    r"^\|\s*`(?P<name>test[\w./]*\.py)`\s*\|\s*(?P<count>\d+)\s*\|",
    re.MULTILINE,
)

# "`test_drift_r8/r9/r10/r11`" -- a shorthand row, not a file list.
SHORTHAND = re.compile(r"^\|\s*`test_drift_r8/r9/r10/r11`", re.MULTILINE)

# "133 test files in total"
TOTAL = re.compile(r"(\d+) test files in total")


@pytest.fixture(scope="module")
def text() -> str:
    return CODEBOOK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rows(text) -> list:
    return [(m.group("name"), int(m.group("count")))
            for m in ROW.finditer(text)]


def test_the_table_was_found(text):
    """A guard on the guard: if the regex stopped matching, every check
    below would pass by finding nothing."""
    assert len(ROW.findall(text)) >= 20, (
        f"only {len(ROW.findall(text))} tripwire rows matched in "
        f"{CODEBOOK.name}; the table format probably changed")


def test_every_named_tripwire_file_exists(rows):
    missing = [name for name, _ in rows
               if not (TESTS / name).is_file()]
    assert not missing, (
        "the CODEBOOK names tripwires that do not exist: "
        f"{missing}. A guard that is not there guards nothing, and a table "
        "that claims it is there is worse than an empty table.")


def test_every_named_tripwire_file_actually_has_tests(rows):
    """A file with no tests in it is not a tripwire."""
    empty = []
    for name, _ in rows:
        path = TESTS / name
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        if not re.search(r"^\s*(async )?def test_", src, re.MULTILINE):
            empty.append(name)
    assert not empty, (
        f"the CODEBOOK names these as tripwires, but they define no test "
        f"function: {empty}")


def test_the_declared_test_counts_match_the_files(text, rows):
    """The number next to the name is a claim about the file.

    It used to be checked in one direction only (the tripwire's own
    files), and the edit-replace row claimed 7 tests for a file that did
    not exist. A count that is too big is a stale claim; a count that is
    too small is a stale claim in the other direction -- the file grew
    and the table did not. Both are the same rot, so both are checked:
    a table whose numbers are "close enough" is a table nobody recomputes.

    The count is the number of *test functions* in the file, not the
    number of collected cases: a parametrized test is one function and
    several cases, and the table has always counted the function.
    """
    wrong = []
    for name, count in rows:
        path = TESTS / name
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        actual = len(re.findall(r"^\s*(async )?def test_", src, re.MULTILINE))
        if count != actual:
            wrong.append(f"{name}: table says {count}, the file has {actual}")
    assert not wrong, (
        "the CODEBOOK's tripwire table does not match the files:\n"
        + "\n".join(wrong))


def test_the_total_number_of_test_files_is_right(text):
    """The sentence under the table.

    Counted exactly, not with a margin. The margin was a hole with a
    number in it: the sentence was free to drift five files in either
    direction, and a reader could not tell a fresh sentence from a
    stale one. The sentence is about the size of the suite, and the
    number rides in the same commit as the test files it counts -- the
    table's own completeness is the previous test's job.
    """
    m = TOTAL.search(text)
    assert m, (
        f"{CODEBOOK.name} no longer states how many test files there are; "
        "a document that quotes a count has to say what it counts")
    declared = int(m.group(1))
    actual = len(list(TESTS.glob("test_*.py")))
    assert declared == actual, (
        f"the CODEBOOK says {declared} test files, the tree has {actual}. "
        "Update the sentence in the same commit as the tests, or the "
        "sentence is a lie with a number in it.")


def test_the_shorthand_row_is_not_mistaken_for_a_file(text):
    """`test_drift_r8/r9/r10/r11` is four files in one row.

    It is written as a shorthand, so it is not matched by the row regex
    and not checked as a file. This test exists to say so out loud: if
    the shorthand is ever replaced by four real rows, this test fails and
    the four get checked individually.
    """
    assert SHORTHAND.search(text), (
        "the shorthand row `test_drift_r8/r9/r10/r11` is gone. If it was "
        "replaced by separate rows, the row regex now has to match them, "
        "and this test should be rewritten rather than left passing.")
