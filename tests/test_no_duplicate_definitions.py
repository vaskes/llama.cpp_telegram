"""No test is defined twice in the same file.

TECH-DEBT #8 block 3, found while cleaning up after it.

`tests/test_blockers.py` had NINETEEN copies of one 60-line test. Every
one after the first was dead: Python binds the last definition and
ignores the rest, so the suite was green and ran one test. A reader
counting tests, or a mutation harness trusting a line-number anchor,
would have been reasoning about code that does not execute.

Nobody hand-wrote nineteen copies. A tool appended a block to the file
without checking whether it was already there, and nothing complained --
because a duplicate definition is not an error in Python, it is
silently the later one.

The tripwire below is the whole fix: a test cannot be defined twice.
This is the cheapest possible check and it is the one that would have
caught all nineteen.
"""
import ast
import collections
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
BOT_DIR = TESTS.parent / "telegram-bot"


def _definitions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return collections.Counter(
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def test_no_test_is_defined_twice_in_the_same_file():
    """A redefinition is dead code, not an update."""
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        dupes = {name: n for name, n in _definitions(path).items() if n > 1}
        if dupes:
            offenders.append(f"{path.name}: {sorted(dupes)}")
    assert not offenders, (
        "test functions defined more than once in the same file -- "
        "Python keeps the last one and the rest never run:\n  "
        + "\n  ".join(offenders))


def test_no_module_defines_the_same_name_twice_under_any_kind():
    """Catches a helper shadowed by a helper, not just tests.

    Same failure mode, quieter: `def _common(...)` defined twice means
    the first one is dead, and the fixtures built by it are missing.
    """
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = collections.Counter(
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)))
        dupes = {n: c for n, c in names.items() if c > 1}
        if dupes:
            offenders.append(f"{path.name}: {sorted(dupes)}")
    assert not offenders, (
        "top-level definitions repeated in the same file:\n  "
        + "\n  ".join(offenders))


def test_no_module_is_imported_under_two_names():
    """`import llm_sse` and `from llm_sse import StreamAccumulator` under
    different bindings gives two module objects in sys.modules' wake,
    and an `isinstance` across them is False for no visible reason."""
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound = collections.Counter()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    bound[a.asname or a.name.split(".")[0]] += 1
            elif isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    if a.name != "*":
                        bound[a.asname or a.name] += 1
        dupes = {n: c for n, c in bound.items() if c > 1}
        if dupes:
            offenders.append(f"{path.name}: {sorted(dupes)}")
    assert not offenders, (
        "names bound more than once at module level:\n  "
        + "\n  ".join(offenders))


def test_the_blockers_file_is_the_size_the_history_says_it_is():
    """A floor, so a re-duplication cannot quietly return.

    The file was 1743 lines with nineteen copies of one test; the live
    content is 628. This does not pin 628 -- a real test added later
    would break it -- it pins "not 1700", which is the shape of the
    regression rather than the exact line.
    """
    lines = len((TESTS / "test_blockers.py").read_text(
        encoding="utf-8").splitlines())
    assert lines < 900, (
        f"tests/test_blockers.py is {lines} lines; it was 628 after "
        f"removing eighteen duplicate copies of the per-user-cap test")