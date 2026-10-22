"""Production code must not point at line numbers; the moves invalidate them.

Four `file.py:NNN` references had accumulated and every one of them was
stale by the time it was written down:

- `call_llama.py:1401`, quoted twice in `test_llm_abort.py`, in a file
  that is 478 lines long;
- `call_llama.py:1036` in `test_drift_call_llama_names.py`, same file;
- `dispatch.py:1020` in `test_selftest_paths.py`, quoted at a moment
  when `dispatch.py` was over 1600.

All four had been correct when written. That is exactly the problem: a
line reference is a fact about a moment, and the moments do not stop
moving. The reference reads like a fact about the present, so nobody
checks it, so it decays.

**The scope here is production code only, and the exclusion is
deliberate.** The four above live in test docstrings, and they are
*correct*: each one says the bug *was* at that line, quoting an
incident. Rewriting those would erase the account of what happened and
buy nothing -- the claim is about the past, and the past does not move.
This file exists for the forward-looking half: a reference that tells a
reader where something is now.

So the rule is: `telegram-bot/**.py` carries no `file.py:NNN` at all,
whether in a comment, docstring or string. Say what the code does and
let the reader find it. Where the location genuinely matters, name the
symbol -- `_tag_sender`, not `llm_messages.py:41` -- because a symbol
moves with its code and a line number does not.

`docs/tickets/` and `docs/review/` are excluded for the same reason as
the test docstrings: they are dated records of a specific review, and
rewriting their line references would falsify them.
"""
import re
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
EXCLUDED_DIRS = {"__pycache__"}

# `some_file.py:123`, but not `https://...` or `3.11:2`.
LINE_REF = re.compile(r"(?<![\w/])([\w]+\.py):(\d+)\b")

def _production_files():
    for path in sorted(BOT.rglob("*.py")):
        if EXCLUDED_DIRS & set(path.parts):
            continue
        yield path


def test_there_are_files_to_check():
    """A guard on the guard. If BOT ever moves, this goes red instead of
    the loop below reporting a cheerful pass over zero files."""
    files = _PRODUCTION
    assert len(files) > 30, (
        f"found only {len(files)} production modules -- the path is "
        f"probably wrong, and a test over nothing is not a pass")


_PRODUCTION = list(_production_files())


@pytest.mark.parametrize("path", _PRODUCTION, ids=lambda p: p.name)
def test_no_line_references_in_production(path):
    text = path.read_text(encoding="utf-8")
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        for match in LINE_REF.finditer(line):
            if stripped.startswith(("http://", "https://")):
                continue
            hits.append(
                f"{path.name}:{lineno} -> {match.group(0)}\n"
                f"    {stripped[:90]}\n"
                f"    name the symbol instead; line numbers are a "
                f"statement about one moment and this code has moved "
                f"since."
            )
    assert not hits, "stale-by-construction line references:\n\n" + \
        "\n".join(hits)


def test_the_pattern_can_see_a_real_reference():
    """Control. A detector that cannot be made to fire is decoration."""
    sample = "the guard lives in llm_messages.py:41, next to the header"
    assert LINE_REF.search(sample), (
        "the detector no longer matches the thing it was written for, so "
        "every green run above is meaningless")
    assert not LINE_REF.search("python:3.11 and llm_messages.py next door")
    assert not LINE_REF.search("see https://example.com/x.py:12 for detail")