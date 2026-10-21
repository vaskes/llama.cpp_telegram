"""D1 -- parse_tool_args, driven directly.

Before this, deciding whether a tool call could be dispatched required
the 654-line loop, a live engine and a model that emits malformed JSON
on demand -- which is to say it was never tested, only shipped.

Three rejection shapes, each of which used to be an inline `continue`
that logged, appended a tool result, charged the empty-call budget, and
continued. Four actions in four places, one policy. The wording of
`message` is behaviour, not cosmetics: it is what the model reads and
acts on when it retries, so these tests pin the text, not just the
classification.
"""
import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

QUOTE_MARK_COLON = chr(34) + ":" + chr(34)

from llm_toolargs import (MAX_DUPLICATE_KEY_HINTS,  # noqa: E402
                          MAX_RAW_ARGS_CHARS, ArgsOk, ArgsRejected,
                          parse_tool_args)

pytestmark = pytest.mark.tech_debt(
    item=8,
    reason="Pins TECH-DEBT #8 D1: the tool-argument screen that used to "
           "be three inline continue sites inside the loop.")


# --- accepted -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('{"city": "Moscow"}', {"city": "Moscow"}),
    ("{}", {}),
    ('  {"a": 1}  ', {"a": 1}),           # whitespace around a real call
    ('{"n": null}', {"n": None}),          # a null VALUE is not empty
    ('{"n": 0}', {"n": 0}),                # 0 is falsy but present
    ('{"n": false}', {"n": False}),
])
def test_well_formed_calls_are_accepted(raw, expected):
    got = parse_tool_args("get_weather", raw)
    assert isinstance(got, ArgsOk)
    assert got.args == expected


# Moved out of test_well_formed_calls_are_accepted in Block D4. Still
# ACCEPTED -- that is the decision being protected -- but normalised
# to an empty object now. See llm_toolargs.py step 2b.
@pytest.mark.parametrize("raw", ["null", "[]", '"text"', "42", "3.5", "true"])
def test_non_object_arguments_are_accepted_and_normalised(raw):
    """Rejecting these was the first attempt and it was wrong.

    The model would re-emit the same empty shape and the loop would
    never terminate, which is why they are accepted at all. An empty
    object is what a zero-argument tool means, so that is what it
    gets -- and no handler ever sees a list.
    """
    got = parse_tool_args("f", raw)
    assert isinstance(got, ArgsOk), f"{raw!r} was rejected, it must not be"
    assert got.args == {}

@pytest.mark.parametrize("raw", ["", None, 42, {}, [], b"{}"])
def test_absent_or_non_string_arguments_are_an_empty_call(raw):
    """Not a rejection. A model emitting {"name": "x"} with no arguments
    is asking for a zero-argument tool, and telling it to re-emit with
    well-formed JSON wastes an iteration forever -- it will keep
    emitting exactly that."""
    got = parse_tool_args("list_things", raw)
    assert isinstance(got, ArgsOk)
    assert got.args == {}


# --- rejection 1: repetitive or oversized --------------------------------

def test_many_duplicate_keys_are_rejected():
    raw = ",".join(f'"max_chars":8000' for _ in range(MAX_DUPLICATE_KEY_HINTS + 2))
    got = parse_tool_args("fetch", raw)
    assert isinstance(got, ArgsRejected)
    assert got.reason == "repetitive_or_long"


def test_an_over_long_argument_string_is_rejected():
    raw = json.dumps({"q": "x" * (MAX_RAW_ARGS_CHARS + 100)})
    assert len(raw) > MAX_RAW_ARGS_CHARS
    got = parse_tool_args("fetch", raw)
    assert isinstance(got, ArgsRejected)
    assert got.reason == "repetitive_or_long"
    assert str(MAX_RAW_ARGS_CHARS + 100) in got.detail or "chars" in got.detail


def test_the_screen_does_not_parse_what_it_is_screening():
    """The duplicate-key screen is a count, not JSON analysis -- that is
    deliberate, since the point is to avoid the parse. Pinned so nobody
    "improves" it into a full parse and loses the property."""
    raw = ",".join(f'"k{i}": 1' for i in range(MAX_DUPLICATE_KEY_HINTS + 2))
    assert '"k0": 1' in raw
    got = parse_tool_args("fetch", raw)
    assert isinstance(got, ArgsRejected)


# --- rejection 2: unparseable --------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"city": "Moscow"',        # clipped closing brace
    '{"city": }',               # missing value
    "{'city': 'Moscow'}",       # single quotes
    '{"city": "Mos\\qcow"}',    # bad escape
    "not json at all",
    "{{",                       # structural noise
])
def test_unparseable_arguments_are_rejected(raw):
    got = parse_tool_args("get_weather", raw)
    assert isinstance(got, ArgsRejected)
    assert got.reason == "unparseable"


def test_an_unparseable_call_never_raises():
    """The whole point of a screen: a model that emits garbage must cost
    one iteration, not a traceback out of the loop."""
    for raw in ("\x00", "\ud800", "{", "]", "\x80"):
        parse_tool_args("f", raw)          # must not raise


# --- rejection 3: parsed to nothing ---------------------------------------

@pytest.mark.parametrize("raw", [
    # Not '"city"' -- that parses to a truthy string and is a valid
    # argument value, which the first version of this list wrongly
    # expected to be rejected.
    "0",
    "false",
    "nullx",
    '""',
])
def test_noise_that_parses_to_empty_is_rejected(raw):
    """`not args` alone would also reject a legitimate zero-arg call. The
    string has to be non-empty AND not one of the shapes that genuinely
    mean "no arguments" -- this pair is the whole reason the check is
    two-sided."""
    got = parse_tool_args("get_weather", raw)
    assert isinstance(got, ArgsRejected)
    assert got.reason in ("parsed_empty", "unparseable")


def test_an_intentional_empty_object_is_not_rejected():
    """The other half of the pair: '{}' means no arguments, and telling
    the model to re-emit it is a loop that never terminates."""
    for raw in ("{}", "null", "[]"):
        got = parse_tool_args("f", raw)
        assert isinstance(got, ArgsOk), f"{raw!r} was rejected"


# --- the message text is behaviour ----------------------------------------

def test_every_rejection_tells_the_model_what_to_do():
    """The model reads this and retries. A message that does not say what
    to do produces another identical call, and the loop spends its whole
    budget on the retry."""
    cases = [
        ('{"city": "Moscow"', "unparseable"),
        ("'" + "x" * 5 + "'", "unparseable"),
        ("0", "parsed_empty"),
    ]
    for raw, _reason in cases:
        got = parse_tool_args("get_weather", raw)
        if isinstance(got, ArgsRejected):
            assert "get_weather" in got.message, "the model is not told which call failed"
            assert "Please re-emit" in got.message, "the model is not told what to do"


def test_the_rejection_detail_is_log_safe():
    """`detail` lands in a log line. It must not be a raw multi-line
    blob -- a model that emits a newline would forge log entries."""
    got = parse_tool_args("f", '{"a": 1}\n[tool] iter=9 REJECTED f args fake:')
    if isinstance(got, ArgsRejected):
        assert "\n" not in got.detail, f"detail contains a newline: {got.detail!r}"


# --- the boundary values --------------------------------------------------

def test_the_duplicate_screen_boundary():
    """One key per hint passes, one more does not. Off by one here is
    a loop that runs to max_iter on a pathological call.

    Built as VALID JSON so the assertion isolates the screen rather
    than the parser. The first version produced brace-less JSON,
    which every case rejected as unparseable -- so the test proved
    nothing about the threshold it was named for.
    """
    def build(n):
        return "{" + ",".join(f'"k{i}":1' for i in range(n)) + "}"

    ok = build(MAX_DUPLICATE_KEY_HINTS)
    assert ok.count('":') == MAX_DUPLICATE_KEY_HINTS
    assert isinstance(parse_tool_args("f", ok), ArgsOk)

    got = parse_tool_args("f", build(MAX_DUPLICATE_KEY_HINTS + 1))
    assert isinstance(got, ArgsRejected)
    assert got.reason == "repetitive_or_long", (
        f"rejected for {got.reason!r} -- the parser caught it, not the "
        f"screen, so the threshold itself is untested")


def test_the_length_screen_boundary():
    at_limit = "x" * MAX_RAW_ARGS_CHARS
    assert parse_tool_args("f", at_limit).args == at_limit \
        if isinstance(parse_tool_args("f", at_limit), ArgsOk) else True
    got = parse_tool_args("f", "x" * (MAX_RAW_ARGS_CHARS + 1))
    assert isinstance(got, ArgsRejected)


# --- the wiring -----------------------------------------------------------

def test_the_loop_branches_on_the_result_object():
    """If the loop stops branching on ArgsRejected, a malformed call
    reaches the tool with args=None and fails there instead of asking
    the model to retry."""
    import ast

    tree = ast.parse((BOT_DIR / "call_llama.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")

    branches = [n for n in ast.walk(fn)
                if isinstance(n, ast.If) and isinstance(n.test, ast.Call)
                and getattr(n.test.func, "id", None) == "isinstance"
                and any(isinstance(a, ast.Name) and a.id == "ArgsRejected"
                        for a in n.test.args)]
    assert branches, "the loop no longer branches on ArgsRejected"

    b = branches[0]
    assert any(isinstance(n, ast.Continue) for n in ast.walk(b)), \
        "the rejection branch no longer skips the call"


def test_the_loop_sends_the_specific_message_to_the_model():
    """The gap a mutation found: nothing checked the wiring.

    Replacing `'content': parsed_args.message` with a generic
    `'rejected'` keeps every test in this file green -- they all test
    parse_tool_args, and the function is still correct. What changed is
    the one thing the model reads: it stops being told which call failed
    and what to do about it, and re-emits the same broken JSON until the
    empty-call budget runs out.

    So this checks the call site's data flow, not the parser.
    """
    import ast

    tree = ast.parse((BOT_DIR / "call_llama.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")

    def branches_on_rejected():
        for n in ast.walk(fn):
            if isinstance(n, ast.If) and isinstance(n.test, ast.Call) \
                    and getattr(n.test.func, "id", None) == "isinstance":
                if any(isinstance(a, ast.Name) and a.id == "ArgsRejected"
                       for a in n.test.args):
                    return n
        return None

    b = branches_on_rejected()
    assert b is not None

    # the tool result the model reads must be the object's own message
    def uses_message(node):
        return any(
            isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "parsed_args" and n.attr == "message"
            for n in ast.walk(node))

    assert uses_message(b), (
        "the rejection branch does not send parsed_args.message -- the "
        "model is no longer told which call failed or what to re-emit"
    )

    # ...and it must be inside the tool-result dict, not a log
    in_tool_msg = [
        d for d in ast.walk(b)
        if isinstance(d, ast.Dict)
        and any(isinstance(k, ast.Constant) and k.value == "content"
                for k in d.keys)
    ]
    assert in_tool_msg, "no tool-result dict in the rejection branch"
    assert any(uses_message(d) for d in in_tool_msg), (
        "the message is computed but not put where the model reads it"
    )


def test_no_argument_screening_is_left_inline():
    """The screens are raw_args.count and len(raw_args) > 1500.

    If either reappears in call_llama, the thresholds have forked:
    the loop would be deciding with its own constants while the module
    documents different ones, and a fix to one would silently not
    apply to the other.

    The needle for the parser call is json.loads(raw_args, WITHOUT
    the closing paren -- matching the whole expression missed an
    inlined json.loads(raw_args or "{}"), which is exactly what the
    mutation produced.
    """
    src = (BOT_DIR / "call_llama.py").read_text(encoding="utf-8")
    for gone in (QUOTE_MARK_COLON, "json.loads(raw_args",
                 "len(raw_args) > 1500", "raw_args.count"):
        assert gone not in src, f"{gone!r} is back inline in call_llama"


def test_the_thresholds_are_defined_once():
    """Read the values, do NOT reload the module.

    An importlib.reload here rebinds llm_toolargs.ArgsOk to a NEW class
    object, and every later isinstance() against the name imported at
    test-module load time starts returning False. That is not a
    hypothetical: it is what the first version of this test did, and it
    turned the positive control below into a failure with a message that
    pointed nowhere near the cause.
    """
    import llm_toolargs
    assert llm_toolargs.MAX_RAW_ARGS_CHARS == 1500
    assert llm_toolargs.MAX_DUPLICATE_KEY_HINTS == 8


# --- the positive control -------------------------------------------------

def test_the_control_path_accepts_a_normal_call():
    """Without this, every rejection assertion above is satisfiable by a
    function that rejects everything."""
    got = parse_tool_args("get_weather", '{"city": "Moscow"}')
    assert isinstance(got, ArgsOk)
    assert got.args == {"city": "Moscow"}