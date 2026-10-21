"""Every ledger cross-reference must point at a file that exists.

Added after shipping a reference to "item 9" in item 8, on the same
session that wrote the lesson it was meant to point at. There are eight
items. The reference was never opened.

The check is deliberately narrow: a backticked `NN-<slug>.md` or a
`NN-name` reference to a numbered item, resolved against the directory
rather than against prose. Prose mentions of "item 3" are how the
ledger reads and are not what breaks; a dangling pointer to a file is.
"""
import re
from pathlib import Path

DEBT = Path(__file__).resolve().parent.parent / "docs" / "tech-debt"
FILES = sorted(p.name for p in DEBT.glob("*.md"))

# 'item 9' / 'item 12' -- a numbered reference to a ledger entry.
BY_NUMBER = {int(m.group(1)): n for n in FILES for m in [re.match(r"(\d+)-", n)] if m}

# `08-decompose-*.md` -- a file reference.
FILE_REF = re.compile(r"`(\d{2}-[^`]*?\.md)`")


def test_the_directory_is_readable():
    assert FILES, "no ledger files found -- the glob is wrong, not the ledger"


def test_numbered_references_resolve():
    dangling = []
    for doc in DEBT.glob("*.md"):
        text = doc.read_text(encoding="utf-8")
        for match in re.finditer(r"\bitem (\d+)\b", text):
            number = int(match.group(1))
            if number not in BY_NUMBER:
                line = text[:match.start()].count("\n") + 1
                dangling.append(f"{doc.name}:{line} -> item {number}")
    assert not dangling, "references to ledger entries that do not exist:\n" + \
        "\n".join(dangling)


def test_file_references_resolve():
    dangling = []
    for doc in DEBT.glob("*.md"):
        text = doc.read_text(encoding="utf-8")
        for match in FILE_REF.finditer(text):
            target = match.group(1)
            if not (DEBT / target).exists() and target not in FILES:
                line = text[:match.start()].count("\n") + 1
                dangling.append(f"{doc.name}:{line} -> {target}")
    assert not dangling, "references to files that do not exist:\n" + \
        "\n".join(dangling)


def test_the_known_dangling_reference_is_pinned():
    """Control. This is what it looked like, so the checker above has to
    be able to fail on exactly this shape."""
    fake = "see item 9 for the lesson"
    numbers = {int(m.group(1)) for m in re.finditer(r"\bitem (\d+)\b", fake)}
    assert numbers - set(BY_NUMBER), "the control does not dangle -- the" \
        " checker cannot detect this shape and proves nothing"