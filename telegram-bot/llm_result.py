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

Pure: no client, no clock, no await. That is the point of the
extraction and it is what makes it testable on its own.
"""
from __future__ import annotations

from typing import NamedTuple

from llm_artifact import Artifact, extract_files
from llm_truncation import condense

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


def _format_executed_tools(executed_tools) -> str:
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

    Returns "" if there is nothing to show.
    """
    if not executed_tools:
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
    lines = ["```bash"]
    for cmd, printed in bash_cmds:
        lines.append(f"$ {cmd}")
        # A new name, not a rebind of the loop variable: the flag-order
        # tripwire reads `x = x.strip()` inside a loop as "the flag is
        # read before it is bound", and it is right to -- the shape
        # that took production down twice.
        out = printed.rstrip("\n")
        if out.strip():
            # The cut is marked, and it keeps both ends. The sandbox
            # prints its "the beginning no longer fits" marker on the
            # first line, and the end is what the model quotes back --
            # so the middle is what goes. The user sees that something
            # was dropped instead of a transcript that looks complete.
            out = condense(out, MAX_TRANSCRIPT_OUTPUT_CHARS)
            lines.append(out)
    lines.append("```")
    return "\n".join(lines)



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
    tool_outputs = [t.get("stdout") or "" for t in executed
                    if isinstance(t, dict)]
    prose, files = extract_files(text, tool_outputs=tool_outputs)
    raw = text
    if executed:
        trace = _format_executed_tools(executed)
        if trace:
            raw = trace + "\n\n" + text
            prose = trace + "\n\n" + prose
    return Answer(prose, files, raw)
