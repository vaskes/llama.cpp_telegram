"""The transcript the CHAT gets is bounded; the one the bot REMEMBERS
is not.

Measured on production, 2026-10-28.  A review task ran 44 commands.
The transcript of what they printed came to 77 044 characters, the
answer to 81 154 -- and the chat got 22 messages of console noise in
front of the 4 110 characters of actual review.  The flood then hit
Telegram's rate limit, and 8 of those 22 messages were delivered
TWICE (a send that timed out was retried with the same text).

So the chat's copy of the transcript is bounded as a whole: the head
and the tail of the command list, with the middle dropped and the drop
marked.  The remembered copy keeps everything, because a follow-up
question about what a command printed has to have the answer.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot"))

import llm_result
from llm_result import COMMANDS_DROPPED, _format_executed_tools, unwrap_answer
import limits


def _turn(command_count=44, output_chars=1700):
    """A turn shaped like the one that flooded the chat."""
    executed = []
    for i in range(command_count):
        executed.append({
            "name": "bash_command",
            "args": {"command": f"sed -n '{i}p' telegram-bot/handlers.py"},
            "stdout": ("line %d " % i) + "x" * output_chars,
        })
    return executed


def _answer(executed, text="The review. 4110 characters of it."):
    return {
        "text": text,
        "executed_tools": executed,
    }


def test_the_chat_copy_fits_the_budget():
    """44 commands, 77 000 characters of output, one budget."""
    executed = _turn()
    out = _format_executed_tools(executed, budget=limits.MAX_TRANSCRIPT_CHARS)
    assert len(out) <= limits.MAX_TRANSCRIPT_CHARS, (
        f"the transcript is {len(out)} characters, the budget is "
        f"{limits.MAX_TRANSCRIPT_CHARS}")


def test_the_head_and_the_tail_are_kept():
    """The head carries what the turn started with, the tail what it
    ended with -- and the tail is the part the model quotes back."""
    executed = _turn()
    out = _format_executed_tools(executed, budget=limits.MAX_TRANSCRIPT_CHARS)
    assert "$ sed -n '0p' telegram-bot/handlers.py" in out, \
        "the first command is missing from the head"
    assert "$ sed -n '43p' telegram-bot/handlers.py" in out, \
        "the last command is missing from the tail"
    assert "$ sed -n '22p' telegram-bot/handlers.py" not in out, \
        "a command from the middle survived -- the middle is what goes"


def test_the_drop_is_marked_and_counted():
    """A cut that says nothing about its own size is a cut the reader
    cannot trust -- and a marker that undercounts is a lie."""
    import re
    executed = _turn()
    out = _format_executed_tools(executed, budget=limits.MAX_TRANSCRIPT_CHARS)
    assert "commands are not shown" in out, "the drop is not marked"
    m = re.search(r"\[\.\.\. (\d+) of (\d+) commands are not shown \.\.\.\]", out)
    assert m, f"the marker does not say how many ran: {out[:200]}"
    dropped, total = int(m.group(1)), int(m.group(2))
    assert total == 44, f"the marker says {total} commands ran, not 44"
    shown = out.count("\n$ ")
    assert shown + dropped == total, (
        f"the marker says {dropped} commands are not shown and the block "
        f"shows {shown} -- together that is {shown + dropped}, not {total}. "
        "A marker that undercounts hides what it dropped.")


def test_the_marker_is_paid_for_out_of_the_budget():
    """The marker is part of the budget: a cut that spends the whole
    limit on the text and then adds a marker on top does not fit."""
    for budget in (200, 500, 1000, 4000, 9000):
        out = _format_executed_tools(_turn(), budget=budget)
        assert len(out) <= budget, (
            f"budget {budget}: the transcript came out at {len(out)}")


def test_a_trace_that_fits_is_untouched():
    """A small turn keeps every command and gets no marker."""
    out = _format_executed_tools(_turn(command_count=2, output_chars=20),
                                  budget=limits.MAX_TRANSCRIPT_CHARS)
    assert "commands are not shown" not in out
    assert "$ sed -n '0p' telegram-bot/handlers.py" in out
    assert "$ sed -n '1p' telegram-bot/handlers.py" in out


def test_the_remembered_trace_is_not_bounded():
    """Delivery and memory are two different artefacts.  The bot has to
    remember what a command printed, or a follow-up question about it
    has no answer."""
    executed = _turn()
    answer = unwrap_answer(_answer(executed))
    assert answer.raw.count("\n$ sed -n '") == 44, (
        "the remembered trace lost commands -- the next turn cannot ask "
        "what a command printed")
    assert len(answer.raw) > limits.MAX_TRANSCRIPT_CHARS


def test_a_zero_budget_means_no_transcript_in_the_chat():
    """0 is a valid budget: the chat gets the answer alone, and the
    memory still has the commands."""
    executed = _turn()
    original = limits.MAX_TRANSCRIPT_CHARS
    try:
        limits.MAX_TRANSCRIPT_CHARS = 0
        answer = unwrap_answer(_answer(executed))
    finally:
        limits.MAX_TRANSCRIPT_CHARS = original
    assert "```bash" not in answer.text, "the chat still got a transcript"
    assert "The review." in answer.text
    assert answer.raw.count("\n$ sed -n '") == 44


def test_one_command_with_a_huge_output_is_still_condensed():
    """The per-command cap is unchanged: one command that printed a
    megabyte must not become a megabyte-long message."""
    executed = [{
        "name": "bash_command",
        "args": {"command": "cat big.log"},
        "stdout": "y" * 50_000,
    }]
    out = _format_executed_tools(executed, budget=limits.MAX_TRANSCRIPT_CHARS)
    assert len(out) <= limits.MAX_TRANSCRIPT_CHARS
    assert "were dropped from the middle" in out, \
        "the per-command cut is not marked"


def test_the_answer_becomes_at_most_two_chat_messages():
    """The invariant the operator actually felt: a 44-command turn
    must not be a wall of console noise in the chat."""
    from telegram_format import render_html, split_html
    from chat_utils import TELEGRAM_TEXT_CHUNK

    executed = _turn()
    answer = unwrap_answer(_answer(executed))
    chunks = split_html(render_html(answer.text), TELEGRAM_TEXT_CHUNK)
    assert len(chunks) <= 2, (
        f"the answer became {len(chunks)} chat messages "
        f"({len(answer.text)} characters)")


def test_the_marker_format_is_one_string():
    """One marker, so a reader can recognise it wherever it appears."""
    assert COMMANDS_DROPPED.format(dropped=3, total=9) == \
        "[... 3 of 9 commands are not shown ...]"
