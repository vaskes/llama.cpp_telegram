r"""Defensive parsing of LLM tool-call arguments.

TECH-DEBT #3, Block D1. Extracted from inside the 654-line tool loop.

Recent models with speculative decoding / MTP heads -- Qwen3.8-27B among
them -- emit malformed tool-call JSON during long chains: the same key
repeated many times, or the closing brace clipped. `json.loads` then
either accepts only the last value of a duplicated key, or raises and
the call falls back to `{}`, which errors at the tool side. Both paths
waste a turn, and the second one lets the loop reach a 500 from
llama-server.

Three rejection shapes, all of which used to be inline `continue` sites
that each did four things: log, append a tool result, bump the empty-call
budget, continue. That shape is why the budget coupling was invisible --
`guard.note_rejected_call()` appeared three times with three different
messages, and a reader could not see that they were one policy.

The log line keeps its greppable prefix `[tool] iter=N REJECTED` exactly;
only the varying tail moved into `detail`. Runbooks match on the prefix.
"""
import json
from typing import NamedTuple, Union

# Cheap pre-parse screens. Deliberately not exact JSON analysis: counting
# duplicate top-level keys by parsing is more expensive than the parse we
# are trying to avoid, and the screen only has to catch the pathological
# cases, not all of them.
MAX_DUPLICATE_KEY_HINTS = 8
MAX_RAW_ARGS_CHARS = 1500

# Parsing to one of these from a non-empty string is a real "no arguments"
# call, not the model failing to produce JSON. Everything else that lands
# on an empty result is structural noise: "{", "null" with no content, a
# duplicated key that parsed away, and so on.
_EMPTY_IS_INTENTIONAL = ('{}', 'null', '[]')


class ArgsOk(NamedTuple):
    """The arguments parsed, or an intentional empty call."""

    args: object


class ArgsRejected(NamedTuple):
    """Why the call cannot be dispatched, and what to tell the model.

    `detail` is the varying tail of the log line. `message` is the exact
    text sent back as the tool result -- the model reads this and
    retries, so its wording is behaviour, not cosmetics.
    """

    reason: str          # repetitive_or_long | unparseable | parsed_empty
    detail: str          # log tail
    message: str         # tool-result content


Parsed = Union[ArgsOk, ArgsRejected]


def _rejection(reason, detail, message) -> ArgsRejected:
    return ArgsRejected(reason, detail, message)


def parse_tool_args(fn_name: str, raw_args) -> Parsed:
    """Screen and parse one tool call's arguments.

    Never raises for malformed input: an unparseable string is a
    rejection, not an exception. The caller appends `message` as the
    tool result and asks the model to retry, which costs one iteration
    prevents the runaway loop.

    `ArgsOk.args` is always a dict on return. JSON that parses to a list
    or a scalar is normalised to `{}` rather than rejected -- see step 4.
    Handlers may rely on that.
    """
    if not isinstance(raw_args, str) or not raw_args:
        # Absent or non-string arguments are an empty call, not a
        # failure. A model that emits {"name": "x"} with no arguments is
        # asking for a zero-argument tool.
        return ArgsOk({})

    # 1. Repetitive or oversized. Cheap: no parsing, just counting and
    #    length. Catches '"max_chars":8000,"max_chars":8000,...' and a
    #    runaway string, both of which are symptoms of the same fault.
    if (raw_args.count('":') > MAX_DUPLICATE_KEY_HINTS
            or len(raw_args) > MAX_RAW_ARGS_CHARS):
        return _rejection(
            "repetitive_or_long",
            f"too repetitive or too long ({len(raw_args)} chars); "
            f"asking model to retry",
            f'[bot: your tool call for {fn_name} had malformed args '
            f'(length={len(raw_args)}, possibly duplicate keys or clipped). '
            f'Please re-emit the tool call with a single, well-formed '
            f'JSON object.]')

    # 2. Unparseable.
    try:
        args = json.loads(raw_args)
    except Exception as e:
        return _rejection(
            "unparseable",
            f"unparseable: {e!r} (head={raw_args[:120]!r})",
            f'[bot: your tool call for {fn_name} had invalid JSON ({e!r}). '
            f'Please re-emit with a single well-formed JSON object.]')

    # 3. Parsed, but to nothing, from a string that was not meant to be
    #    empty. That is structural noise rather than a zero-arg call.
    if not args and raw_args.strip() not in _EMPTY_IS_INTENTIONAL:
        return _rejection(
            "parsed_empty",
            f"parsed to empty for raw={raw_args[:120]!r}",
            f'[bot: your tool call for {fn_name} parsed to an empty object. '
            f'Please re-emit with actual parameters.]')

    # 4. Parsed, but not to an object -- a list, a string, a number.
    #    Every tool declares an object schema, so nothing can use one,
    #    and bash_command used to raise AttributeError on it: the turn
    #    died on a model mistake.
    #
    #    Normalised rather than rejected. Rejecting was the first
    #    attempt, and tests/test_d1_toolargs.py says why that is wrong
    #    for the empty shapes: the model re-emits the same thing and the
    #    loop never terminates. An empty object is what a zero-argument
    #    tool means anyway, so that is what it gets.
    #
    #    Placed after every other check on purpose. Normalising earlier
    #    turns a truthy non-dict falsy, and the parsed-empty check then
    #    rejects it -- which moves the accept/reject boundary, and that
    #    boundary is D1's decision, not this one's.
    if not isinstance(args, dict):
        args = {}

    return ArgsOk(args)