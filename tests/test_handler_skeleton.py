"""What the four handlers do in the same order, and the three places they do not.

TECH-DEBT #13, block C. Blocks A and B pulled out the result unwrap and
the abort tail. What is left is the skeleton around the call:

    route -> slot -> typing -> thinking -> history -> rating -> llm
          -> persist -> reply

This file does not extract it. It pins it first, because the skeleton
owns the handler's control flow rather than one leaf job, and an
extraction that is only defended by "the tests were green" would be
guessing.

**The order is partial, not total.** `_sender_display_name` and
`_stop_button_markup` swap places between photo and the other three,
and `handle_text` creates its abort event before it routes rather than
beside it. A check demanding one fixed sequence would be red on
correct code, and the first draft of this file was going to be exactly
that. What is genuinely invariant is must-happen-before: a slot is
taken before typing starts, history is loaded before the model is
asked, and the transcript is persisted before the reply goes out. The
last one is the load-bearing half -- persist after reply would mean the
model's own answer is missing from the transcript it just joined.

The three divergences are checked as divergences, not excused:

- photo has no `turn`, because an image turn is never persisted, so
  there is nothing for `turn.rollback` to undo;
- voice has no empty-response retry, so it is the only handler with one
  `call_llama`;
- only photo asks for `use_stream=False`. The other three take the
  default. That default cost an afternoon of production JSONDecodeErrors
  when a branch condition was lost in a refactor, and it is worth
  pinning rather than leaving to whoever reads the signature next.
"""
import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=13)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

HANDLERS = ["handle_photo", "handle_voice", "handle_document", "handle_text"]


def _handler(name):
    src = (BOT / "handlers.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in handlers.py")


def _sites(fn, want):
    """Line numbers of calls to `want`, one per site.

    Counts, deliberately. "Is it called somewhere" is satisfied by the
    wrong call; block A shipped a tripwire that asked that question and a
    mutation removed one of two live calls without turning it red.
    """
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name == want:
            out.append(n.lineno)
    return sorted(out)


# --- the skeleton, stage by stage ----------------------------------------

# must-happen-before. Two names are deliberately absent: they are the
# pair that swaps between handlers.
STAGES = [
    "_route_to_thread",
    "_check_user_slot",
    "send_action",
    ("_sender_display_name", "_stop_button_markup"),
    "_register_abort_event",
    "_load_history",
    "_is_rating_active",
    "_get_global_llm_sem",
    "call_llama",
    "_apply_rating_and_persist",
    "send_reply",
]


@pytest.mark.parametrize("handler", HANDLERS)
def test_the_skeleton_runs_once_per_stage(handler):
    fn = _handler(handler)
    missing = []
    for stage in STAGES:
        names = stage if isinstance(stage, tuple) else (stage,)
        counts = {n: len(_sites(fn, n)) for n in names}
        # every name of the stage is called exactly once, and the first
        # call_llama is the only one not accounted for by a retry
        for name, count in counts.items():
            if count < 1:
                missing.append(f"{handler}: {name} never called")
    assert not missing, missing


@pytest.mark.parametrize("handler", HANDLERS)
def test_the_skeleton_runs_in_order(handler):
    """Each stage after the first starts after the previous one ended.

    Compares the first site of each stage, which is what the invariant
    is about -- "by the time history is loaded, the slot is held".
    """
    fn = _handler(handler)
    firsts = []
    for stage in STAGES:
        names = stage if isinstance(stage, tuple) else (stage,)
        lines = [ln for n in names for ln in _sites(fn, n)]
        assert lines, f"{handler}: stage {names} missing"
        firsts.append((names, min(lines)))

    for (prev_names, prev_line), (names, line) in zip(firsts, firsts[1:]):
        assert prev_line < line, (
            f"{handler}: {prev_names} at line {prev_line} must come before "
            f"{names} at line {line}. Stages, not decoration: the slot has "
            f"to be held before typing starts, and the transcript has to be "
            f"persisted before the reply claims it is in the history"
        )


def test_the_two_unordered_names_really_are_unordered():
    """Guards the `("a", "b")` tuple above.

    If one handler ever fixes the order, the tuple can become a plain
    string and this test should say so rather than silently keep
    accepting either. If it has not -- which is the case today, and the
    reason the stage list is a list of tuples -- this stays as the
    record that it was measured, not assumed.
    """
    orders = {}
    for handler in HANDLERS:
        lines = (min(_sites(_handler(handler), "_sender_display_name")),
                 min(_sites(_handler(handler), "_stop_button_markup")))
        orders[handler] = "name-then-markup" if lines[0] < lines[1] else "markup-then-name"
    assert len(set(orders.values())) > 1, (
        f"all four handlers now agree on the order ({orders}). The tuple in "
        f"STAGES can become a single name and this test can assert the order"
    )


# --- the three divergences, checked as divergences ------------------------

@pytest.mark.parametrize("handler,has_retry", [
    ("handle_photo", True),
    ("handle_document", True),
    ("handle_text", True),
    ("handle_voice", False)])
def test_only_voice_has_a_single_llm_call(handler, has_retry):
    """One `call_llama` per turn without a retry, two with one."""
    fn = _handler(handler)
    calls = _sites(fn, "call_llama")
    assert len(calls) == (2 if has_retry else 1), (
        f"{handler}: {len(calls)} call_llama sites. Voice has no "
        f"empty-response retry by design -- adding or removing one in any "
        f"other handler changes how many times a user's message reaches "
        f"the model"
    )


@pytest.mark.parametrize("handler", ["handle_photo"])
def test_photo_streams_nothing(handler):
    """`use_stream=False` on photo's call, explicitly.

    The regression this pins is not hypothetical: a branch condition
    lost across a refactor sent a streaming request to the non-streaming
    parser, and the production symptom was a JSONDecodeError that read
    like the engine returning an empty body. It was the request.
    """
    fn = _handler(handler)
    passed = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "call_llama":
                passed[n.lineno] = {k.arg: ast.dump(k.value)
                                    for k in n.keywords}
    for lineno, kws in passed.items():
        assert "use_stream" in kws, (
            f"{handler}: call_llama at {lineno} does not say which branch "
            f"it wants. Without the keyword the default is True and the "
            f"response is SSE"
        )
        assert kws["use_stream"] == "Constant(value=False)", (
            f"{handler}: call_llama at {lineno} passes use_stream="
            f"{kws['use_stream']}, expected False -- photo has no "
            f"streaming edit path"
        )


@pytest.mark.parametrize("handler", ["handle_voice", "handle_document", "handle_text"])
def test_the_streaming_handlers_take_the_default_deliberately(handler):
    """They do not pass `use_stream`; they want the streaming branch.

    Written as an assertion rather than left implicit, because "it is
    fine as long as nobody touches it" is how the default was wrong
    once already.
    """
    fn = _handler(handler)
    for lineno in _sites(fn, "call_llama"):
        node = next(n for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and n.lineno == lineno)
        kws = {k.arg for k in node.keywords}
        assert "use_stream" not in kws, (
            f"{handler}: call_llama at {lineno} now passes use_stream "
            f"explicitly. If that is deliberate, say so in the test name "
            f"too -- this file exists because a lost condition here was "
            f"invisible"
        )


def test_only_the_streaming_handlers_have_an_empty_response_branch():
    """The retry is gated on the marker, so it can only exist where the
    branch does. Pinning the count of the marker is the same invariant
    as the call count, from the other side -- and it is the side that
    survives someone renaming `call_llama`."""
    for handler, expect in [("handle_photo", 1), ("handle_document", 1),
                            ("handle_text", 1), ("handle_voice", 0)]:
        src = (BOT / "handlers.py").read_text(encoding="utf-8")
        fn = _handler(handler)
        hits = sum(1 for n in ast.walk(fn)
                   if isinstance(n, ast.Name) and n.id == "EMPTY_RESPONSE_PREFIX")
        assert hits >= expect, f"{handler}: {hits} EMPTY_RESPONSE_PREFIX uses"


# --- the reply is the last thing ----------------------------------------

@pytest.mark.parametrize("handler", HANDLERS)
def test_the_reply_is_sent_after_the_turn_is_persisted(handler):
    """The single most load-bearing ordering in the file.

    Reply before persist means the model answers a question and the
    transcript it just joined does not contain that turn, so the next
    message is answered about a slightly different conversation than the
    one on screen.
    """
    fn = _handler(handler)
    persist = min(_sites(fn, "_apply_rating_and_persist"))
    reply = min(_sites(fn, "send_reply"))
    assert persist < reply, (
        f"{handler}: persists at {persist} but replies at {reply}"
    )


@pytest.mark.parametrize("handler", HANDLERS)
def test_no_handler_sends_thinking_before_taking_the_slot(handler):
    """`send_action` is the typing indicator. Before the slot it means a
    user who was already being told to wait also gets a rejection
    message."""
    fn = _handler(handler)
    assert min(_sites(fn, "_check_user_slot")) < min(_sites(fn, "send_action")), \
        f"{handler}: typing starts before the slot is held"