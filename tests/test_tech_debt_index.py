"""TECH-DEBT.md is an index, and it cannot drift from docs/tech-debt/.

The root file used to be a second, hand-maintained full copy of every
debt item. It drifted on four of eight entries while the `docs/` copies
stayed correct, and the drift is not cosmetic:

  * it described `call_llama` as "a 1177-line function marked NOT
    TOUCHED" on the same day six leaf modules had been extracted from
    it, and a review report was written against that description;
  * it carried three items (`created_at`, migration atomicity, the tool
    loop) with no state field at all;
  * it never mentioned `_selftest`, which at 1940 lines is the largest
    untested unit in the repository.

Updating four lines would have fixed this instance and left the trap
armed. So the root file is now an index, and these tests are what stops
it becoming a copy again.

Three separate properties, because they fail in three separate ways:

  * every item in `docs/tech-debt/` is listed in the index;
  * an item the index calls closed is not also described as open;
  * the tripwire named in an item's frontmatter exists on disk.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ITEMS = ROOT / "docs" / "tech-debt"
INDEX = ROOT / "TECH-DEBT.md"
CODEBOOK = ROOT / "docs" / "CODEBOOK.md"
SRC = ROOT / "telegram-bot"


def _front_matter(path):
    """Parse the YAML-ish frontmatter the item files carry.

    Hand-parsed rather than pulled in via yaml: the repo runs with
    stdlib only on the host, and this format is four scalar keys.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    block = text.split("---", 2)[1]
    out = {}
    for line in block.splitlines():
        line = line.split("#", 1)[0].strip() if "#" in line and "http" not in line else line.strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def _items():
    got = {}
    for f in sorted(ITEMS.glob("*.md")):
        fm = _front_matter(f)
        if "tech_debt" not in fm:
            continue
        got[int(fm["tech_debt"])] = (f, fm)
    return got


ITEMS_BY_ID = _items()


def test_every_item_has_front_matter():
    """Unfiled files are how an item goes missing a second time."""
    missing = [f.name for f in sorted(ITEMS.glob("*.md"))
               if "tech_debt" not in _front_matter(f)]
    assert not missing, f"item file(s) without tech_debt: in frontmatter: {missing}"


def test_every_item_is_listed_in_the_index():
    index = INDEX.read_text(encoding="utf-8")
    missing = [n for n in sorted(ITEMS_BY_ID)
               if not re.search(rf"^\|\s*{n}\s*\|", index, re.M)]
    assert not missing, (
        f"items exist in docs/tech-debt/ but not in the index: {missing}. "
        "The index is generated from the item files by hand and this test "
        "is the only thing keeping them in agreement.")


def test_index_does_not_contradict_a_closed_item():
    index = INDEX.read_text(encoding="utf-8")
    bad = []
    for n, (f, fm) in sorted(ITEMS_BY_ID.items()):
        if fm.get("state") != "closed":
            continue
        row = next((l for l in index.splitlines()
                    if re.match(rf"^\|\s*{n}\s*\|", l)), None)
        if row and "closed" not in row.lower():
            bad.append(f"{n}: frontmatter says closed, index row says {row.strip()!r}")
    assert not bad, "\n".join(bad)


# A string check for the four stale claims was written here and
# deleted. It could not tell "asserting 1177 lines" from "quoting the
# old claim while explaining that it was measured false", which is
# exactly what the index does -- so it was red on correct content. A
# tripwire that reddens on correct content is worse than none; someone
# fixes the prose to satisfy it and the note goes with it.
#
# test_measured_sizes_in_the_index_is_current below catches the same
# drift properly, because it recomputes instead of matching.


def test_named_tripwires_exist():
    """A marker pointing at a test that was renamed or deleted is a lie."""
    missing = []
    for n, (f, fm) in sorted(ITEMS_BY_ID.items()):
        marker = fm.get("marker", "").strip()
        if marker in ("", "none yet"):
            continue
        for t in re.split(r",\s*", marker):
            t = t.strip()
            if t and not (ROOT / t).exists():
                missing.append(f"{n}: {t}")
    assert not missing, f"frontmatter names tripwires that do not exist: {missing}"


def test_measured_sizes_in_the_index_are_current():
    """Every row of the measured block must match the tree.

    The check used to name three files: `dispatch.py`, `selftest.py` and
    `bot.py`. All three happened to be right, and the nine rows it did
    not look at rotted: `handlers.py` said 1504 while the file had 1502,
    `storage.py` said 886 while the file had 938. A tripwire that covers
    three of twelve rows is green precisely where it happens to be
    correct -- and the next reviewer builds a conclusion on a number
    that "is checked by a test".

    So the rows are read from the block itself. A new row is covered the
    day it is written, and a row cannot be forgotten by the check that
    is supposed to guard it.
    """
    index = INDEX.read_text(encoding="utf-8")
    block = index.split("## Measured, 2026-10-22", 1)[1].split("```", 2)[1]
    rows = re.findall(r"^(\S+\.py)\s+(\d+) lines", block, re.MULTILINE)
    assert rows, (
        "the measured block has no rows -- the check would compare "
        "nothing and pass")
    for name, declared in rows:
        path = SRC / name
        assert path.exists(), (
            f"the measured block names {name}, which is not in the tree")
        actual = len(path.read_text(encoding="utf-8").splitlines())
        assert int(declared) == actual, (
            f"{name}: the block says {declared} lines, the file has {actual}")


def test_the_codebook_s_size_claims_are_current():
    """The rulebook quotes module sizes; the quotes must be true.

    The measured block above is checked, so it is the place that says
    how big a module is. The rulebook quotes the same numbers in its
    prose -- and those quotes were not checked, so they rotted on their
    own: `llm_iteration.py` was quoted at 571 lines while the file had
    575. A number that the rulebook quotes and nobody recomputes is the
    same trap as the block was, one document over.

    Only CURRENT-state claims are read. A sentence that says a file
    "went from 1672 lines to ~900" is history, and history is not
    recomputed -- it is what the sentence is for.
    """
    codebook = CODEBOOK.read_text(encoding="utf-8")
    name_re = re.compile(r"`(?P<name>[\w./]+\.py)`")
    number_re = re.compile(r"(?<![\d.])(?P<n>\d{3,5})(?![\d.])")
    historical = re.compile(r"\b(from|was|were|went|had)\b")
    claims = []
    for line_no, line in enumerate(codebook.splitlines(), 1):
        for match in name_re.finditer(line):
            clause = line[match.end():].split(". ")[0]
            if historical.search(clause):
                continue
            if clause.lstrip().startswith("|"):
                # A row of the tripwire table: the number after the
                # name is a test count, not a line count.
                continue
            number = number_re.search(clause)
            if number:
                claims.append((line_no, match.group("name"),
                               int(number.group("n"))))
    assert claims, (
        "no module size claim was found in the codebook -- the check "
        "would compare nothing and pass")
    wrong = []
    for line_no, name, claimed in claims:
        path = SRC / name
        assert path.exists(), (
            f"the codebook quotes {name}, which is not in the tree")
        actual = len(path.read_text(encoding="utf-8").splitlines())
        if actual != claimed:
            wrong.append(f"line {line_no}: {name} says {claimed}, "
                         f"the file has {actual}")
    assert not wrong, "\n".join(wrong)