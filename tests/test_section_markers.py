"""Every section marker names a function with nothing defined between them.

Wave 3 §3 follow-up, and the generalisation of the tripwire that
`fix_markers2.py` shipped with.

That first version checked only markers naming HANDLERS and only asked
that they not sit inside another function. It passed while
`# === _sender_display_name ===` sat 84 lines above the function it
names, with `_safe_sender_label` defined in between. A reader following
that marker lands 69 lines above `_sender_display_name` and inside a
different function entirely.

Measured over the file: twelve of thirteen markers sat three lines above
their function -- marker, two blanks, def, the PEP 8 shape used
throughout. The thirteenth was the outlier, and its problem was that it
was MISNAMED rather than misplaced: the comment block it introduces is
about sanitising a user-controlled name, which is
`_safe_sender_label`'s job, not `_sender_display_name`'s.

The property is therefore not "a marker is near its function" -- distance
is a convention and conventions drift. It is: **no function definition
appears between a marker and the definition it names.** That holds for
all thirteen now, is checkable from the AST alone, and does not care how
much prose sits in between.

Handlers get a second check, inherited from wave 3: a marker naming a
handler must not appear inside any function at all.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "telegram-bot" / "handlers.py"
TEXT = SRC.read_text(encoding="utf-8")
TREE = ast.parse(TEXT)
LINES = TEXT.split("\n")

HANDLERS = {"handle_photo", "handle_voice", "handle_document", "handle_text"}

TOP_DEFS = {n.name: n.lineno for n in TREE.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
DEF_LINES = sorted(TOP_DEFS.values())

ALL_DEFS = {n.name: (n.lineno, n.end_lineno) for n in ast.walk(TREE)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _markers():
    """(line_number_1based, name) for markers that name a top-level def."""
    for i, line in enumerate(LINES, start=1):
        st = line.strip()
        if st.startswith("# ===") and st.endswith("==="):
            name = st[5:].rstrip(" =").strip()
            if name in TOP_DEFS:
                yield i, name


MARKERS = list(_markers())


def test_the_marker_scan_found_something():
    """A regex that silently stops matching would make all of this vacuous."""
    assert len(MARKERS) >= 10, f"only found {len(MARKERS)} markers -- wrong?"
    assert len({n for _, n in MARKERS}) == len(MARKERS), \
        "two markers claim the same function"


def test_no_definition_between_a_marker_and_the_function_it_names():
    bad = []
    for line, name in MARKERS:
        between = [n for n in DEF_LINES if line < n < TOP_DEFS[name]]
        if between:
            named = [TOP_DEFS[n] for n in TOP_DEFS if TOP_DEFS[n] in between]
            bad.append(f"line {line}: {name!r} is defined at "
                       f"{TOP_DEFS[name]}, with {len(named)} definition(s) "
                       f"in between at {named}")
    assert not bad, ("\n".join(bad) + "\n\nA reader following that marker "
                     "lands inside a different function. Either rename the "
                     "marker to the function whose section it actually "
                     "introduces, or give each function its own.")


# Functions that are deliberately unmarked, with the reason. A plain
# "every top-level function must have a marker" rule was written first
# and rejected: it failed on these three, and the only way to satisfy it
# would have been to add markers where none are warranted. That is the
# "tripwire that reddens on correct content" failure -- someone removes
# the marker to silence it and loses the real check with it.
UNMARKED_BY_DESIGN = {
    "_format_executed_tools": "small formatting helper, sits in the "
                              "tool-diagnostics group",
    "_persist_message": "re-exported for tests (see the marker above "
                        "the pair)",
    "_load_history": "same re-export block",
}


def test_unmarked_functions_are_all_accounted_for():
    """The other half: a marker naming nothing is decoration, and a new
    unmarked function is a section that quietly lost its heading."""
    marked = {n for _, n in MARKERS}
    unmarked = set(TOP_DEFS) - marked
    new = unmarked - set(UNMARKED_BY_DESIGN)
    assert not new, (
        f"top-level functions with no marker and no stated reason: "
        f"{sorted(new)}. Add a marker, or add it to UNMARKED_BY_DESIGN "
        f"with why the heading is not wanted.")


def test_the_allowlist_has_not_rotted():
    """An entry here for a function that IS marked would mean the
    allowance silently outlived its reason."""
    marked = {n for _, n in MARKERS}
    stale = sorted(set(UNMARKED_BY_DESIGN) & marked)
    assert not stale, (
        f"{stale} are in UNMARKED_BY_DESIGN but carry a marker; drop the "
        "allowance or the marker, whichever is now wrong")


def test_a_handler_marker_is_never_inside_another_function():
    """The wave-3 §6 property, unchanged."""
    bad = []
    for line, name in MARKERS:
        if name not in HANDLERS:
            continue
        for other, (lo, hi) in ALL_DEFS.items():
            if lo < line <= hi and other != name:
                bad.append(f"line {line}: {name!r} sits inside {other}")
    assert not bad, "\n".join(bad)


def test_markers_name_something_real():
    """Catches `# === renamed_function ===` left after a rename."""
    dangling = []
    for i, line in enumerate(LINES, start=1):
        st = line.strip()
        if st.startswith("# ===") and st.endswith("==="):
            name = st[5:].rstrip(" =").strip()
            if name.startswith("_") or name.islower():
                # section markers are prose; only snake_case-with-underscore
                # or exact function names are claims
                if name not in TOP_DEFS and "_" in name:
                    dangling.append(f"line {i}: {name!r} looks like a "
                                    f"function name but no such function exists")
    assert not dangling, "\n".join(dangling)


# --- the census the codebook quotes --------------------------------------

OVERSIZE_LIMIT = 500
CENSUS_START = "<!-- census: start -->"
CENSUS_END = "<!-- census: end -->"


def _tree_census():
    """(module, markers) for every production module over the limit.

    The marker is the same shape the check above uses: a comment line
    that starts with "# ===" and ends with "===". The count is what a
    reader sees, so it is what the census must say.
    """
    rows = []
    for path in sorted((ROOT / "telegram-bot").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if len(text.splitlines()) <= OVERSIZE_LIMIT:
            continue
        markers = sum(1 for line in text.splitlines()
                      if line.strip().startswith("# ===")
                      and line.strip().endswith("==="))
        rows.append((path.relative_to(ROOT / "telegram-bot").as_posix(),
                     markers))
    return rows


def _codebook_census():
    """(module, markers) as the codebook's census block states them."""
    text = (ROOT / "docs" / "CODEBOOK.md").read_text(encoding="utf-8")
    assert CENSUS_START in text and CENSUS_END in text, (
        "the codebook has no census block -- the check would compare "
        "nothing and pass")
    block = text.split(CENSUS_START, 1)[1].split(CENSUS_END, 1)[0]
    rows = []
    for line in block.splitlines():
        m = re.match(r"\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|", line)
        if m:
            rows.append((m.group(1), int(m.group(2))))
    return rows


def test_the_census_of_big_modules_matches_the_codebook():
    """The codebook's census was remembered, and it had rotted.

    It said six production modules were over 500 lines; the tree has
    eight, because `limits.py` and `llm_delete.py` crossed the line
    since. It said `handlers.py` carried 19 section markers; it carries
    28. It named `bash_command.py` where the file now lives under
    `personal_tools/`.

    A rule whose own evidence has rotted teaches the reader to trust
    it less than they already should -- and this rule is the one that
    says big modules must carry a map, so the evidence is the rule.
    The census is computed now, and the block is compared against the
    tree.
    """
    codebook = _codebook_census()
    tree = _tree_census()
    assert codebook, "the census block has no rows"
    assert tree, (
        "no production module is over the limit any more -- if that is "
        "true, the rule and its census have to go with it")
    assert codebook == tree, (
        "the codebook's census does not match the tree:\n"
        f"  codebook: {codebook}\n"
        f"  tree:     {tree}")