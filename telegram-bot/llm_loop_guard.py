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

`last_empty` is incremented in two distant places:

  * `note_rejected_call()` -- three times inside the tool loop, when the
    model emits malformed args (too repetitive, unparseable, or parsed
    to an empty object).
  * `tick()` -- once at the tail, when the iteration produced no real
    result.

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

    __slots__ = ("budget_sec", "prev_calls", "last_empty")

    def __init__(self, budget_sec: float):
        self.budget_sec = budget_sec
        self.prev_calls = []
        self.last_empty = 0

    # -- called from inside the tool loop ---------------------------

    def note_rejected_call(self) -> None:
        """A tool call was rejected for malformed args.

        Three sites in call_llama do this. It counts toward the same
        budget as an iteration that produced nothing, which is how a
        model that emits nothing but malformed calls gets stopped.
        """
        self.last_empty += 1

    # -- the loop tail ----------------------------------------------

    def tick(self, *, iteration: int, current_calls: list, elapsed: float,
             iter_had_real_result: bool, final_fallback: Optional[str],
             reasoning_tail: str, finish_reason) -> Optional[Nudge | Abort]:
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
            return Abort(
                f'[bot: the model asked for the same tool call twice in a row '
                f'({current_calls[0][0]}). It is stuck in a loop. '
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
        else:
            self.last_empty += 1
            if self.last_empty >= 3:
                log.info(f"[loop] iter={iteration} 3 empty iters in a row; aborting")
                return Abort(
                    f'_(модель 3 итерации подряд не получила полезных данных от инструментов; '
                    f'останавливаю, чтобы не сжигать токены. '
                    f'finish_reason={finish_reason}, reasoning_chars={len(reasoning_tail)})\n\n'
                    f'{reasoning_tail}'
                )
            if self.last_empty >= 2:
                return Nudge(NO_NUDGE_TEXT)

        return None