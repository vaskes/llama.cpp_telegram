"""A pointer names a section that exists -- and never a section that will move.

The repo's "where to read the record" lines point into another document
with a backticked path and a section sign:

    `CHANGELOG.md` § The record holds a message once

Two different things rot that pointer, and they rot in opposite
directions.

**The section goes away.** A heading is renamed or deleted, the pointer
keeps the old name, and the reader follows it into a document that does
not have it. The pointer is a claim about another file, and nobody
recomputes it.

**The section moves.** The CHANGELOG's `## [Unreleased]` is where the
work in flight is written; at the next tag it is renamed to the release
section, and every pointer into it silently starts pointing at whatever
is unreleased *now*. Eighteen pointers in `docs/TODO.md` said
"§ Unreleased" at the same time -- for work that had shipped a release
earlier, in sections that had a name and a tag. A pointer into
`Unreleased` is a pointer to a place that will move, so it is refused:
the pointer has to name the section by name, and a name survives the
move from `Unreleased` into the release section.

So the rule is: name the section, and the name has to exist. The
section may be a markdown heading, a CHANGELOG release section, or a
CODEBOOK rule id (`X-23`).

Deliberately narrow: this checks the backticked-path form, which is the
form this repo uses for a pointer. Two other conventions are *not*
covered here, and both are known rot:

  * the link form -- `[docs/SECURITY.md](SECURITY.md) §3.5` -- where the
    anchor is in the URL and can rot the same way;
  * the bare-number form -- "§2", "§8" -- where a section is renamed by
    renumbering it.

Both are in `docs/TODO.md` as open work. What this file covers is the
form that was measured to be rotten, in the document that was measured
to be rotten.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: The side-car is another repository (a submodule) and `docs/archive/` is
#: a dated record, not instructions. Neither is scanned. `donsetch` is a
#: second submodule, and `.worktrees/` holds other checkouts of THIS
#: repository: their markdown is a copy of these documents at an older
#: commit, so a pointer that was fixed here is still broken there -- and
#: the tree would go red on the copy instead of on the truth.
SKIP_DIRS = {".git", ".mavis", ".home", ".worktrees", "__pycache__",
             "donsetch", "node_modules", "sandbox", "venv", "archive"}

#: A pointer: a backticked path to a markdown file, then a section sign.
#: The name runs to the end of the clause -- a semicolon, a pipe or the
#: end of the line.
POINTER = re.compile(r"`(?P<path>[^`\n]+?\.md)`\s*§\s*(?P<name>[^;|\n]+)")

#: A markdown heading, a CHANGELOG release section, a CODEBOOK rule id.
HEADING = re.compile(r"^#{1,6}\s+(?P<text>.*?)\s*#*\s*$", re.MULTILINE)
RELEASE = re.compile(r"^##\s+\[(?P<name>[^\]]+)\]", re.MULTILINE)
RULE_ID = re.compile(r"^\*\*(?P<name>X-\d+)\b", re.MULTILINE)


def _norm(text: str) -> str:
    """Fold a name to something two spellings can be compared by.

    Case, punctuation and the typographic quotes are noise: the heading
    says "The bot's pin" and the pointer may say "The bot’s pin".
    """
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("—", " ").replace("–", " ")
    text = re.sub(r"[^0-9a-z]+", " ", text.lower())
    return " ".join(text.split())


def _names(raw: str) -> list:
    """The section names a pointer asks for.

    A name may be quoted (`§ "Moving a file out of the sandbox"`) and it
    may be a list (`§ X-23, X-24`), so it is cut at the closing quote
    and split at the commas.
    """
    name = raw.strip().lstrip("«\"“' ")
    for stop in ("»", "\"", "“"):
        idx = name.find(stop)
        if idx != -1:
            name = name[:idx]
    name = name.strip().rstrip(".,;:")
    return [part.strip() for part in name.split(",") if part.strip()]


def _anchors(path: Path) -> set:
    """Every name a document answers to."""
    text = path.read_text(encoding="utf-8")
    anchors = set()
    for m in HEADING.finditer(text):
        anchors.add(_norm(m.group("text")))
    for m in RELEASE.finditer(text):
        anchors.add(_norm(m.group("name")))
    for m in RULE_ID.finditer(text):
        anchors.add(_norm(m.group("name")))
    return anchors


def _documents() -> list:
    """Every markdown file in the repository, minus the exceptions."""
    out = []
    for path in sorted(ROOT.rglob("*.md")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        out.append(path)
    return out


def _pointers() -> list:
    """[(referring file, pointer path, raw name)] for the whole tree."""
    found = []
    for doc in _documents():
        text = doc.read_text(encoding="utf-8")
        for m in POINTER.finditer(text):
            found.append((doc, m.group("path").strip(), m.group("name")))
    return found


def _resolve(ref: str, referring: Path) -> Path | None:
    """Where the pointed-at file is: from the root, or next to the pointer."""
    for candidate in (ROOT / ref, referring.parent / ref):
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture(scope="module")
def pointers() -> list:
    return _pointers()


def test_the_scan_is_not_vacuous(pointers):
    """A guard on the guard.

    Every check below looks for pointers and complains about them. If
    the pattern stops matching -- the convention changes, the section
    sign is written differently -- they all pass by finding nothing,
    which is the exact failure this file exists to prevent.
    """
    assert len(pointers) >= 25, (
        f"only {len(pointers)} section pointers found in the tree; the "
        "pattern probably stopped matching, and every check below would "
        "pass by finding nothing")
    targets = {ref for _, ref, _ in pointers}
    for expected in ("CHANGELOG.md", "docs/CODEBOOK.md", "docs/BASH-SANDBOX.md",
                     "docs/ARCHITECTURE.md"):
        assert expected in targets, (
            f"no pointer targets {expected} any more; the scan is not "
            "covering the documents it is meant to cover")
    assert any(ref.startswith("TICKET-") for ref in targets), (
        "no pointer into a ticket document any more -- the relative-path "
        "form is not exercised")


def test_every_pointer_names_a_file_that_exists(pointers):
    """The file the pointer names is there.

    A pointer into a file that was renamed or moved is a pointer to
    nowhere, and it reads exactly like a pointer that works.
    """
    missing = []
    for referring, ref, _name in pointers:
        if _resolve(ref, referring) is None:
            missing.append(f"{referring.relative_to(ROOT)} -> {ref}")
    assert not missing, (
        "these pointers name a file that does not exist:\n  "
        + "\n  ".join(missing))


def test_every_pointer_names_a_section_that_exists(pointers):
    """The section the pointer names is there too.

    The name is matched against the target's headings, its CHANGELOG
    release sections and its CODEBOOK rule ids. A name that is a phrase
    of the heading matches -- "The model's read budget" against "The
    model's read budget (the MCP door)" -- because that is how a
    pointer is written.
    """
    broken = []
    for referring, ref, raw in pointers:
        target = _resolve(ref, referring)
        if target is None:
            continue  # the previous test owns that failure
        anchors = _anchors(target)
        for name in _names(raw):
            if len(_norm(name)) < 3:
                continue
            if not any(_norm(name) in anchor for anchor in anchors):
                broken.append(
                    f"{referring.relative_to(ROOT)} -> {ref} § {name!r}")
    assert not broken, (
        "these pointers name a section that does not exist:\n  "
        + "\n  ".join(broken))


def test_no_pointer_names_a_section_that_moves(pointers):
    """`Unreleased` is where work is written, not where a record is.

    At the next tag the CHANGELOG's `## [Unreleased]` becomes the
    release section, and every pointer into it starts pointing at
    whatever is unreleased *now*. Eighteen pointers in `docs/TODO.md`
    were doing exactly that: they named "§ Unreleased" for work that
    had shipped a release earlier.
    """
    moving = []
    for referring, ref, raw in pointers:
        for name in _names(raw):
            if _norm(name) == "unreleased":
                moving.append(f"{referring.relative_to(ROOT)} -> {ref}")
    assert not moving, (
        "these pointers name the CHANGELOG's Unreleased section, which is "
        "renamed at every release:\n  " + "\n  ".join(moving)
        + "\nName the section by its own heading instead -- the heading "
        "survives the move into the release section.")
