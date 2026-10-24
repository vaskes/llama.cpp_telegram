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

The screen is a bound on the PARSE, not a policy on the call
------------------------------------------------------------------

It used to be both, and the policy half was wrong. The length limit was
1500 characters and the duplicate-key screen counted `":` occurrences,
which reads as "a call with more than eight key-value pairs is
pathological". Both fired on ordinary work:

  * bash_command accepts a command of up to 8192 characters
    (personal_tools/bash_command.py:_BASH_COMMAND_MAX_LEN). The screen
    was stricter than the tool it guarded, so a legal call was refused
    on the way in. On 2026-10-24 that killed a real turn on production:
    the model wrote a 3081-character heredoc to build a zip, the screen
    called it "too repetitive or too long", the loop ended, and the user
    got a canned diagnostic about flight prices instead of a file.
  * the duplicate screen counted the three-character sequence `":` --
    which, in the compact JSON a model emits, is one per string-valued
    parameter. So any call with more than eight string-valued
    parameters was called "repetitive" and refused: a search with nine
    filters is not a broken call, and the count cannot tell a parameter
    from a repetition.

So the two jobs are now separate:

  * `MAX_RAW_ARGS_CHARS` bounds how much text the parser is asked to
    chew on. It is deliberately generous: the raw arguments are the
    ENCODED form, and encoding inflates non-ASCII sixfold (one
    character becomes `\uXXXX`), so a command of 8192 Cyrillic
    characters is 49k of raw text. The tool's own validator checks the
    real limit, on the decoded command; the screen only has to keep the
    parse bounded.
  * the duplicate-key check is exact: the arguments are parsed with a
    pair hook that counts the keys of every object. A key repeated more
    than twice in one object is the MTP pathology; a key repeated once
    is ordinary JSON, and a quotation mark inside a command is not a key
    at all.

tests/test_d1_toolargs.py reads the tool's own limit out of its source
and fails if the screen ever becomes stricter than the tool.
"""
import json
from typing import NamedTuple, Union

#: How far the parser may be asked to read. A parse-safety bound, not a
#: limit on what a tool may be asked to do -- the tool validates that,
#: on the decoded value, where the real limit lives. 64 KB is six times
#: the tool's 8192-character command plus the JSON envelope, which
#: covers the worst case of every character being escaped.
MAX_RAW_ARGS_CHARS = 65536

#: How many times the SAME key may appear in one object before the call
#: is treated as the MTP repetition pathology. Twice is normal JSON
#: (last one wins); three times is the model repeating itself.
MAX_DUPLICATE_KEY_OCCURRENCES = 2

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


class _DuplicateTracker:
    """Counts the keys of every object in the parsed arguments.

    `json.loads` accepts a duplicated key and keeps the last value, so
    the repetition is invisible unless somebody counts. The hook runs
    once per object, so the same key in two DIFFERENT objects is not a
    duplicate -- only a key repeated inside one object is.
    """

    def __init__(self) -> None:
        self.duplicates: list[tuple[str, int]] = []

    def hook(self, pairs):
        counts: dict[str, int] = {}
        for key, _value in pairs:
            counts[key] = counts.get(key, 0) + 1
        for key, count in counts.items():
            if count > MAX_DUPLICATE_KEY_OCCURRENCES:
                self.duplicates.append((key, count))
        return dict(pairs)


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

    # 1. Oversized. Cheap: a length check, before anything parses. This
    #    is the runaway guard -- a model emitting megabytes of arguments
    #    must not be able to make the bot chew on them. It is a bound on
    #    the parse, NOT a limit on the call: the tool checks the real
    #    limit, on the decoded value.
    if len(raw_args) > MAX_RAW_ARGS_CHARS:
        return _rejection(
            "repetitive_or_long",
            f"too long ({len(raw_args)} chars); asking model to retry",
            f'[bot: your tool call for {fn_name} had malformed args '
            f'(length={len(raw_args)}, possibly clipped). '
            f'Please re-emit the tool call with a single, well-formed '
            f'JSON object.]')

    # 2. Unparseable.
    tracker = _DuplicateTracker()
    try:
        args = json.loads(raw_args, object_pairs_hook=tracker.hook)
    except Exception as e:
        return _rejection(
            "unparseable",
            f"unparseable: {e!r} (head={raw_args[:120]!r})",
            f'[bot: your tool call for {fn_name} had invalid JSON ({e!r}). '
            f'Please re-emit with a single well-formed JSON object.]')

    # 3. The same key over and over. Exact, so a command whose text
    #    contains JSON is not mistaken for a broken one.
    if tracker.duplicates:
        listed = ", ".join(f"{key!r} x{count}"
                           for key, count in tracker.duplicates[:3])
        return _rejection(
            "repetitive_or_long",
            f"duplicate keys ({listed}); asking model to retry",
            f'[bot: your tool call for {fn_name} repeated the same '
            f'argument key several times ({listed}). '
            f'Please re-emit the tool call with a single, well-formed '
            f'JSON object.]')

    # 4. Parsed, but to nothing, from a string that was not meant to be
    #    empty. That is structural noise rather than a zero-arg call.
    if not args and raw_args.strip() not in _EMPTY_IS_INTENTIONAL:
        return _rejection(
            "parsed_empty",
            f"parsed to empty for raw={raw_args[:120]!r}",
            f'[bot: your tool call for {fn_name} parsed to an empty object. '
            f'Please re-emit with actual parameters.]')

    # 5. Parsed, but not to an object -- a list, a string, a number.
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
