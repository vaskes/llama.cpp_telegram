"""C3 -- LoopGuard, driven directly.

Before this extraction, checking that a stuck model gets stopped meant an
engine, a fake httpx response, a tool that returns "[unavailable]" three
times and an event loop. Now it is an object with a tick().

What these pin is not "the guards exist". The characterization test in
test_call_llama_characterization.py already does that, by looking for
strings. These pin the behaviour those strings are supposed to describe,
which is the part that was only ever checked by reading.

The two interactions worth writing down:

  * prev_calls advances AFTER the identical check and BEFORE the
    wall-clock check. Reverse either and a repeated call is reported on
    a different iteration than it happens.

  * last_empty is zeroed whenever an iteration produced a real result,
    which overwrites the rejections counted earlier in that same
    iteration. That looks like a bug and is not: it is the behaviour
    call_llama has always had, and `test_a_real_result_wipes_rejections_
    counted_this_iteration` exists so nobody "fixes" it silently.
"""
import ast
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

from llm_loop_guard import Abort, LoopGuard, Nudge  # noqa: E402

pytestmark = pytest.mark.tech_debt(
    item=3,
    reason="Pins TECH-DEBT #3 C3: the loop guards fire in exactly the "
           "situations they fired in before the extraction.")


def tick(guard, *, iteration=0, current_calls=(), elapsed=0.0,
         real_result=True, final_fallback=None, reasoning="", finish="stop"):
    return guard.tick(
        iteration=iteration,
        current_calls=list(current_calls),
        elapsed=elapsed,
        iter_had_real_result=real_result,
        final_fallback=final_fallback,
        reasoning_tail=reasoning,
        finish_reason=finish,
    )


# --- the quiet path -------------------------------------------------------

def test_a_healthy_iteration_returns_nothing():
    assert tick(LoopGuard(600)) is None


def test_a_first_iteration_with_calls_is_not_a_loop():
    """prev_calls starts empty; a first call must not read as a repeat.

    T32c. When every tool call in an iteration was rejected,
    current_calls stayed [], which equals the initial prev_calls -- a
    spurious "stuck" report on iteration 1, and an IndexError on
    current_calls[0] once it did report.
    """
    g = LoopGuard(600)
    assert tick(g, current_calls=[("get_weather", '{"city":"Moscow"}')]) is None


# --- identical-call -------------------------------------------------------

def test_the_same_call_twice_in_a_row_aborts():
    g = LoopGuard(600)
    call = [("get_weather", '{"city":"Moscow"}')]
    assert tick(g, iteration=0, current_calls=call) is None
    out = tick(g, iteration=1, current_calls=call)
    assert isinstance(out, Abort)
    assert "stuck in a loop" in out.text
    assert "get_weather" in out.text, "the message names the tool that looped"


def test_a_different_call_after_a_repeat_does_not_abort():
    g = LoopGuard(600)
    tick(g, current_calls=[("a", "{}")])
    assert tick(g, current_calls=[("b", "{}")]) is None


def test_a_changed_argument_is_a_different_call():
    """The comparison is (name, args) -- a different city is progress, and
    stopping on it would cut off a working search."""
    g = LoopGuard(600)
    tick(g, current_calls=[("get_weather", '{"city":"Moscow"}')])
    assert tick(g, current_calls=[("get_weather", '{"city":"Paris"}')]) is None


def test_an_empty_call_list_never_aborts_on_repetition():
    """The T32c guard itself, pinned. current_calls=[] twice in a row is
    an iteration whose tools were all rejected, not a stuck model."""
    g = LoopGuard(600)
    tick(g, current_calls=[], real_result=False)
    second = tick(g, current_calls=[], real_result=False)
    assert not isinstance(second, Abort), (
        "an empty call list is an iteration whose tools were all "
        "rejected, not a stuck model. It may still nudge -- that is the "
        "empty-result budget doing its own job."
    )


def test_an_abort_names_the_final_answer_so_far():
    g = LoopGuard(600)
    call = [("get_weather", "{}")]
    tick(g, current_calls=call)
    out = tick(g, current_calls=call, final_fallback="Вот что нашёл: 15°")
    assert "Вот что нашёл: 15°" in out.text


def test_an_abort_with_no_fallback_says_so():
    g = LoopGuard(600)
    call = [("x", "{}")]
    tick(g, current_calls=call)
    assert "(none)" in tick(g, current_calls=call).text


# --- wall-clock ----------------------------------------------------------

def test_the_budget_stops_a_loop_whose_calls_all_differ():
    """The reason this guard exists. A model that searches a different
    city every iteration defeats identical-call detection completely and
    burns the budget instead."""
    g = LoopGuard(600)
    for i in range(40):
        out = tick(g, iteration=i, current_calls=[("s", str(i))], elapsed=1.0 * i)
        if i * 1.0 <= 600:
            assert out is None, f"aborted early at iteration {i}"
    out = tick(g, current_calls=[("s", "40")], elapsed=601.0)
    assert isinstance(out, Abort)
    assert "time budget exceeded" in out.text


def test_the_budget_boundary_is_strictly_greater_than():
    """elapsed == budget must NOT abort. Off-by-one here costs a whole
    iteration of work on every single long call."""
    g = LoopGuard(600)
    assert tick(g, current_calls=[("a", "{}")], elapsed=600.0) is None


def test_a_budget_of_zero_aborts_immediately():
    g = LoopGuard(0)
    assert isinstance(tick(g, elapsed=0.001), Abort)


# --- the empty-result budget ---------------------------------------------

def test_two_empty_iterations_nudge_before_they_abort():
    """The order is deliberate: warn once, then stop. Aborting at 2 would
    leave the model no chance to answer with what it already has."""
    g = LoopGuard(600)
    assert tick(g, iteration=0, real_result=False) is None
    out = tick(g, iteration=1, real_result=False)
    assert isinstance(out, Nudge), "the second empty iteration must nudge, not abort"
    assert "final answer now" in out.text
    out = tick(g, iteration=2, real_result=False)
    assert isinstance(out, Abort)
    assert "3 итерации подряд" in out.text


def test_the_abort_carries_the_reasoning_tail():
    g = LoopGuard(600)
    g.note_rejected_call()
    g.note_rejected_call()
    g.note_rejected_call()
    out = tick(g, real_result=False, reasoning="Я проверил погоду.", finish="length")
    assert isinstance(out, Abort)
    assert "Я проверил погоду." in out.text
    assert "finish_reason=length" in out.text


def test_a_real_result_resets_the_empty_streak():
    g = LoopGuard(600)
    tick(g, real_result=False)
    assert tick(g, real_result=False) is not None      # nudged
    assert tick(g, current_calls=[("a", "{}")], real_result=True) is None
    assert g.last_empty == 0
    assert tick(g, iteration=9, real_result=False) is None, \
        "the streak must have restarted"


# --- rejected tool calls, from the other end of the loop -----------------

def test_three_rejected_calls_in_one_iteration_stop_the_loop():
    """The counter is shared between malformed tool calls and empty
    results, which is how a model that emits nothing but broken JSON gets
    stopped at all."""
    g = LoopGuard(600)
    for _ in range(3):
        g.note_rejected_call()
    out = tick(g, real_result=True)
    assert g.last_empty == 0, (
        "a real result zeroes the counter -- which is why three "
        "rejections in one iteration do NOT trip it on that iteration"
    )


def test_rejections_accumulate_across_iterations_without_a_real_result():
    g = LoopGuard(600)
    g.note_rejected_call()
    g.note_rejected_call()
    out = tick(g, real_result=False)
    assert isinstance(out, Abort), (
        "two rejections plus an empty iteration is three -- this is the "
        "path that catches a model stuck in malformed tool calls"
    )


def test_rejections_are_cleared_by_the_next_real_iteration():
    """Behaviour, not preference. Do not 'fix' this into a per-iteration
    reset without changing every test above."""
    g = LoopGuard(600)
    g.note_rejected_call()
    g.note_rejected_call()
    tick(g, current_calls=[("ok", "{}")], real_result=True)
    assert g.last_empty == 0
    assert tick(g, iteration=1, real_result=False) is None


# --- state does not leak between turns ----------------------------------

def test_a_second_turn_starts_with_no_history():
    """call_llama builds a new LoopGuard per call. Reusing one across
    turns would abort turn 2 on turn 1's last tool call."""
    call = [("get_weather", "{}")]

    g2 = LoopGuard(600)
    assert g2.prev_calls == [] and g2.last_empty == 0

    g1 = LoopGuard(600)
    tick(g1, current_calls=call)

    assert tick(g2, current_calls=call) is None, (
        "a fresh guard must treat turn 1's last call as new"
    )
    assert g2.prev_calls == call


# --- the positive control ------------------------------------------------

def test_the_control_path_actually_returns_an_abort():
    """Without this, every assertion above is satisfiable by a tick()
    that returns None forever."""
    out = tick(LoopGuard(600), elapsed=9999.0)
    assert isinstance(out, Abort), "the control path is not reachable"


def test_nudge_and_abort_are_distinguishable_types():
    """The caller branches on isinstance, so collapsing them into one
    string would silently turn every nudge into an abort."""
    g = LoopGuard(600)
    tick(g, real_result=False)
    nudge = tick(g, real_result=False)
    assert isinstance(nudge, Nudge) and not isinstance(nudge, Abort)
    assert isinstance(tick(g, real_result=False), Abort)


def _call_llama_node():
    tree = ast.parse((BOT_DIR / "call_llama.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")


def branch_on(cls):
    """Find `if isinstance(outcome, cls):` inside call_llama.

    Written as a predicate over the tree, not as a substring of
    ast.dump. ast.dump renders it

        Isinstance(Name(id='outcome', ctx=Load()),
                   Name(id='Nudge', ctx=Load()))

    which does not contain the source text "isinstance(outcome,
    Nudge)" -- the same trap as searching for `turn.rollback` in a
    dump and getting an empty list forever.
    """
    for n in ast.walk(_call_llama_node()):
        if not (isinstance(n, ast.If) and isinstance(n.test, ast.Call)):
            continue
        call = n.test
        f = call.func
        fname = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else None)
        if fname != "isinstance":
            continue
        names = [a.id for a in call.args if isinstance(a, ast.Name)]
        if names[:2] == ["outcome", cls]:
            return n
    return None


# --- the extraction itself -----------------------------------------------

def test_call_llama_no_longer_owns_the_loop_state():
    """prev_calls and last_empty must exist in exactly one place. Two
    counters means the tail reads one and the rejections feed the other,
    and the guard quietly stops seeing malformed tool calls."""
    src = (BOT_DIR / "call_llama.py").read_text(encoding="utf-8")
    for stale in ("last_empty", "prev_calls"):
        code = [l for l in src.split("\n")
                if stale in l and not l.strip().startswith("#")]
        assert not code, f"{stale} still assigned in call_llama: {code}"


def test_every_rejection_site_goes_through_the_guard():
    """Three `guard.note_rejected_call()` calls, no bare increments. A
    bare `last_empty += 1` left behind would create a local nothing
    reads -- the exact failure this block already had once."""
    src = (BOT_DIR / "call_llama.py").read_text(encoding="utf-8")
    assert src.count("guard.note_rejected_call()") == 3, (
        f"expected 3 rejection sites, found "
        f"{src.count('guard.note_rejected_call()')}"
    )
    assert "last_empty += 1" not in src


def test_the_nudge_still_reaches_the_summarize_call():
    """The summarize call was nested inside `if last_empty >= 2:`. Moving
    the guard changed its parent, and the summarize block lost an indent
    level with it -- the first version of this cut orphaned the
    `async with` and the module stopped parsing.

    Both halves have to be under the same condition or the bot either
    skips the summarize or runs it unconditionally.
    """
    fn = _call_llama_node()

    def walks_to(node, pred):
        return any(pred(n) for n in ast.walk(node))

    branch = branch_on("Nudge")
    assert branch is not None, "the Nudge branch is gone"

    # the summarize POST must be inside it
    assert walks_to(branch, lambda n: isinstance(n, ast.AsyncWith)), (
        "the summarize call is no longer under the Nudge branch"
    )
    assert walks_to(branch, lambda n: isinstance(n, ast.Return)), \
        "the Nudge branch no longer returns the summarize result"


def test_an_abort_returns_before_the_summarize_call():
    """An abort must leave the loop at once. If the Abort branch ever
    falls through to the summarize call, a stuck model gets one extra
    paid request before it is stopped."""
    fn = _call_llama_node()

    abort = branch_on("Abort")
    assert abort is not None, "the Abort branch is gone"

    body = abort.body
    assert isinstance(body[0], ast.Return), (
        f"the Abort branch must start with the return; it starts with "
        f"{type(body[0]).__name__}"
    )
    assert len(body) == 1 and abort.orelse == [], (
        "an Abort must leave the loop at once -- anything after the "
        "return, or an else, means a stuck model pays for one more "
        "request before it is stopped"
    )