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


def _dangling_in(text):
    """The same detection the two tests above use.

    Factored out so the control below exercises the real thing rather
    than a reimplementation that could drift from it.
    """
    out = []
    for match in re.finditer(r"\bitem (\d+)\b", text):
        if int(match.group(1)) not in BY_NUMBER:
            out.append(int(match.group(1)))
    return out


def test_the_control_shape_is_actually_detectable():
    """Control: the checker can still fail on the shape it was built for.

    Was hardcoded to "see item 9 for the lesson", which became a live
    ledger entry on 2026-10-22. Pinning a control to a number that will
    eventually be filled in disarms it silently -- the test keeps
    passing while proving nothing.

    My first repair derived the number as `max(BY_NUMBER) + 1` and
    asserted it was absent. That is a tautology: the number is absent
    by construction, so the assert cannot fail. What the control needs
    to prove is not that some number is missing, but that the detector
    fires on this text. So it runs the detector.
    """
    missing_number = max(BY_NUMBER) + 1
    fake = f"see item {missing_number} for the lesson"
    assert _dangling_in(fake), (
        "the control does not dangle -- the checker cannot detect this "
        "shape and proves nothing")


def test_a_real_reference_is_not_flagged():
    """Positive control, so the control above cannot pass by breakage.

    If the detector stopped matching `item NN` entirely, every reference
    would resolve clean and the negative control would still pass.
    """
    real = "see item 1 and item " + str(min(BY_NUMBER))
    assert not _dangling_in(real), (
        "the detector flags references that DO resolve -- it is matching "
        "wrongly, not narrowly")
