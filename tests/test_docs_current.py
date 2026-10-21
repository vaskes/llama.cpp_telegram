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
TABLE = re.compile(r"^[ \t]*(llm_\w+\.py)[ \t]+(\d+)[ \t]", re.MULTILINE)


def _call_llama():
    tree = ast.parse((BOT / "call_llama.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")
    loop = next(n for n in fn.body if isinstance(n, ast.For))
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


def test_the_check_can_see_a_stale_number():
    """Control. A freshness check that cannot fail is the exact thing it
    exists to prevent, so this runs the same comparison on a table with a
    deliberately wrong entry."""
    quoted = {"llm_sse.py": 99999}
    actual = len((BOT / "llm_sse.py").read_text(encoding="utf-8").splitlines())
    assert quoted["llm_sse.py"] != actual