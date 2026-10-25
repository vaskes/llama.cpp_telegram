"""What happens when the user presses Stop, and where it used to be written.

TECH-DEBT #13, second block. The tail of an aborted turn -- log, roll
back the transcript, edit the thinking message, return -- was written out
seven times across handlers.py. The line that matters is
`turn.rollback('aborted')`: a transcript persisted for a turn the user
cancelled is why the next message gets answered about something that did
not happen.

The log text is part of the contract. An operator tells a cancelled first
POST from a cancelled retry by the words "on retry", so those two
messages are pinned byte for byte below. Unifying them would read
tidier and break the runbook.

Also here: comments that cite a line number. Four of them were pointing
at addresses that no longer held the code they described -- one at
`handlers.py:538`, deleted by the very commit that added the check, and
one at `call_llama.py:1401` in a 478-line file. A comment that names an
address rather than a property is wrong the moment anything moves, and
nothing reads it again.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=13)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from llm_abort import finalize_aborted_turn  # noqa: E402


class FakeEvent:
    def __init__(self, is_set=False):
        self._is_set = is_set

    def is_set(self):
        return self._is_set


class FakeTurn:
    """Records what the transcript was told to do."""

    def __init__(self):
        self.rollbacks = []

    async def rollback(self, reason=None):
        self.rollbacks.append(reason)


class FakeThinking:
    """`edit_text` that raises when asked to edit a dead message, which is
    what happens when Stop arrives after the reply was already posted."""

    def __init__(self, fail=False):
        self.fail = fail
        self.edits = []

    async def edit_text(self, text, *a, **kw):
        if self.fail:
            raise RuntimeError("message to edit not found")
        self.edits.append(text)


async def finalize(label, response=None, abort=False, turn=..., thinking=None,
                   attempt="primary"):
    turn = FakeTurn() if turn is ... else turn
    thinking = FakeThinking() if thinking is None else thinking
    await finalize_aborted_turn(
        label=label, response=response, abort_event=FakeEvent(abort),
        thinking=thinking, turn=turn, attempt=attempt)
    return turn, thinking


# --- the log text, which is the runbook contract --------------------------

@pytest.mark.parametrize("label", ["handle_photo", "handle_text"])
async def test_the_primary_post_won_race_is_logged_verbatim(label, caplog):
    turn, _ = await finalize(label, response="x" * 42, abort=True)
    assert f"[{label}] ABORTED via Stop; discarding 42-char response (post-won race)" \
        in caplog.text
    assert turn.rollbacks == ["aborted"]


@pytest.mark.parametrize("label", ["handle_photo", "handle_document"])
async def test_the_primary_cancellation_is_logged_verbatim(label, caplog):
    await finalize(label, response=None, abort=True)
    assert f"[{label}] ABORTED by user via Stop button, moving to next" in caplog.text


async def test_the_retry_abort_keeps_its_own_wording(caplog):
    """"on retry" is how an operator tells the two apart. Not cosmetic."""
    await finalize("handle_text", response="x" * 7, abort=True, attempt="retry")
    assert ("[handle_text] ABORTED on retry via Stop; "
            "discarding 7-char response") in caplog.text
    assert "post-won race" not in caplog.text


async def test_the_retry_cancellation_is_logged_verbatim(caplog):
    await finalize("handle_text", response=None, abort=True, attempt="retry")
    assert "[handle_text] ABORTED on retry, moving to next" in caplog.text


async def test_an_unknown_attempt_is_rejected():
    """A typo'd attempt would silently log the primary wording, which is
    the kind of wrong that only shows up while reading logs."""
    with pytest.raises(ValueError):
        await finalize_aborted_turn(
            label="handle_text", response=None, abort_event=FakeEvent(True),
            thinking=FakeThinking(), attempt="retries")


# --- what the user sees, and what the transcript does ---------------------

async def test_the_thinking_message_says_stopped():
    _, thinking = await finalize("handle_text", abort=True)
    assert thinking.edits == ["⏹ Stopped"]


async def test_a_dead_thinking_message_does_not_break_the_abort():
    """Stop can land after the reply was posted, leaving nothing to edit."""
    turn, _ = await finalize("handle_text", abort=True,
                             thinking=FakeThinking(fail=True))
    assert turn.rollbacks == ["aborted"]


async def test_a_turn_is_rolled_back_with_the_aborted_reason():
    """The reason string is what the transcript reads back as."""
    turn, _ = await finalize("handle_text", abort=True)
    assert turn.rollbacks == ["aborted"]


async def test_no_turn_means_no_rollback_and_no_crash():
    """Photo turns have nothing in the transcript to undo -- the image was
    never written -- and pass no turn at all."""
    thinking = FakeThinking()
    await finalize_aborted_turn(
        label="handle_photo", response=None, abort_event=FakeEvent(True),
        thinking=thinking, turn=None)
    assert thinking.edits == ["⏹ Stopped"]


async def test_a_post_won_race_response_is_rolled_back_too():
    """call_llama returned a full answer and Stop still cancels it."""
    turn, _ = await finalize("handle_text", response="an answer", abort=True)
    assert turn.rollbacks == ["aborted"]


# --- handlers delegating, rather than re-implementing ---------------------

def _handlers_tree():
    return ast.parse((BOT / "handlers.py").read_text(encoding="utf-8"))


def _functions(tree):
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)}


@pytest.mark.parametrize("handler,turn_expected", [
    ("handle_photo", False),        # no transcript entry for an image
    ("handle_voice", True),
    ("handle_document", True),
    ("handle_text", True)])
def test_the_handler_delegates_and_keeps_its_rollback_decision(
        handler, turn_expected):
    """Both halves. Delegating without passing turn drops a rollback; and
    passing one where there is nothing to undo invents a rollback call that
    no test expected."""
    fn = _functions(_handlers_tree())[handler]
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "finalize_aborted_turn"]
    assert calls, f"{handler} does not call finalize_aborted_turn"
    assert len(calls) == (2 if handler != "handle_voice" else 1), (
        f"{handler}: {len(calls)} calls; voice has no empty-response retry, "
        f"the other three have a primary and a retry abort")
    for call in calls:
        kws = {k.arg for k in call.keywords}
        assert ("turn" in kws) == turn_expected, (
            f"{handler}: passes turn={turn_expected}, expected "
            f"{turn_expected}")


ABORT_CONDITIONS = [
    "bot_response is None or abort_event.is_set()",
    "retry_response is None or abort_event.is_set()",
]


@pytest.mark.parametrize("condition", ABORT_CONDITIONS)
def test_every_abort_branch_delegates_and_does_nothing_else(condition):
    """The abort branch is a delegation, not an implementation.

    Scoped to branches whose condition *is* the abort test, rather than a
    blanket ban on `rollback("aborted")`: that reason is also how a turn
    is undone when `call_llama` raises, or when persisting the turn
    fails. A handler-wide ban would be red on correct code, and a check
    that is red on correct code is one people delete.

    Shape: await the helper, then return. Anything else in the branch --
    a rollback written inline, an edit_text, a second log -- is the tail
    growing back.
    """
    wanted = ast.dump(ast.parse(condition, mode="eval").body)
    offenders = []
    for fn in _functions(_handlers_tree()).values():
        for n in ast.walk(fn):
            if isinstance(n, ast.If) and ast.dump(n.test) == wanted:
                shape = [type(s).__name__ for s in n.body]
                ok = (len(n.body) == 2
                      and shape[0] == "Expr"
                      and isinstance(n.body[0].value, ast.Await)
                      and isinstance(n.body[0].value.value, ast.Call)
                      and getattr(n.body[0].value.value.func, "id", None)
                      == "finalize_aborted_turn"
                      and isinstance(n.body[1], ast.Return))
                if not ok:
                    offenders.append(f"{fn.name}:{n.lineno} -> {shape}")
    assert not offenders, offenders


def test_handlers_do_not_write_the_stopped_marker_themselves():
    """Scoped to handlers.py on purpose.

    dispatch.py writes the same string, and is right to: its callback
    path deletes the thinking message when Stop is pressed and only
    substitutes the text if the delete failed. That is a different layer
    with a different job, reached without ever entering a handler, so
    folding it into llm_abort would mean passing a callback_query in.
    A first draft of this test banned the marker repo-wide and was red on
    that code.
    """
    hits = [n.lineno for n in ast.walk(ast.parse(
        (BOT / "handlers.py").read_text(encoding="utf-8")))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "Stopped" in n.value]
    assert not hits, hits


# --- comments that cite a line number ------------------------------------

LOCATION_REF = re.compile(r"\b[\w/]+\.py:\d+")


@pytest.mark.parametrize("path", sorted(BOT.rglob("*.py")))
def test_no_comment_cites_a_line_number(path):
    """A comment should describe a property, not an address.

    Four of these had gone stale before the check existed: one pointed at
    `handlers.py:538`, which the commit adding this check deleted, and one
    at `call_llama.py:1401`, in a file 478 lines long. Nothing reads a
    comment twice, so a wrong one costs nothing until it misleads someone
    editing at that spot.

    Parsed, not grepped for the file: the module's own docstring in this
    test names all four, and `path.name` would match it.
    """
    bad = []
    # Comments are not in the AST. tokenize is the only place they live.
    import io
    import tokenize
    for tok in tokenize.generate_tokens(
            io.StringIO(path.read_text(encoding="utf-8")).readline):
        if tok.type == tokenize.COMMENT and LOCATION_REF.search(tok.string):
            bad.append(f"{path.name}:{tok.start[0]}: {tok.string.strip()[:70]}")
    assert not bad, (
        "comments citing a line number (they rot; name the property):\n  "
        + "\n  ".join(bad))