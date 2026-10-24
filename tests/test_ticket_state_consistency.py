"""A ticket cannot be decided in one document and open in another.

Added 2026-10-24, after the operator caught exactly this: the decision
"we are not adding a guaranteed minimum of N messages" was recorded on
2026-10-20, and two documents on 2026-10-23 still carried it as an open
question — the index said `open, low impact` and the backlog listed it
under "the blocker is not closed".

Nothing was broken. The code was right, the decision was right, and the
documents disagreed with each other. That is the dangerous shape: a
reader who opens two files gets two different answers to "is this
pending?", and the one that says "open" is the one a reader is more
likely to believe, because a backlog is where people look for work.

So the check is mechanical, in both directions:

  * **inside the backlog** — a section under "## Open ..." whose status
    line says closed/decided is a contradiction with its own heading;
  * **between the backlog and the index** — a question the tickets index
    marks answered must not appear in an open section of the backlog.

The first is the one that catches a *decayed* entry: the item was
decided, the section was moved to the closed list, and the status line
was never updated — or the item was decided in place and nobody moved
it. The second catches the disagreement *between* documents, which is
what the operator actually found.

Deliberately narrow: it checks the state markers, not the prose. A
sentence can still be wrong with a right status next to it.

The scan is not allowed to be vacuous: if the sections stop being
findable, the checks below would pass by finding nothing.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TODO = ROOT / "docs" / "TODO.md"
INDEX = ROOT / "docs" / "tickets" / "README.md"

#: A ticket id: T70, T70.5, T68b. NOT T-FREEZE-1 (the dash means the
#: token is a different namespace -- the freeze list, not the tickets).
TICKET_ID = re.compile(r"\bT\d+(?:\.\d+)?[a-z]?\b")

#: Words that mean "this is not pending". Checked against the FIRST
#: word of the status value, not the whole line: the T68b status says
#: "deferred by the operator ... are recorded as closed" and must not
#: trip on the word "closed" buried in the explanation.
DECIDED = ("✅", "closed", "done", "decided", "resolved", "implemented",
           "shipped", "fixed")

STATUS = re.compile(r"^\*\*Status:\*\*\s*(?P<value>.*)$", re.MULTILINE)


def _sections(text: str) -> list:
    """[(heading, body)] for every `###` section."""
    out = []
    current = None
    body = []
    for line in text.splitlines():
        if line.startswith("### "):
            if current is not None:
                out.append((current, "\n".join(body)))
            current = line[4:].strip()
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        out.append((current, "\n".join(body)))
    return out


def _open_sections(text: str) -> list:
    """The sections that live under a `## Open ...` heading.

    "## Accepted as documented risk" is a third state -- neither open
    nor closed -- and is deliberately not included.
    """
    out = []
    in_open = False
    for heading, body in _sections(text):
        if heading.startswith("Open "):
            in_open = True
            continue
        if in_open:
            out.append((heading, body))
    # A `## ` heading that is not "Open ..." ends the open region; the
    # section splitter above loses that boundary, so recompute it here.
    text_sections = []
    region = None
    current = None
    body = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                text_sections.append((region, current, "\n".join(body)))
            region = line[3:].strip()
            current = None
            body = []
        elif line.startswith("### "):
            if current is not None:
                text_sections.append((region, current, "\n".join(body)))
            current = line[4:].strip()
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        text_sections.append((region, current, "\n".join(body)))
    return [(h, b) for region, h, b in text_sections
            if region and region.startswith("Open ")]


def _decided_value(status_value: str) -> bool:
    """Is this status line a "not pending" marker?"""
    value = status_value.strip().lstrip("*").strip()
    if not value:
        return False
    first = value.split()[0].strip("*").lower()
    return first in DECIDED


#: Words that mean "this question is settled" in a ticket document.
DECIDED_WORDS = ("закрыт", "закрыто", "решено", "решён", "решен", "closed",
                 "decided", "answered", "не добавляю", "не добавляем")


def _decided_somewhere() -> set:
    """Question ids that the ticket documents themselves mark as settled.

    Two shapes count, because the documents use two shapes:

    * a LINE that carries both the id and the marker -- the T70 ticket's
      question list, where a resolved question is struck through and
      answered on the same line;
    * a `###` SECTION whose HEADING carries the id and the marker -- the
      T66 ticket, where the whole section is the answer to a question.

    This is the direction the operator found: the ticket said "decided"
      and the INDEX said "open, low impact".
    """
    ids = set()
    for path in sorted((ROOT / "docs" / "tickets").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            low = line.lower()
            if not any(w in low for w in DECIDED_WORDS):
                continue
            ids.update(TICKET_ID.findall(line))
        for heading, body in _sections(text):
            low = heading.lower()
            if not any(w in low for w in DECIDED_WORDS):
                continue
            ids.update(TICKET_ID.findall(heading))
    return ids


def _index_rows(index_text: str):
    """[(id, state_cell)] for every question row of the tickets index."""
    rows = []
    for line in index_text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        found = TICKET_ID.findall(cells[1])
        if not found:
            continue
        rows.append((found, cells[-1]))
    return rows


def _answered_ids(index_text: str) -> set:
    """Question ids the tickets index marks as answered.

    The table is `| # | Question | Owner | State |`; a row is answered
    when its state cell starts with a check mark. A struck-through
    question (`~~T69 ...~~`) is also answered -- the row keeps the
    original text and adds the answer next to it.
    """
    ids = set()
    for line in index_text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        state = cells[-1]
        if "✅" not in state:
            continue
        ids.update(TICKET_ID.findall(cells[1]))
    return ids


@pytest.fixture(scope="module")
def todo_text() -> str:
    return TODO.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def open_sections(todo_text) -> list:
    return _open_sections(todo_text)


def test_the_open_sections_were_found(open_sections):
    """A guard on the guard.

    If the section splitter stops matching, every check below passes by
    finding nothing -- which is the exact failure this file exists to
    prevent, one level up.
    """
    # Six entries were open on 2026-10-24, after four of the freeze
    # items were closed (T-FREEZE-CHUNK-1, -AUTO-1, -DOC-1, -AUTO-2).
    # The floor is deliberately low and deliberately counted: it is there
    # to catch a splitter that stopped matching, not to keep a number of
    # open items in line. Closing one more item means moving this
    # number on purpose, not that the check has stopped working.
    assert len(open_sections) >= 6, (
        f"only {len(open_sections)} open sections found in {TODO.name}; the "
        "splitter probably stopped matching. Every check below would pass "
        "by finding nothing.")


def test_no_open_section_claims_to_be_decided(open_sections):
    """The decay case: decided in place, never moved.

    A section under "## Open ..." whose status line says CLOSED or
    "decided" contradicts its own heading. The operator reads the
    heading, the list gets longer, and the work never gets done.
    """
    offenders = []
    for heading, body in open_sections:
        m = STATUS.search(body)
        if not m:
            continue
        if _decided_value(m.group("value")):
            first = m.group("value").strip().split()[0]
            offenders.append(f"{heading} -> status starts with {first!r}")
    assert not offenders, (
        "these sections are under '## Open ...' but their status says they "
        "are decided:\n" + "\n".join(offenders))


def test_a_decided_question_is_not_open_in_the_index():
    """The direction the operator actually found.

    The ticket documents say the question is settled; the index still
    says "open, low impact". The index is the table people scan to
    decide what is pending, so a stale "open" there is the one that
    produces work nobody does.
    """
    decided = _decided_somewhere()
    assert decided, (
        "no decided question ids found in docs/tickets/ -- the markers "
        "probably changed, and this check would pass by finding nothing")

    offenders = []
    for ids, state in _index_rows(INDEX.read_text(encoding="utf-8")):
        low = state.lower()
        if "✅" in state or "moot" in low:
            continue
        for tid in ids:
            if tid in decided:
                offenders.append(f"{tid} (index says {state!r})")
    assert not offenders, (
        "the ticket documents mark these settled, but the index still "
        "lists them as pending: " + "; ".join(offenders))


def test_an_answered_question_is_not_in_an_open_section(open_sections):
    """The disagreement BETWEEN documents -- what the operator found.

    The tickets index says the question is answered; the backlog still
    lists it as pending work. Two files, two answers, and the backlog
    is the one a reader trusts.
    """
    answered = _answered_ids(INDEX.read_text(encoding="utf-8"))
    assert answered, (
        "no answered questions found in the tickets index -- the table "
        "format probably changed, and this check would pass by finding "
        "nothing")

    offenders = []
    for heading, body in open_sections:
        # Only the lines that ARE entries count: a heading, or a table
        # row. A prose mention is not a pending item -- "T66.1 left this
        # table on 2026-10-24" is a note about a closed item, and a
        # check that cannot tell that from a backlog row would have to
        # delete the notes.
        entry_lines = [ln for ln in body.splitlines()
                       if ln.lstrip().startswith("|")]
        blob = heading + "\n" + "\n".join(entry_lines)
        for tid in sorted(set(TICKET_ID.findall(blob))):
            if tid in answered:
                offenders.append(f"{tid} (in '{heading}')")
    assert not offenders, (
        "the tickets index marks these answered, but they are still in an "
        "open section of the backlog: " + ", ".join(offenders))
