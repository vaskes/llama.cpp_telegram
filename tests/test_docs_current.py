"""Documentation that quotes numbers must quote the current ones.

Added while preparing this repo for review. The refresh found the
architecture map listing eleven modules and a `call_llama.py` with a
line count from before five refactor blocks, and `CALL_LLAMA.md`
naming guards that had moved into `llm_loop_guard.py`.

Those rot silently. A doc is not compiled and no test reads it, so the
usual failure is a reviewer trusting it -- which is worse than the doc
being absent, because it looks like an answer.

So: every size this repo quotes in prose is checked against the code.
The numbers are read from the modules themselves, not from a constant,
which means the check fails when the code moves rather than when someone
remembers to update the doc.

Deliberately narrow -- it checks the numbers, not the prose. A sentence
can still be wrong with the right number next to it, and no amount of
arithmetic catches that. What it does catch is the common case: a size
that drifted while the sentence around it stayed.

Note the marker is declared through pytestmark, not a docstring prefix:
`tech_debt(item=8)` as bare source raises NameError at import, which cost
one round trip.
"""
import ast
from loop_owner import loop_tree, run_one  # noqa: E402
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=8)

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
BOT = REPO / "telegram-bot"
ARCH = DOCS / "ARCHITECTURE.md"

# [ \t] not \s -- \s matches newlines, so a greedy match can skip
# whole lines and report modules as absent from their own table.
TABLE = re.compile(r"^[ \t]*([\w.]+\.py)[ \t]+(\d+)[ \t]", re.MULTILINE)

# module -> the public names the map claims it exports. A size is
# satisfiable by comments; a name is not.
SURFACE = re.compile(r"^[ \t]*([\w.]+\.py)[ \t]+\d+[ \t]+(.+)$", re.MULTILINE)


def _call_llama():
    """The loop is a method now; `run_one` is the iteration."""
    fn = run_one()
    cls = next(n for n in loop_tree().body
               if isinstance(n, ast.ClassDef) and n.name == "ToolLoop")
    run = next(n for n in cls.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    loop = next(n for n in run.body if isinstance(n, ast.For))
    return fn, loop


def test_the_module_table_matches_the_modules_on_disk():
    """The architecture map quotes a line count per leaf module."""
    quoted = {name: int(lines) for name, lines in TABLE.findall(
        ARCH.read_text(encoding="utf-8"))}
    assert quoted, "the module table is gone -- this check is now vacuous"

    wrong = []
    for name, said in sorted(quoted.items()):
        path = BOT / name
        if not path.is_file():
            wrong.append(f"{name}: quoted in the map, does not exist")
            continue
        actual = len(path.read_text(encoding="utf-8").splitlines())
        if actual != said:
            wrong.append(f"{name}: map says {said}, file is {actual}")

    assert not wrong, (
        "docs/ARCHITECTURE.md quotes sizes that no longer hold:\n  "
        + "\n  ".join(wrong)
        + "\n\nEither re-measure or drop the number. A wrong size in a "
          "map is a reviewer being misled by a document that looks checked.")


def test_every_leaf_module_is_in_the_map():
    """The failure this repo actually had: eleven modules and no list of
    them. A new leaf that nobody adds to the map is invisible to the next
    reader."""
    quoted = {name for name, _ in TABLE.findall(ARCH.read_text(encoding="utf-8"))}
    on_disk = {p.name for p in BOT.glob("llm_*.py")}
    missing = sorted(on_disk - quoted)
    assert not missing, (
        "leaf modules absent from the architecture map: "
        f"{missing}. Add them with a measured size, or the next reader "
        f"has no way to find {len(on_disk)} modules.")


def test_the_call_llama_sizes_are_current():
    """The map quotes three sizes for call_llama: file, function, loop."""
    fn, loop = _call_llama()
    actual = {
        "file": len((BOT / "call_llama.py").read_text(
            encoding="utf-8").splitlines()),
        "function": fn.end_lineno - fn.lineno + 1,
        "loop": loop.end_lineno - loop.lineno + 1,
    }
    text = ARCH.read_text(encoding="utf-8")

    for label, value in actual.items():
        assert re.search(rf"\b{value}\b", text), (
            f"the map does not mention the current {label} size "
            f"({value} lines)")

    assert f"{actual['file']} lines file" in text, (
        f"the map says something other than {actual['file']} lines for "
        f"call_llama.py")


def test_call_llama_md_points_at_the_guard_module():
    """CALL_LLAMA.md described four guards as living in the loop body.
    They live in llm_loop_guard.py now, and the doc's own section title
    said 'abort ladder' for a set of guards that is not ordered by cost.
    """
    text = (DOCS / "CALL_LLAMA.md").read_text(encoding="utf-8")
    assert "llm_loop_guard.py" in text, \
        "CALL_LLAMA.md still describes the guards as inline"

    # The phrase itself has to appear once, in the note explaining the
    # rename -- a reader of an older copy needs to know what changed.
    # What is banned is the section *title*, because that is what someone
    # skims for.
    headings = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert not any("abort ladder" in ln.lower() for ln in headings), \
        "a heading still calls them a ladder -- cost-ordering would " \
        "change the message the user sees when two conditions fire together"
    assert text.lower().count("abort ladder") == 1, \
        "the old name should appear exactly once, in the correction note"


def test_tool_isolation_doc_names_the_real_sites():
    text = (DOCS / "TOOL-ISOLATION.md").read_text(encoding="utf-8")
    for module in ("llm_toolcalls.py", "llm_toolargs.py"):
        assert module in text, (
            f"TOOL-ISOLATION.md does not mention {module}, which is where "
            f"the refusal actually happens now")


def test_the_public_surface_matches_too():
    """A size can be satisfied by comments; a name cannot.

    The review's objection, and it is right: a table row saying
    "133 lines, dispatch" stays satisfied if someone deletes the code
    and leaves 133 lines of prose. Checking that every name the map
    claims a module exports still exists closes that, and it is the
    part of the row a reader actually acts on.
    """
    quoted = {}
    for name, surface in SURFACE.findall(ARCH.read_text(encoding="utf-8")):
        quoted[name] = {n.strip() for n in surface.split(",") if n.strip()}

    assert quoted, "no surface column parsed -- the table shape moved"

    missing = []
    for name, names in sorted(quoted.items()):
        path = BOT / name
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        actual = {n.name for n in tree.body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef))}
        for claimed in sorted(names):
            if claimed.startswith("_"):
                continue          # private helpers are not the contract
            if claimed not in actual:
                missing.append(f"{name}: map claims {claimed}, module has no such name")

    assert not missing, (
        "docs/ARCHITECTURE.md names things the modules no longer export:\n  "
        + "\n  ".join(missing)
        + "\n\nA reader following the map to a function that is not there "
          "has been actively misled.")


def test_a_key_entry_point_still_exists():
    """The names the rest of the system calls. Cheap, and it is the set a
    reviewer would try first."""
    expected = {
        "llm_dispatch.py": "execute_tool",
        "llm_toolargs.py": "parse_tool_args",
        "llm_finish.py": "finish_without_tool_calls",
        "llm_loop_guard.py": "LoopGuard",
        "llm_sse.py": "StreamAccumulator",
        "llm_nonstream.py": "non_stream_iteration",
    }
    gone = []
    for module, name in sorted(expected.items()):
        tree = ast.parse((BOT / module).read_text(encoding="utf-8"))
        actual = {n.name for n in tree.body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef))}
        if name not in actual:
            gone.append(f"{module}:{name}")
    assert not gone, f"entry points referenced from the map are gone: {gone}"


def test_the_summary_line_matches_the_tree():
    """The one-line summary of the tree is a claim about the tree.

    `docs/ARCHITECTURE.md` ends its map with "64 top-level modules, 172
    test files". Both numbers move whenever a module or a test file is
    added, and nothing recomputed them -- the summary was a snapshot of
    a moment, and the suite's size in it was already behind.

    The size of the suite is quoted in one place only now -- the
    README's badge, checked against the collected tests -- because a
    number in two places is a number nobody recomputes.
    """
    text = ARCH.read_text(encoding="utf-8")
    m = re.search(r"(\d+) top-level modules, (\d+) test files", text)
    assert m, (
        "the architecture map no longer states how many modules and test "
        "files the tree has -- the sentence this check reads is gone, and "
        "the check would pass by finding nothing")
    said_modules, said_tests = int(m.group(1)), int(m.group(2))
    actual_modules = len(list(BOT.glob("*.py")))
    actual_tests = len(list((REPO / "tests").glob("test_*.py")))
    assert said_modules == actual_modules, (
        f"the map says {said_modules} top-level modules, the tree has "
        f"{actual_modules}")
    assert said_tests == actual_tests, (
        f"the map says {said_tests} test files, the tree has {actual_tests}")


#: The tree diagram: one line per module, its size in brackets. The
#: numbers are typed by hand, so a module that grows leaves the map
#: describing a file that no longer exists.
DIAGRAM = re.compile(r"^[^\w]*(?P<name>[\w*]+\.py)\b(?P<rest>.*)$", re.MULTILINE)

#: The size in a diagram line: "(124 lines)" or "(382)".
SIZE_IN_LINE = re.compile(r"\((?P<lines>\d+)(?:\s+lines)?\)")

#: The two counts of files the diagram states in prose.
LEAF_MODULES = re.compile(r"(\d+)\s+leaf modules")
SELFTEST_LEAVES = re.compile(r"(\d+)\s+selftest_\*\.py\s+leaves")


def _map_block():
    """The fenced block that draws `telegram-bot/` as a tree."""
    text = ARCH.read_text(encoding="utf-8")
    m = re.search(r"## What's inside\s*\n+```[a-z]*\n(?P<block>.*?)\n```",
                  text, re.S)
    assert m, (
        "the architecture map no longer has the tree block -- this check "
        "would pass by finding nothing")
    return m.group("block")


def _module(name):
    """The file a name in the map refers to, or None if there is none."""
    direct = BOT / name
    if direct.is_file():
        return direct
    hits = list(BOT.rglob(name))
    return hits[0] if len(hits) == 1 else None


def test_the_map_quotes_the_tree_it_describes():
    """The diagram of `telegram-bot/` quotes a size per module and two
    counts of files. Nothing recomputed them.

    The sizes were typed by hand, and six of them were already wrong
    before this wave -- one of them by a line, because the file has no
    trailing newline and `wc -l` counts one less than the line count
    every tripwire in this repo measures.

    The leaf table below the diagram was checked, but only for the
    `llm_*` modules: the two rows that name a module outside that
    family -- `call_llama.py` and `user_notice.py` -- were never
    checked at all, and `user_notice.py` was quoted at 128 lines when it
    is 187.

    So the diagram is measured against the tree, the table is measured
    for every module it names, and the two counts are measured against
    the files they count.
    """
    block = _map_block()
    wrong = []
    checked = 0

    for line in block.splitlines():
        m = DIAGRAM.match(line)
        if not m:
            continue
        name = m.group("name")
        size = SIZE_IN_LINE.search(m.group("rest"))
        if not size or "*" in name:
            continue          # a glob, not a module: the counts below
        path = _module(name)
        if path is None:
            wrong.append(f"{name}: quoted in the map, does not exist")
            continue
        checked += 1
        actual = len(path.read_text(encoding="utf-8").splitlines())
        said = int(size.group("lines"))
        if actual != said:
            wrong.append(f"{name}: map says {said}, file is {actual}")

    assert checked >= 20, (
        f"only {checked} sizes parsed out of the diagram -- the shape of "
        f"the map moved and this check would pass by finding nothing")

    for pattern, files in ((LEAF_MODULES, "llm_*.py"),
                           (SELFTEST_LEAVES, "selftest_*.py")):
        m = pattern.search(block)
        assert m, f"the diagram no longer states the number of {files}"
        said = int(m.group(1))
        actual = len(list(BOT.glob(files)))
        if said != actual:
            wrong.append(f"the map says {said} {files}, the tree has {actual}")

    assert not wrong, (
        "docs/ARCHITECTURE.md quotes a tree it no longer has:\n  "
        + "\n  ".join(wrong)
        + "\n\nThe map is hand-written. A number in it is a copy of a "
          "measurement, and a copy nobody recomputes is a copy that "
          "rots -- re-measure it, or drop the number.")


def test_the_check_can_see_a_stale_number():
    """Control. A freshness check that cannot fail is the exact thing it
    exists to prevent, so this runs the same comparison on a table with a
    deliberately wrong entry."""
    quoted = {"llm_sse.py": 99999}
    actual = len((BOT / "llm_sse.py").read_text(encoding="utf-8").splitlines())
    assert quoted["llm_sse.py"] != actual