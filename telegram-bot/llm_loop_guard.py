"""The tool loop's guards.

TECH-DEBT #3, Block C3. call_llama's loop tail, extracted so it can be
tested without an engine, a fake httpx, an abort event and a Telegram
callback -- the same four things C2 removed.

The ledger calls this an "abort ladder". Measured against the code, it is
not one, and building it as one would have changed behaviour:

  * The rungs are in two places, not one. `hallucinated tool_calls` fires
    at the top of the iteration, before any tool runs, and returns a dict
    (text + executed_tools + aborted) while everything else returns a
    plain string. It stays in call_llama.
  * The order inside the tail is not cheapest-first. Identical-call is a
    list compare, wall-clock is a subtraction, and last_empty is a
    counter -- but the counter rung is evaluated LAST even though it is
    the one that has to stop a token-burning loop. Reordering them would
    change which message the user gets when two conditions fire on the
    same iteration. The order below is the order in the code, unchanged.

The shared counter, which is the subtle part

`last_empty` counts the fruitless iterations of the tool loop, and two
things feed it:

  * `note_rejected_call()` -- once per rejected tool call (too
    repetitive, unparseable, or parsed to an empty object).
  * `tick()` -- once at the tail, when the iteration produced no real
    result AND carried no rejected call.

An iteration that carried a rejected call is counted ONCE, by the
rejection. It used to be counted twice -- once by the rejection and once
by the tail -- which made a single rejected call look like the second
empty iteration. The second empty iteration ends the tool loop with a
nudge, so the model was told "please re-emit the tool call" and the loop
ended before it could: the user got a canned diagnostic instead of the
work. That is not a theoretical difference. On 2026-10-24 a legitimate
3081-character bash command was rejected by the argument screen, the
nudge fired, the final summarize call came back empty, and the user got
a message about flight prices.

And it is zeroed at the tail whenever the iteration DID produce a real
result, which overwrites the rejections counted in the same iteration.
That is deliberate-looking but was not designed -- it is the behaviour
call_llama has always had, and it is what the tests pin. Moving the
counter into an object and resetting it per iteration would have quietly
changed how many malformed calls it takes to stop a loop.
"""
from typing import NamedTuple, Optional

from logconf import get_logger

log = get_logger(__name__)


class Nudge(NamedTuple):
    """Tell the model to wrap up, but let the loop continue."""

    text: str


class Abort(NamedTuple):
    """Stop the loop and hand this text back to the handler."""

    text: str


NO_NUDGE_TEXT = (
    'The previous tool calls returned no useful data. '
    'Please stop searching and give your final answer now — '
    'if you cannot find the requested information, say so honestly '
    'and suggest where the user can find it themselves.'
)


class LoopGuard:
    """Owns the two pieces of loop state the guards need.

    Not reusable across turns: prev_calls and last_empty are per-call.
    One instance per call_llama.
    """

    __slots__ = ("budget_sec", "prev_calls", "last_empty",
                 "_rejected_this_iter")

    def __init__(self, budget_sec: float):
        self.budget_sec = budget_sec
        self.prev_calls = []
        self.last_empty = 0
        self._rejected_this_iter = False

    # -- called from inside the tool loop ---------------------------

    def note_rejected_call(self) -> None:
        """A tool call was rejected for malformed args.

        It counts toward the same budget as an iteration that produced
        nothing, which is how a model that emits nothing but malformed
        calls gets stopped.

        One rejection is ONE fruitless iteration. The tail must not
        count the same iteration again: the model was just told "please
        re-emit the tool call", and if the tail counts the iteration as
        empty too, the loop ends before the re-emit can happen. The flag
        is what keeps those two facts apart.
        """
        self.last_empty += 1
        self._rejected_this_iter = True

    # -- the loop tail ----------------------------------------------

    def tick(self, *, iteration: int, current_calls: list, elapsed: float,
             iter_had_real_result: bool, final_fallback: Optional[str],
             reasoning_tail: str, finish_reason,
             last_tool_text: Optional[str] = None) -> Optional[Nudge | Abort]:
        """Evaluate the tail guards in the order call_llama evaluates them.

        Returns None to continue, a Nudge to append to the message list,
        or an Abort whose text becomes the turn's reply.
        """
        # 1. identical-call. Guarded on current_calls being non-empty:
        #    T32c. When every tool call in an iteration was rejected,
        #    current_calls stayed [] and equals prev_calls=[] on the
        #    first iteration -- a spurious "stuck" report, plus an
        #    IndexError on current_calls[0].
        if current_calls and current_calls == self.prev_calls:
            log.info(f"[loop] iter={iteration} same tool calls as previous iter; aborting")
            # The truth first. On 2026-10-26 the operator got this
            # message with "Final answer so far: (none)" -- a dump of
            # the bot's internals and no reason at all, while the tool
            # had already said WHY it could not do it. The tool's last
            # words are the answer the user needed; the loop is the
            # bot's problem, not the user's.
            if last_tool_text:
                return Abort(
                    f'[bot: I stopped the turn -- the model asked to run '
                    f'{current_calls[0][0]} twice in a row, and a third try '
                    f'would not have told you anything new. What the tool '
                    f'said: {last_tool_text}]'
                )
            return Abort(
                f'[bot: I stopped the turn -- the model asked to run '
                f'{current_calls[0][0]} twice in a row, and a third try '
                f'would not have told you anything new. '
                f'Final answer so far: {final_fallback or "(none)"}]'
            )
        self.prev_calls = current_calls

        # 2. wall-clock. Bounds the total even when every iteration
        #    produces a DIFFERENT tool call, which is exactly the case
        #    the identical-call detector above cannot see.
        if elapsed > self.budget_sec:
            log.info(f"[loop] iter={iteration} time budget exceeded ({elapsed:.0f}s > {self.budget_sec}s); aborting")
            return Abort(
                f'[bot: time budget exceeded after {int(elapsed/60)} min and {iteration+1} iterations. '
                f'The model is reasoning too slowly for this question. '
                f'Try a simpler question, /reset, or a different model.]'
            )

        # 3. empty-result budget. The zeroing here deliberately
        #    overwrites rejections counted earlier in the same
        #    iteration; see the module docstring before "fixing" it.
        if iter_had_real_result:
            self.last_empty = 0
        elif not self._rejected_this_iter:
            # An iteration whose tool calls were all rejected was
            # already counted by note_rejected_call(). Counting it here
            # as well made one rejection look like two, and two is the
            # nudge that ends the tool loop -- so the model was told
            # "please re-emit the tool call" and the loop ended before
            # it could.
            self.last_empty += 1
        self._rejected_this_iter = False
        if self.last_empty >= 3:
            log.info(f"[loop] iter={iteration} 3 empty iters in a row; aborting")
            return Abort(
                f'_(the model got no useful data from the tools for 3 '
                f'iterations in a row; stopping it to avoid burning tokens. '
                f'finish_reason={finish_reason}, reasoning_chars={len(reasoning_tail)})\n\n'
                f'{reasoning_tail}'
            )
        if self.last_empty >= 2:
            return Nudge(NO_NUDGE_TEXT)

        return None