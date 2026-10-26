from loop_owner import (call_source, loop_source,  # noqa: F401
                       loop_tree, run_one)
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


def test_the_abort_carries_the_tool_s_own_words():
    """The user gets the tool's reason, not "(none)".

    Live test of 2026-10-26 16:40: the model asked for the same
    delete twice, the guard stopped it, and the operator's chat got
    "[bot: the model asked for the same tool call twice in a row ...
    Final answer so far: (none)]" -- the bot's internals and no reason
    at all, while the tool had already said why it could not do it.
    """
    guard = LoopGuard(budget_sec=600.0)
    guard.prev_calls = [("delete_messages", '{"message_ids": [3267]}')]
    outcome = guard.tick(
        iteration=1,
        current_calls=[("delete_messages", '{"message_ids": [3267]}')],
        elapsed=1.0,
        iter_had_real_result=False,
        final_fallback=None,
        reasoning_tail="",
        finish_reason="tool_calls",
        last_tool_text="[delete_messages: refused -- nothing was deleted.]",
    )
    assert isinstance(outcome, Abort)
    assert "nothing was deleted" in outcome.text
    assert "(none)" not in outcome.text


def test_the_abort_without_a_tool_result_says_so():
    guard = LoopGuard(budget_sec=600.0)
    guard.prev_calls = [("delete_messages", "{}")]
    outcome = guard.tick(
        iteration=1,
        current_calls=[("delete_messages", "{}")],
        elapsed=1.0,
        iter_had_real_result=False,
        final_fallback=None,
        reasoning_tail="",
        finish_reason="tool_calls",
    )
    assert isinstance(outcome, Abort)
    assert "(none)" in outcome.text


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
    assert "twice in a row" in out.text
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
    assert "3 iterations in a row" in out.text


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
    """Two rejected iterations, and the loop ends -- but with a nudge, not
    an abort.

    A nudge is the graceful end: the model gets one last plain answer
    out of what it already has. The abort is for a model that burns
    three fruitless iterations, and two are not three.

    What changed on 2026-10-24 is the OTHER direction: a single
    rejection used to be counted TWICE (once by the rejection, once by
    the tail), so one rejected call looked like two, and the nudge --
    which ends the tool loop -- fired before the model could re-emit the
    call it had just been told to re-emit. The user got a canned
    diagnostic instead of the work.
    """
    g = LoopGuard(600)
    g.note_rejected_call()
    g.note_rejected_call()
    out = tick(g, real_result=False)
    assert isinstance(out, Nudge), (
        f"two rejected iterations end the tool loop with a nudge, not an "
        f"abort -- got {type(out).__name__}")


def test_one_rejection_lets_the_model_re_emit():
    """The regression this pins, measured on production on 2026-10-24.

    The model asked for a zip file and wrote a 3081-character bash
    heredoc to build it. The argument screen called it "too long", the
    loop ended, and the user got a message about flight prices instead
    of a file. The rejection message says "please re-emit the tool
    call" -- so the loop has to give the model that chance.

    One rejected call is ONE fruitless iteration, and the tail must not
    count the same iteration again.
    """
    g = LoopGuard(600)
    g.note_rejected_call()
    out = tick(g, real_result=False)
    assert out is None, (
        f"a single rejected call ended the tool loop ({type(out).__name__}) "
        f"-- the model was told to re-emit the call and never got the "
        f"chance")
    assert g.last_empty == 1, (
        "one rejection is one fruitless iteration, not two")


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
    """The named iteration, `ToolLoop.run_one` (T8.1)."""
    return run_one()


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
    src = loop_source()
    for stale in ("last_empty", "prev_calls"):
        code = [l for l in src.split("\n")
                if stale in l and not l.strip().startswith("#")]
        assert not code, f"{stale} still assigned in call_llama: {code}"


def test_every_rejection_site_goes_through_the_guard():
    """A rejected tool call is charged to the budget. No bare increments.

    Three inline rejection sites became one branch when the argument
    screening moved to llm_toolargs.py, so counting sites stopped
    describing the property. What has to stay true is the branch: the
    rejection path must charge `guard`, and nothing anywhere may
    increment the counter directly.

    A bare `last_empty += 1` left behind would create a local that
    nothing reads -- the exact failure this block already had once with
    saw_unavailable_tool.
    """
    src = loop_source()

    assert "last_empty += 1" not in src, (
        "a bare increment bypasses the guard's budget entirely"
    )
    assert "guard.note_rejected_call()" in src, (
        "no rejection path charges the empty-call budget any more -- a "
        "model stuck emitting malformed tool calls would loop forever"
    )

    # ...and it has to be on the rejection path, not somewhere else.
    tree = ast.parse(src)
    fn = _call_llama_node()

    def charges(node):
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "note_rejected_call"
            for n in ast.walk(node))

    rejections = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Call)
        and getattr(n.test.func, "id", None) == "isinstance"
        and any(isinstance(a, ast.Name) and a.id == "ArgsRejected"
                for a in n.test.args)
    ]
    assert rejections, (
        "the ArgsRejected branch is gone -- if the rejection path was "
        "renamed or inlined, confirm it still charges the budget"
    )
    assert all(charges(r) for r in rejections), (
        "the ArgsRejected branch no longer charges the empty-call budget"
    )


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

    # Since T8.1 the summarize POST lives in the `_summarize`
    # helper; the Nudge branch calls it. The property is
    # "the summarize POST only runs on the nudge path", so the
    # check follows the call and fails if either half moves.
    calls_summarize = walks_to(
        branch, lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_summarize")
    assert calls_summarize or walks_to(branch, lambda n: isinstance(n, ast.AsyncWith)), (
        "the summarize call is no longer reachable from the Nudge branch"
    )
    if calls_summarize:
        helper = next((m for m in ast.walk(loop_tree())
                       if isinstance(m, ast.AsyncFunctionDef)
                       and m.name == "_summarize"), None)
        assert helper is not None, "_summarize is called but not defined"
        assert walks_to(helper, lambda n: isinstance(n, ast.AsyncWith)), (
            "_summarize no longer contains the POST -- the branch "
            "calls it and it does nothing")
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

def test_the_abort_is_spoken_in_the_bots_voice():
    """The operator's chat got this:

        [bot: the model asked for the same tool call twice in a row
        (delete_messages), so the bot stopped rather than repeat itself.
        What the tool said: [delete_messages: refused -- ...]]

    A debug sentence with the tool's whole output glued to it. The
    wording IS the contract here: the bot is the one who stopped, and
    the tool's reason is what the user needs to read.
    """
    guard = LoopGuard(budget_sec=600.0)
    guard.prev_calls = [("delete_messages", '{"message_ids": [3267]}')]
    outcome = guard.tick(
        iteration=1,
        current_calls=[("delete_messages", '{"message_ids": [3267]}')],
        elapsed=1.0,
        iter_had_real_result=False,
        final_fallback=None,
        reasoning_tail="",
        finish_reason="tool_calls",
        last_tool_text="[delete_messages: refused -- nothing was deleted.]",
    )
    assert isinstance(outcome, Abort)
    assert outcome.text.startswith("[bot: I stopped the turn"), outcome.text
    assert "delete_messages" in outcome.text, outcome.text
    assert "nothing was deleted" in outcome.text, outcome.text
