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

`unwrap_llm_result` is the one form. The primary and retry sites were
not two shapes of the same rule; they were one rule written twice, and
the retry copy's missing `else` was the only difference -- a non-dict
`retry_response` left the name alone, which is exactly what returning
the value unchanged does.

`_format_executed_tools` moved here with it rather than being passed in.
It is the other half of the same decision: given the executed tools,
render the block. Keeping it in `handlers.py` would have made this
module a leaf that imports the module it was extracted from, which is
the import cycle TECH-DEBT #2 was closed to remove.
"""
from __future__ import annotations

__all__ = ["unwrap_llm_result"]


def _format_executed_tools(executed_tools) -> str:
    """T44: render a fenced ```` ```bash ... ``` ```` block listing
    the bash commands the model actually ran this turn.

    Only ``bash_command`` entries are surfaced — the other tools
    (react_to_message, donsetch web tools) are not user-facing
    commands so they would just be noise in the history.

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
            bash_cmds.append(cmd)
    if not bash_cmds:
        return ""
    lines = ["```bash"]
    for cmd in bash_cmds:
        lines.append(f"$ {cmd}")
    lines.append("```")
    return "\n".join(lines)



def unwrap_llm_result(result):
    """`call_llama`'s return value, as the text to answer with.

    `None` survives -- it is what an aborted turn returns, and every
    caller branches on it afterwards. A dict becomes `text`, prefixed
    with the executed-tool trace when there is one to show.

    Pure: no client, no clock, no await. That is the point of the
    extraction and it is what makes it testable on its own.
    """
    if not isinstance(result, dict):
        return result
    text = result.get("text") or ""
    executed = result.get("executed_tools") or []
    if executed:
        trace = _format_executed_tools(executed)
        if trace:
            text = trace + "\n\n" + text
    return text
