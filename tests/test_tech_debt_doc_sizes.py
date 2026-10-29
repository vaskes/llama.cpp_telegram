"""Closed items must say what the file looks like *now*, and be checked.

Three stale numbers were sitting in `docs/tech-debt/` on 2026-10-23,
all of them written by me:

- item 1 claimed `bot.py` ended at 261 lines; it was 124, and had been
  124 since the commit that closed the item;
- item 13 quoted `handlers.py` at 1357, which was true when it was
  written and stopped being true when TECH-DEBT #10 block C moved the
  sender-label filter out;
- the index and item 10 both said `dispatch.py` went 1672 → 1278, and
  1278 was `handlers.py`. I typed one file's number onto another file's
  row while closing the item.

None of them was caught by reading. They were caught by grepping every
`N -> M lines` in the directory and comparing to `wc -l`.

That comparison cannot be a test on its own, because most numbers in
these documents are deliberately historical: item 8 records
`call_llama`: 917 -> 794 -> 796, one line per commit, and only the last
of those was ever current. A test that read prose and guessed which
number was "the" number would either miss all three or invent failures
on the intermediate steps.

So the documents carry the claim explicitly. A closed item ends with:

    <!-- current: handlers.py=1278 -->

and this test requires two things: that the numbers in that comment
match the files on disk, and that a closed item has one at all.

Every `current:` comment of an item is checked, not only the first
one. Item 10 carries two -- the page of the item that is still open --
and the second one was read by nobody: the file it names had grown in
the very commit that added it, and the number stayed. A check that
reads the first marker of a page is green exactly where it happens to
be right. Adding
a new closed item without declaring its current numbers is a failure,
not a silent omission -- otherwise the next reader has exactly the
problem this test exists to prevent.

Historical numbers stay historical. Nothing here forbids an item from
recording what the file looked like at the time; it forbids the item
from leaving that as the only claim.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item="index")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "tech-debt"
BOT = ROOT / "telegram-bot"

DECLARATION = re.compile(r"<!--\s*current:\s*(?P<body>.*?)\s*-->")
ENTRY = re.compile(r"(?P<path>[\w./\-]+\.py)\s*=\s*(?P<lines>\d+)")


def _closed_docs():
    out = []
    for path in sorted(DOCS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^state:\s*closed\s*$", text, re.MULTILINE):
            out.append((path, text))
    return out


def test_there_are_closed_docs_to_check():
    """A guard on the guard: if the frontmatter drifts, this goes red
    rather than the loop below quietly testing nothing."""
    assert len(_closed_docs()) >= 10, (
        "expected the closed items to be discoverable via 'state: closed' "
        "frontmatter; the doc set is smaller than the index claims")


@pytest.mark.parametrize("path,text", _closed_docs(),
                         ids=[p.name for p, _ in _closed_docs()])
def test_closed_item_declares_its_current_sizes(path, text):
    match = DECLARATION.search(text)
    assert match, (
        f"{path.name} is closed but does not declare what its files look "
        f"like now. Add a line like "
        f"'<!-- current: file.py=123 -->' (one per file the item names). "
        f"Without it the historical numbers in the prose are the only "
        f"claim a reader has, and that is exactly how item 1 ended up "
        f"saying 261 when the file was 124.")


@pytest.mark.parametrize("path,text", _closed_docs(),
                         ids=[p.name for p, _ in _closed_docs()])
def test_declared_sizes_match_the_tree(path, text):
    matches = list(DECLARATION.finditer(text))
    if not matches:
        pytest.skip("covered by test_closed_item_declares_its_current_sizes")

    wrong = []
    for number, match in enumerate(matches, 1):
        entries = ENTRY.findall(match.group("body"))
        assert entries, f"{path.name}: 'current:' comment names no file"

        for rel, declared in entries:
            declared_n = int(declared)
            candidates = [ROOT / rel, BOT / rel, BOT / Path(rel).name]
            target = next((c for c in candidates if c.is_file()), None)
            assert target, (
                f"{path.name} declares current:{rel} but no such file exists "
                f"(looked in {', '.join(str(c) for c in candidates)})")
            actual = len(target.read_text(encoding="utf-8").splitlines())
            if actual != declared_n:
                wrong.append(
                    f"{path.name} (current-comment {number}) declares "
                    f"{rel} = {declared_n}, but it is {actual} lines")

    assert not wrong, (
        "\n".join(wrong)
        + "\n\nEither the code moved and the claim did not, or the claim "
          "was never about this file -- fix the number, not the test.")