"""What a finished turn looks like on the way out of `call_llama`.

TECH-DEBT #13. `call_llama` returns either a string or, when the model
ran tools, a dict carrying `text` plus `executed_tools`. Every one of
the four handlers had to flatten that, and every one of them flattened it
slightly differently:

    if isinstance(_result, dict):
        bot_response = _result.get("text") or ""
        _exec = _result.get("executed_tools") or []
        if _exec:
            _hist = _format_executed_tools(_exec)
            if _hist:
                bot_response = _hist + "\n\n" + bot_response
    else:
        bot_response = _result

-- four byte-identical copies of that, plus three near-identical copies
of the same thing over `retry_response` in the empty-response retry.
The preamble in front of the answer is the reason it is a hazard rather
than a nuisance: a copy that drops it does not fail, it answers without
the tool trace, and the user sees a response that looks like the model
did nothing.

`unwrap_answer` is the one form. The primary and retry sites were
not two shapes of the same rule; they were one rule written twice, and
the retry copy's missing `else` was the only difference -- a non-dict
`retry_response` left the name alone, which is exactly what returning
the value unchanged does.

`_format_executed_tools` moved here with it rather than being passed in.
It is the other half of the same decision: given the executed tools,
render the block. Keeping it in `handlers.py` would have made this
module a leaf that imports the module it was extracted from, which is
the import cycle TECH-DEBT #2 was closed to remove.

The second half of the answer
-----------------------------

An answer is not only text. When the model answers with a document --
html, json, sql, code -- the document is a file, and a file in a chat
is something the user can open. So the answer carries two things now:
the prose, and the files the prose was wrapped around.

The split is made here rather than in the handlers because the *type* of
the result is what says whether the text is an answer at all. A dict is
an answer: the model produced content, and the code blocks in it are
files. A plain string is a diagnostic -- "the model spent all its
tokens on reasoning" -- and it carries the model's reasoning tail.
That tail is not a file and must never become one: it is the model
thinking out loud about a failure, not a deliverable. The type is the
only thing that tells the two apart, so the split has to happen where
the type is still visible.

The transcript and the delivered file
-------------------------------------

The transcript in front of the answer is the console output of the
commands the model ran (the operator's rule, 2026-10-24: the console
output belongs in the chat).  It is also the one place where a file
can arrive twice.

Measured on production, 2026-10-25.  The operator asked for an HTML
file.  The model wrote it in the sandbox with a heredoc, called
`send_file`, and the document arrived -- 595 bytes, correct.  Then the
chat got a second message: the transcript of the heredoc, which is the
same HTML, twice over, once as the command and once as the output,
because a heredoc command carries the whole file in its own text and
the sandbox answers with the pane that shows what was typed.

The user asked for a file and got the file and a copy of it.  So when
a file reached the chat, the transcript does not go with it.

Two things are deliberately NOT changed:

  * the transcript still goes to the chat when no file was delivered.
    There it is the only place the user can see what the model did, and
    the operator asked for it by name;
  * the transcript still goes into `raw`, which is what the bot
    remembers, even when it is kept out of the chat.  Delivery and
    memory are two different artefacts: what the user reads and what
    the next turn needs are not the same thing.

And the files the answer carries (`extract_files`) are not counted as
"a file was delivered": those come out of the model's own text, so the
transcript cannot be showing them a second time.

The bound, and where it lives
-----------------------------

The transcript's size grows with the number of commands the model ran,
so the chat's copy is bounded by `limits.MAX_TRANSCRIPT_CHARS` -- the
head and the tail of the command list, with the middle dropped and the
drop marked.  The remembered copy (`raw`) is not bounded: a follow-up
question about what a command printed has to have the answer.  The
number is a setting because it decides how much of the turn the chat
sees, and that is the operator's call -- not the code's.

Pure: no client, no clock, no await. That is the point of the
extraction and it is what makes it testable on its own.
"""
from __future__ import annotations

from typing import NamedTuple

from llm_artifact import Artifact, extract_files
from llm_truncation import condense
import limits

__all__ = ["Answer", "unwrap_answer"]


class Answer(NamedTuple):
    """A finished answer: the text to send, the files it carries, and
    the answer as the model wrote it.

    `text` is what the chat gets. `files` is what arrives as a document
    each. `raw` is the answer with the code blocks still in it, and it
    is what the bot remembers: the model wrote that code, and a follow-up
    question about the file it just sent needs it to still be there.

    For a diagnostic there are no files, and `text` is the diagnostic,
    unchanged. `text` is None for an aborted turn, which is what
    `call_llama` returns when the user pressed Stop.
    """
    text: str | None
    files: list[Artifact]
    raw: str


#: How much of one command's output goes into the chat.
#:
#: The output is what the operator asked to see ("вывод консоли надо
#: выводить в чат"), but a command that prints a megabyte must not
#: become a megabyte-long message. The model sees the whole output in
#: its context; the chat gets enough of it to see what happened.
MAX_TRANSCRIPT_OUTPUT_CHARS = 2000

#: The marker that says how many commands the bounded transcript left
#: out. The wording follows `llm_truncation.MIDDLE_DROPPED`: a cut that
#: says nothing about its own size is a cut the reader cannot trust.
COMMANDS_DROPPED = "[... {dropped} of {total} commands are not shown ...]"


def _fit_blocks(blocks, budget):
    """The head and the tail of `blocks` that fit in `budget`.

    Head + tail, not head only: the head carries what the turn started
    with (and the sandbox's own "the beginning no longer fits" marker),
    the tail carries what it ended with -- and the tail is the part the
    model quotes back in its answer. The middle goes, and the number
    that went with it is the marker's job, not this function's.

    The cut is at command granularity, not character granularity: a
    half-printed command in the middle of a transcript reads like a
    broken command, and the reader has to guess what it was.

    Returns `(head, tail, dropped)`.
    """
    if not blocks:
        return [], [], 0
    # The fence and the marker are part of the budget: a cut that spends
    # the whole limit on the text and then adds a marker on top is a cut
    # that does not fit.
    marker = COMMANDS_DROPPED.format(dropped=len(blocks), total=len(blocks))
    overhead = (len("```bash\n") + len(marker) + 1 + len("```"))
    room = budget - overhead
    if room <= 0:
        return [], [], len(blocks)
    total = sum(len(b) + 1 for b in blocks)
    if total <= room:
        return list(blocks), [], 0

    head_room = room // 2
    tail_room = room - head_room

    head = []
    used = 0
    for b in blocks:
        need = len(b) + 1
        if used + need > head_room:
            break
        head.append(b)
        used += need

    tail = []
    used = 0
    # The tail stops where the head stops: the two must not meet, or a
    # command would be shown twice.
    for i in range(len(blocks) - 1, len(head) - 1, -1):
        need = len(blocks[i]) + 1
        if used + need > tail_room:
            break
        tail.insert(0, blocks[i])
        used += need

    dropped = len(blocks) - len(head) - len(tail)
    return head, tail, dropped


def _format_executed_tools(executed_tools, budget=None) -> str:
    """T44: render a fenced ```` ```bash ... ``` ```` block with the
    bash commands the model ran this turn AND what they printed.

    Only ``bash_command`` entries are surfaced — the other tools
    (react_to_message, donsetch web tools) are not user-facing
    commands so they would just be noise in the history.

    The output is here because of the operator's rule (2026-10-24): the
    console output goes into the chat. Before this the chat showed the
    COMMANDS and the user saw the OUTPUT only through the model's
    paraphrase — or, worse, as a `.txt` document, because the model
    pasted the output into a code block and the file feature took it
    out of the chat and delivered it as a file. A transcript without
    its output is half a transcript.

    `budget` bounds the WHOLE block, because the per-command cap alone
    does not: the size of a transcript grows with the number of
    commands the model ran, and a 44-command turn produced 77 044
    characters of console noise in front of a 4 110-character answer
    (measured on production, 2026-10-28). The head and the tail of the
    command list are kept and the middle is dropped, with the drop
    marked. `budget=None` means NO bound — that is the copy the bot
    remembers, and it keeps everything (see `unwrap_answer`).
    `budget=0` means no transcript at all: the chat gets the answer
    alone.

    Returns "" if there is nothing to show.
    """
    if not executed_tools:
        return ""
    if budget is not None and budget <= 0:
        return ""
    bash_cmds = []
    for t in executed_tools:
        if t.get("name") != "bash_command":
            continue
        _args = t.get("args")
        if not isinstance(_args, dict):
            continue
        cmd = _args.get("command", "")
        if cmd:
            bash_cmds.append((cmd, t.get("stdout") or ""))
    if not bash_cmds:
        return ""
    blocks = []
    for cmd, printed in bash_cmds:
        out = printed.rstrip("\n")
        # A new name, not a rebind of the loop variable: the flag-order
        # tripwire reads `x = x.strip()` inside a loop as "the flag is
        # read before it is bound", and it is right to -- the shape
        # that took production down twice.
        if out.strip():
            # The cut is marked, and it keeps both ends. The sandbox
            # prints its "the beginning no longer fits" marker on the
            # first line, and the end is what the model quotes back --
            # so the middle is what goes. The user sees that something
            # was dropped instead of a transcript that looks complete.
            out = condense(out, MAX_TRANSCRIPT_OUTPUT_CHARS)
        block = f"$ {cmd}"
        if out.strip():
            block += "\n" + out
        blocks.append(block)

    head, tail, dropped = blocks, [], 0
    if budget is not None:
        head, tail, dropped = _fit_blocks(blocks, budget)
    lines = ["```bash"]
    lines.extend(head)
    if dropped:
        lines.append(COMMANDS_DROPPED.format(dropped=dropped,
                                              total=len(blocks)))
    lines.extend(tail)
    lines.append("```")
    return "\n".join(lines)



def _delivered_a_file(executed_tools) -> bool:
    """True when a file reached the chat in this turn.

    The `send_file` tool is the only thing that puts a file into the
    chat on its own, and its ledger carries the sha256 of the bytes it
    delivered.  A delivery that failed has no ledger at all, so the
    hash is what says "this one got there" -- and a file that did not
    get there must NOT switch the transcript off: its output is then
    the only place the user can see what the model wrote.

    The files the answer carries (`extract_files`) are a different
    channel and are NOT counted here.  Those come out of the model's
    own text, so the transcript cannot be showing them twice.
    """
    for entry in executed_tools or ():
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != "send_file":
            continue
        if entry.get("sha256"):
            return True
    return False


def unwrap_answer(result) -> Answer:
    """`call_llama`'s return value, as the answer to send.

    `None` survives -- it is what an aborted turn returns, and every
    caller branches on it afterwards. A dict becomes the prose, with
    the executed-tool trace in front of it and the code blocks the
    model wrote pulled out as files.

    A string is a diagnostic and is returned as it is, with no files:
    the reasoning tail it may carry is not a deliverable.
    """
    if not isinstance(result, dict):
        return Answer(result, [], result if isinstance(result, str) else "")
    text = result.get("text") or ""
    executed = result.get("executed_tools") or []
    # What the tools printed. The extractor needs it: a code block that
    # repeats the console output is output, not a deliverable, and it
    # belongs in the chat.
    #
    # The base is what the MODEL saw -- the tool's own result text
    # (`output`), not the narrower piece the ledger keeps for the
    # database (`stdout`). Measured on production, 2026-10-30: the model
    # quoted the middle of a command's output, the ledger held a
    # narrower window than the model had seen, the quote looked like a
    # file, and the user got `answer.txt` with the execution artifacts
    # in it. A comparison against the wrong window cannot see the
    # quote it is supposed to see.
    tool_outputs = [(t.get("output") or t.get("stdout") or "")
                    for t in executed if isinstance(t, dict)]
    prose, files = extract_files(text, tool_outputs=tool_outputs)
    raw = text
    if executed:
        trace = _format_executed_tools(executed)
        if trace:
            raw = trace + "\n\n" + text
            # 2026-10-25: the file is already in the chat, and the
            # transcript would deliver it a second time -- the command
            # that made it carries the whole file in its own text (a
            # heredoc IS the file), so trimming the OUTPUT would not be
            # enough, the command has to go as well.  The chat gets the
            # model's answer; the transcript stays in `raw`, which is
            # what the bot remembers.
            if not _delivered_a_file(executed):
                # The chat's copy is bounded; the remembered one is not.
                # Delivery and memory are two different artefacts: the
                # user must be able to read the answer without scrolling
                # through 22 messages of console noise, and the next turn
                # must still be able to ask what a command printed.
                chat_trace = _format_executed_tools(
                    executed, budget=limits.MAX_TRANSCRIPT_CHARS)
                if chat_trace:
                    prose = chat_trace + "\n\n" + prose
    return Answer(prose, files, raw)
