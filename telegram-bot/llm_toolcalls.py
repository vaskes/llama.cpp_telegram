"""Normalise the streaming tool-call buffer into a dispatchable list.

TECH-DEBT #8, Block D2.

The SSE accumulator fills `tool_calls_buf` as a dict keyed by index,
with one entry per index and fragments merged. This turns it into the
list shape the dispatch loop wants, and applies tool isolation on the
way: a name the bot does not know is rewritten to `__rejected__` and
the iteration is kept alive.

T38.5 and T43. The soft reject exists so a hallucinated or injected
tool name does not reach an executor. Hard-rejecting would kill the
turn and the user would see nothing; the synthetic `__rejected__` entry
is dispatched, hits the unhandled-tool branch, and the model is told
which name was refused and what the allowed set is.

One behaviour changed here deliberately, and it was dead code:

the inline version rewrote `tc["name"] = "__rejected__"` in the
caller's buffer. Nothing reads `tool_calls_buf` after this point --
it is re-created at the top of every iteration and the dispatch loop
works from the returned list -- so the mutation was invisible. It also
meant the function could not be called without silently editing its
argument. The rewrite happens on the output instead.

Pure: no logging, no state, no I/O. The caller logs, so a test can
call this without a log handler.
"""
import json
from typing import NamedTuple

REJECTED_NAME = "__rejected__"


class NormalizedToolCalls(NamedTuple):
    calls: list                  # ready to dispatch
    saw_unavailable_tool: bool   # the caller uses this to phrase the reply
    rejected: list               # the names that were refused, in order

    @property
    def empty(self) -> bool:
        return not self.calls


def normalize_tool_calls(buffered: dict, *, known: frozenset) -> NormalizedToolCalls:
    """buffered -- {index: {id, name, arguments}} -- to a dispatch list.

    Entries with no name are dropped: an empty name means the model
    opened an index and never filled it, which happens when a stream
    is cut mid-call.

    An entry whose name is not in `known` is kept, with its name
    replaced by `__rejected__`. Dropping it instead would leave the
    dispatch loop with a tool call that has no matching tool result,
    and the OpenAI-shaped protocol requires one result per call.
    """
    calls = []
    rejected = []

    for idx, tc in buffered.items():
        name = tc.get("name") or ""
        if not name:
            continue

        if name not in known:
            rejected.append(name)
            name = REJECTED_NAME

        args_str = tc.get("arguments") or ""
        try:
            args_obj = json.loads(args_str) if args_str else {}
        except Exception:
            # A model can produce a valid tool name and broken JSON. The
            # dispatcher's own screen (llm_toolargs) is the place that
            # decides what to do about it and tells the model; here the
            # raw string is carried through unchanged so the diagnostic
            # still points at what the model actually sent.
            args_obj = {}

        calls.append({
            "id": tc.get("id") or f"call_{idx}",
            "type": "function",
            "function": {
                "name": name,
                # `if args_obj` and not `is not None`: an empty parse
                # falls back to the raw string, which is what the model
                # sent. That string is then screened by parse_tool_args,
                # which is where a broken one is reported to the model.
                "arguments": json.dumps(args_obj) if args_obj else args_str,
            },
        })

    return NormalizedToolCalls(calls, bool(rejected), rejected)

def reject_unknown_names(tool_calls: list, *, known: frozenset) -> list:
    """Rewrite unknown names in a list-shaped tool_calls, in place.

    The other site. This one runs on the fallback response -- a JSON
    list, already in the protocol shape -- not on the streaming buffer,
    so it cannot share `normalize_tool_calls`. What it does share is the
    sentinel and the rule, which is why both live here.

    Unlike the streaming site this also blanks the arguments: the call
    is going to be dispatched immediately and a rejected tool must not
    carry a payload into an executor it was never meant to reach.

    Returns the names refused, so the caller can log them.
    """
    rejected = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if name and name not in known:
            fn["name"] = REJECTED_NAME
            fn["arguments"] = "{}"
            rejected.append(name)
    return rejected
