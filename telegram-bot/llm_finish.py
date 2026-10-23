"""How an iteration ends when the model did not ask for a tool.

TECH-DEBT #8, Block D3.

Two terminal shapes, sitting between the tool-call normalisation and the
dispatch loop:

  * no tool calls at all -- content, or reasoning, or nothing
  * tool calls for a turn that never asked for tools -- the model
    hallucinated a tool, which on a vision or math turn would otherwise
    enter a loop the user cannot escape

Both existed inline as a wall of returns. They are pure functions of the
buffers and the finish reason, so they can be stated once, tested
exhaustively, and read without holding the whole loop in mind.

Neither ending is obviously a bug, and neither is treated as one: an
empty response with reasoning is answered with the reasoning. Some
models -- Ornith is named in the original comment -- burn the entire
token budget thinking and never emit content. Surfacing the tail tells
the user why the reply is odd; a generic "empty response" string would
just look broken.

Both return the value `call_llama` returns, unchanged, including the
mixed types. The dict form means "a real answer, with the tool ledger
attached"; the string form means "a diagnostic the caller sends as is".
Flattening that to one type would mean wrapping every `return` in the
loop, which is the opposite of what this block is for.

The three endings are shared but their *messages* are not: each says
something different about what went wrong, and collapsing them into one
formatter would have made the diagnostics interchangeable.

No logging here: the caller owns the log line and the runbook greps it.
"""
from typing import Any, NamedTuple

# The empty-response marker lives in config because handlers.py
# screens for it and config is the module both can reach. The
# consumer side is six call sites; the producer side is this
# function plus config.EMPTY_RESPONSE_FALLBACK.
from config import EMPTY_RESPONSE_PREFIX

REASONING_TAIL_CHARS = 3500


class Ending(NamedTuple):
    """Which of the three endings this iteration reached.

    A classifier rather than a formatter, because the messages differ.
    Returning the value directly was the first attempt and it silently
    dropped the prefix on the reasoning branch.
    """

    kind: str                      # "content" | "reasoning" | "nothing"
    content: str = ""
    reasoning_tail: str = ""

    @classmethod
    def of(cls, content_buf: str, reasoning_buf: str) -> "Ending":
        if content_buf:
            return cls("content", content_buf)
        if reasoning_buf:
            return cls("reasoning", reasoning_tail=reasoning_buf[-REASONING_TAIL_CHARS:])
        return cls("nothing")


def _answer_dict(ending: Ending, executed_tools: list) -> dict:
    return {"text": ending.content,
            "executed_tools": list(executed_tools),
            "aborted": False}


def finish_without_tool_calls(*, content_buf: str, reasoning_buf: str,
                              finish_reason, executed_tools: list) -> Any:
    """The model produced no tool calls. One of three endings."""
    ending = Ending.of(content_buf, reasoning_buf)

    if ending.kind == "content":
        return _answer_dict(ending, executed_tools)
    if ending.kind == "reasoning":
        # The marker comes first on purpose. The handlers retry exactly the
        # responses that start with config.EMPTY_RESPONSE_PREFIX, and a
        # reasoning-only answer IS an empty answer -- it just happens to
        # carry the model's thoughts along. Without the marker the model
        # got one attempt and the user got a diagnostic instead of a reply.
        # 2026-10-23, production: 18 generated tokens, finish_reason=stop,
        # and the user was told the model had spent all its tokens.
        #
        # The wording follows the finish reason, because the two are
        # different failures. "length" means the budget ran out in the
        # middle of a thought; anything else means the model stopped on
        # its own and chose not to write an answer. The old wording
        # claimed the budget in both cases, and for 18 tokens out of a
        # 65536-token budget that was simply false.
        if finish_reason == "length":
            what = ("модель потратила все токены на размышления, "
                    "не оставив на сам ответ")
        else:
            what = ("модель закончила ответ в размышлениях "
                    "и не выдала текст")
        return (
            f'{EMPTY_RESPONSE_PREFIX} {what}; '
            f'finish_reason={finish_reason}; reasoning_chars={len(reasoning_buf)}]'
            f'\n\n{ending.reasoning_tail}'
        )
    return (
        f"{EMPTY_RESPONSE_PREFIX} finish_reason={finish_reason}, "
        f'reasoning_chars={len(reasoning_buf)}, content_chars=0]'
    )


def refuse_hallucinated_tools(*, content_buf: str, reasoning_buf: str,
                              finish_reason, executed_tools: list) -> Any:
    """The model asked for a tool on a turn where none were offered.

    A vision or math turn passes `tools=None`. A model that emits a tool
    call there is confused, not helpful, and looping on it produces an
    unbreakable turn: the next request offers no tools either, the model
    emits the same call, and the user watches.

    Whatever content did come back is returned. If nothing did, the
    reasoning tail is shown with an explicit "(no reasoning)" rather
    than an empty section -- an unexplained blank is worse than a
    verbose one.
    """
    ending = Ending.of(content_buf, reasoning_buf)

    if ending.kind == "content":
        return _answer_dict(ending, executed_tools)
    return (
        f"_(модель попыталась вызвать инструмент, хотя в этом turn'е он не "
        f"запрашивался; finish_reason={finish_reason}, "
        f"content_chars={len(content_buf)})_\n\n"
        f'{ending.reasoning_tail if ending.reasoning_tail else "(no reasoning)"}'
    )

def finish_fallback_empty(*, content, reasoning, finish_reason) -> str:
    """The 500-fallback response carried no tool calls.

    The third of the three endings, and the only one that returns a bare
    string in every case. This path is already a degraded one -- the
    engine failed once already and this is the retry with the tools
    removed -- and the caller returns the value straight to Telegram, so
    there is no ledger to attach and no structured form to build.

    Same three endings as the other two, in the same order, for the same
    reason: content is the answer, reasoning explains an empty answer,
    and neither gets a message that says which happened.

    Kept separate from the other two rather than folded into one
    formatter, because the three messages say different things about
    different failures. Only the ordering is shared, and that is
    carried by `Ending`.
    """
    ending = Ending.of(content or "", reasoning or "")

    if ending.kind == "content":
        return ending.content
    if ending.kind == "reasoning":
        return (
            f'_(fallback non-stream: content пустой, '
            f'reasoning_chars={len(reasoning)})_\n\n'
            f'{ending.reasoning_tail}'
        )
    return f'[fallback empty: finish_reason={finish_reason}]'
