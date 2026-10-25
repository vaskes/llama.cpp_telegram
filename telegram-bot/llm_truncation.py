"""How a cut looks.

Every truncation in this codebase used to be silent: the text was
shorter and the reader could not tell that anything was missing. A
model that reads a truncated tool result cannot tell that there was
more, and answers as if it had seen the whole thing. A user who reads a
truncated transcript believes the command printed exactly that.

One place decides what a cut looks like, so the marker is the same
everywhere and one test can pin it.

The shape is head + marker + tail, and the reason is that both ends of
a command's output carry something the other end does not:

* the beginning carries the sandbox's own marker. When the beginning of
  an output does not fit in the session scrollback, the sandbox says so
  on its **first line** -- "the beginning of this output no longer fits
  in the session scrollback, what follows is only its tail". A cut that
  keeps the tail throws that line away, and the reader is shown a
  fragment as if it were the whole thing. That is the exact defect the
  sandbox fixed on its side in v2.7.0, and it would be reintroduced
  here by a cut that keeps the wrong end.

* the end carries what the command printed last: the part the model
  quotes back when it pastes the output into its answer, and the part
  the bot compares that quote against (see `llm_artifact._is_console_output`,
  which asks whether a code block is console output rather than a
  deliverable -- a comparison that stops matching turns the output back
  into a `.txt` document).

So the middle is what goes, and the marker says so.
"""
from __future__ import annotations

__all__ = ["condense", "MIDDLE_DROPPED"]


#: The marker that says what the cut took.
#:
#: The wording is deliberately not the sandbox's ("[output truncated at
#: N bytes]"). That one means the *tail* was cut; a reader who has seen
#: both has to be able to tell which cut they are looking at.
MIDDLE_DROPPED = (
    "[... {dropped} of {total} characters were dropped from the middle ...]"
)


def condense(text: str, limit: int) -> str:
    """`text`, bounded to about `limit` characters, with the middle marked.

    The head and the tail are kept, split on line boundaries where the
    text allows it: half a line in the middle of a transcript looks
    like a broken command, and the reader has to guess what it was.

    The marker is part of the budget. A cut that spends the whole limit
    on the text and then adds a marker on top is a cut that does not
    fit -- and the ledger that carries it is exactly as big as it was
    supposed to stop being.
    """
    if not isinstance(text, str):
        text = str(text)
    total = len(text)
    if limit <= 0:
        return MIDDLE_DROPPED.format(dropped=total, total=total)
    if total <= limit:
        return text

    # The marker carries numbers, and those numbers depend on how much
    # is kept -- so the room for the marker is reserved first, with the
    # widest number it could have.
    reserved = len(MIDDLE_DROPPED.format(dropped=total, total=total)) + 2
    if reserved >= limit:
        # The budget cannot hold the marker and any text at all. Say
        # that everything was dropped; a cut that lies about its own
        # size is worse than no cut.
        return MIDDLE_DROPPED.format(dropped=total, total=total)

    budget = limit - reserved
    head_budget = budget // 2
    tail_budget = budget - head_budget
    head = _head_of(text, head_budget)
    tail = _tail_of(text, tail_budget)

    marker = ""
    for _ in range(4):
        kept = len(head) + len(tail)
        marker = MIDDLE_DROPPED.format(dropped=total - kept, total=total)
        overflow = len(head) + len(tail) + len(marker) + 2 - limit
        if overflow <= 0:
            break
        # The marker came out a character or two longer than reserved.
        # Take it off the tail -- the head carries the sandbox's marker,
        # and the tail is the part that can lose a line.
        tail_budget = max(0, tail_budget - overflow)
        tail = _tail_of(text, tail_budget)

    return f"{head}\n{marker}\n{tail}"


def _head_of(text: str, budget: int) -> str:
    """The head, cut on a line boundary where the text allows it."""
    if budget <= 0:
        return ""
    head = text[:budget]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    return head


def _tail_of(text: str, budget: int) -> str:
    """The tail, cut on a line boundary where the text allows it."""
    if budget <= 0:
        return ""
    tail = text[-budget:]
    start = tail.find("\n")
    if start >= 0:
        tail = tail[start + 1:]
    return tail
