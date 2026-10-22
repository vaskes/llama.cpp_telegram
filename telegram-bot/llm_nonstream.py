"""The non-streaming iteration: one POST, then parse and validate.

TECH-DEBT #3, Block C1. Extracted from the middle of call_llama's body.
This is the vision path -- handle_photo and its retry -- and the
slow-reasoning models that do not stream.

Why the seam is the try BODY and not the branch

`try:` sits at 608 in call_llama and its `except` at 730, with the loop's
buffer writes in between. Cutting the branch cut the try in half and left
the handler orphaned -- the module stopped compiling. So call_llama keeps
the try/except and this module is what runs inside it. That also means
exception semantics are unchanged: a failure in push_threading, which
happens after this returns, is still caught by the same handler.

Why the result has an outcome field

The branch never simply returned an answer. With tool_calls --
react_to_message on a vision task -- it hands content, reasoning,
tool_calls and finish_reason to the SHARED dispatch loop and sets
_ns_handled so the streaming block is skipped for this iteration. An
unconditional `return content` existed once and was removed when
react_to_message landed, because it silently dropped the tool call.

    FINAL  return final_text to the handler. Abort -> None, context
           overflow -> OVERFLOW_MESSAGE, no choices -> the "try a
           shorter prompt" text.
    LOOP   fill the caller's buffers and continue into the shared loop.

A helper returning a bare string would have to smuggle "return None",
"return this message" and "keep going" through one value, and the third
is the bug.
"""
import json
from dataclasses import dataclass
from typing import Any, Dict, NamedTuple, Optional

import httpx

import limits
from config import API_KEY, LLAMA_URL
from context_budget import OVERFLOW_MESSAGE, is_context_overflow
from http_clients import _abortable_post
from logconf import get_logger
from tool_isolation import (KNOWN_TOOL_NAMES, UnknownToolNameError,
                            validate_tool_calls)

log = get_logger(__name__)


class NonStreamResult(NamedTuple):
    outcome: str                  # "final" | "loop"
    final_text: str | None = None
    content: str = ""
    reasoning: str = ""
    tool_calls: list = []
    finish_reason: str = "stop"
    saw_unavailable_tool: bool = False


async def non_stream_iteration(req_body: dict, *, iteration: int,
                               abort_event=None) -> NonStreamResult:
    """One non-streaming POST, its parse, and tool validation.

    req_body comes from the caller so this stays transport-and-parse.
    Raises whatever the transport raises; call_llama's own handler deals
    with it.
    """
    # Was an enclosing-scope variable in call_llama (assigned to False on
    # every iteration before the branch). As a local it only existed on
    # the UnknownToolNameError path, so a successful vision call raised
    # UnboundLocalError at the return below -- which would have crashed
    # every photo the bot handled.
    saw_unavailable_tool = False
    payload_bytes = json.dumps(req_body, ensure_ascii=False).encode('utf-8')
    async with httpx.AsyncClient(
        timeout=limits.LLAMA_HTTP_TIMEOUT_SEC,
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=1),
    ) as vclient:
        # v0.7.6: race the post against abort_event.
        # The plain vclient.post() blocks until the
        # full response arrives (5-30s for vision).
        # The Stop button sets abort_event, but the
        # post() doesn't see it. With _abortable_post,
        # we detect abort as soon as the user clicks.
        r = await _abortable_post(
            vclient, f"{LLAMA_URL}/chat/completions",
            abort_event=abort_event,
            headers={"Authorization": f"Bearer {API_KEY}",
                     "Content-Type": "application/json"},
            content=payload_bytes,
        )
        if r is None:
            # User clicked Stop. Return None so the
            # handler treats this as "aborted" and
            # doesn't send a partial response.
            log.info(f"[call_llama] abort_event fired during non-streaming iter={iteration}, aborting")
            return NonStreamResult("final", final_text=None)
        r.raise_for_status()
        data = r.json()
    # T32 (2026-10-13): guard against empty 'choices'.
    # Heavy prompts that exhaust the context window, or
    # rate-limit / content-filter responses, can return
    # 200 OK with choices:[] (or with no 'message' key).
    # Before this guard, every such response produced
    # 'list index out of range' in the bot's reply, and
    # the user saw a generic 'Error: list index out of
    # range' with no useful info. Surface a clear,
    # user-readable message instead.
    _choices = data.get("choices") or []
    if not _choices:
        # T70.4: distinguish "context exhausted" from the other
        # two causes. Before this, an overflow produced a normal
        # answer-shaped message and the bot hallucinated about
        # the truncated conversation.
        _raw = (data.get("error") or {})
        _detail = json.dumps(_raw) if _raw else json.dumps(data)[:500]
        if is_context_overflow(_detail):
            log.info("[call_llama] CONTEXT OVERFLOW detected")
            log.info("[call_llama] CONTEXT OVERFLOW detected")
            return NonStreamResult("final", final_text=OVERFLOW_MESSAGE)
        return NonStreamResult(
            "final",
            final_text=(
                "[model returned no choices — the prompt may "
                "have exceeded the context window, hit a rate "
                "limit, or been blocked by a content filter. "
                "Try a shorter prompt or try again in a minute.]"
            ),
        )
    msg0 = _choices[0].get("message") or {}
    content = msg0.get("content") or ""
    reasoning = msg0.get("reasoning_content") or ""
    non_stream_tool_calls = msg0.get("tool_calls") or []
    # T38.5: tool isolation. If llama-server returned a tool_call
    # whose function name is not in the bot's whitelist, we refuse
    # to dispatch it. This is the bot-side guarantee that the
    # model can NEVER reach llama-server's native --agent or
    # MCP-loaded tools.
    try:
        validate_tool_calls(non_stream_tool_calls)
    except UnknownToolNameError as _e:
        # T43: soft-reject. Rewrite the unknown tool_calls
        # in place to `__rejected__` so the dispatch loop
        # below hits its "unhandled tool" branch, and
        # inject a synthetic tool result into msgs so the
        # model can read it as a tool-role message on the
        # next iteration. This gives the model a chance to
        # retry with a whitelisted tool instead of
        # silently dropping the turn.
        bad_names = sorted({(tc.get("function") or {}).get("name") or "" for tc in non_stream_tool_calls})
        log.info(f"[TOOL-ISOLATION] iter={iteration} soft-reject {bad_names!r}; "
            f"injecting tool result feedback")
        known_list = ", ".join(sorted(KNOWN_TOOL_NAMES))
        for tc in non_stream_tool_calls:
            fn = (tc.get("function") or {})
            if (fn.get("name") or "") not in KNOWN_TOOL_NAMES:
                fn["name"] = "__rejected__"
                fn["arguments"] = "{}"
        saw_unavailable_tool = True
        # The dispatch loop below will run for the
        # __rejected__ entries and hit the "unhandled"
        # branch, which appends a `role: tool` message
        # explaining the rejection back to msgs. We do
        # NOT return here; the loop continues.

    return NonStreamResult(
        "loop",
        content=content,
        reasoning=reasoning,
        tool_calls=non_stream_tool_calls,
        finish_reason=data["choices"][0].get("finish_reason") or "tool_calls",
        saw_unavailable_tool=saw_unavailable_tool,
    )

async def tools_less_fallback(req_body: dict, *, iteration: int,
                              abort_event=None, primary_error: BaseException) -> str:
    """Retry one request with the tools removed, and decide what to say.

    v0.8.1: a 500 from llama-server is usually the PEG grammar parser
    rejecting the model's tool-call output. The model is not at fault;
    the grammar is. One retry without tools almost always succeeds,
    because the question itself was fine -- only the tool syntax was not.

    Lives here rather than in call_llama because it needs the same httpx
    client as the primary non-streaming call, and Block C1 moved that
    client's binding into this module. The fallback stayed behind,
    referencing a name that no longer existed there, and py_compile
    cannot see that kind of break.

    Returns the text for the turn. An empty-choices response aborts
    (None) rather than answering, same as T32 on the primary path.
    """
    log.info(f"[call_llama] 500 on tools-enabled request; retrying "
             f"iter={iteration} without tools")
    fallback_body = dict(req_body)
    fallback_body.pop("tools", None)
    fallback_body.pop("tool_choice", None)

    try:
        async with httpx.AsyncClient(
                timeout=limits.LLAMA_HTTP_TIMEOUT_SEC) as vclient:
            r2 = await _abortable_post(
                vclient, f"{LLAMA_URL}/chat/completions",
                abort_event=abort_event,
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json=fallback_body,
            )
            if r2 is None:
                return None
            r2.raise_for_status()
            data2 = r2.json()

        # T32 (2026-10-13): same empty-choices guard as the primary and
        # first-fallback paths. A 200 with choices:[] is a rate limit or
        # a content filter, not an answer.
        choices = data2.get("choices") or []
        if not choices:
            # Classify before giving up. The other four empty-choices
            # sites do, and this one returned None for every cause: a
            # context overflow -- the most likely one here, since the
            # primary request just failed with a 500 -- left the user
            # looking at nothing after a long wait, when the honest
            # answer was sitting in the error body.
            if is_context_overflow(
                    json.dumps(data2.get("error") or data2)[:500]):
                log.info("[call_llama] tools-less fallback: context overflow")
                return OVERFLOW_MESSAGE
            log.info("[call_llama] tools-less fallback: empty choices, "
                     "returning None")
            return None

        content = (choices[0].get("message", {}) or {}).get("content") or ""
        log.info(f"[call_llama] tools-less fallback succeeded: {len(content)} chars")
        return content
    except Exception as e2:
        log.info(f"[call_llama] tools-less fallback also failed: "
                 f"{type(e2).__name__}: {e2}")
        return f"[llama-server request failed: {primary_error}]"


# --- TECH-DEBT #8, block 2 -----------------------------------------
# What the non-streaming branch does with the result, moved out.
#
# My first version of this took the whole branch, `await
# non_stream_iteration` and the push_thinking call included. Two
# existing tripwires refused it, correctly:
#
#   test_c1_nonstream -- call_llama must call non_stream_iteration
#     inside its OWN try. C1's docstring says why: the try stays out
#     here so that a failure in push_thinking, which happens after the
#     helper returns, is caught by the same handler. Moving the try in
#     turns that failure into a terminal "[llama-server request
#     failed]" the user reads.
#
#   test_c4_thinking -- three push_thinking call sites, each passing
#     its context explicitly. One of them moved.
#
# So the seam is the RESULT, not the call. What is extracted is the
# decision: given what the engine answered, either the turn is over, or
# the loop continues carrying these six values. It is a pure function,
# which is why it needs no client, no clock and no await -- and is
# testable without any of them.
#
# `_ns_handled` disappears here too. It used to be set in this branch
# and read three blocks later through `locals()`; the caller now owns
# it as an ordinary local.


@dataclass(frozen=True)
class NonStreamStep:
    """Either the turn ends with `terminal`, or the loop continues.

    `is_terminal` is set by the DECISION, not inferred from `terminal`
    being non-empty. My first version inferred it, and the abort path --
    outcome "final" with final_text=None -- came back flagged as "neither"
    and raised. A None text is a real terminal value: it means the turn
    ended without a reply, which is what abort is for.

    `assert_terminal` rejects the genuinely-neither case, so a future
    edit that decides nothing is caught here rather than showing up as
    an empty reply in production with nothing in the log to explain it.
    """
    is_terminal: bool = False
    terminal: Optional[str] = None
    content_buf: str = ""
    reasoning_buf: str = ""
    tool_calls_buf: Optional[Dict[int, Dict[str, Any]]] = None
    accumulated_reasoning: str = ""
    saw_unavailable_tool: bool = False

    def assert_terminal(self) -> Optional[str]:
        if self.is_terminal and self.tool_calls_buf:
            raise AssertionError(
                "NonStreamStep is both terminal and a continuation; the "
                "loop would return and also dispatch tool calls")
        if not self.is_terminal and not self.tool_calls_buf:
            raise AssertionError(
                "NonStreamStep is neither terminal nor a continuation: "
                "is_terminal is False and tool_calls_buf is empty, so the "
                "loop would continue with nothing to dispatch and nothing "
                "to say")
        return self.terminal


def non_stream_step(
    result: "NonStreamResult",
    *,
    accumulated_reasoning: str,
    saw_unavailable_tool: bool,
) -> NonStreamStep:
    """Decide what one non-streaming response means for the loop.

    `result` is what `non_stream_iteration` produced. `accumulated_
    reasoning` is the caller's running total, already including this
    iteration's reasoning -- push_thinking has run by the time this is
    called, which is why it stays in call_llama.

    The three exits, unchanged from the inline branch:

      FINAL   outcome == "final". The helper already decided this one:
              abort -> None, context overflow -> OVERFLOW_MESSAGE, no
              choices -> the "try a shorter prompt" text.
      TEXT    content with no tool calls. Short-circuit.
      TOOLS   react_to_message on a vision task, or any other tool call.
              Fill tool_calls_buf in the SAME shape the SSE parser
              produces -- {idx: {id, name, arguments}} -- so the code
              that reconstructs the final list, shared with the
              streaming path, handles both without knowing which ran.

    The unconditional `return content` that used to sit here was
    removed when react_to_message landed, because it silently dropped
    the tool call and the user saw an empty response.
    """
    if result.outcome == "final":
        # is_terminal=True even when final_text is None: abort ends the
        # turn with no reply, and that is a decision, not an absence.
        return NonStreamStep(is_terminal=True, terminal=result.final_text)

    if result.saw_unavailable_tool:
        saw_unavailable_tool = True

    if not result.tool_calls:
        return NonStreamStep(is_terminal=True, terminal=result.content,
                             accumulated_reasoning=accumulated_reasoning,
                             saw_unavailable_tool=saw_unavailable_tool)

    tool_calls_buf: Dict[int, Dict[str, Any]] = {}
    for idx, tc in enumerate(result.tool_calls):
        fn = tc.get("function", {}) or {}
        tool_calls_buf[idx] = {
            "id": tc.get("id", f"call_ns_{idx}"),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "") or "",
        }
    return NonStreamStep(
        terminal=None,
        content_buf=result.content,
        reasoning_buf=accumulated_reasoning,
        tool_calls_buf=tool_calls_buf,
        accumulated_reasoning=accumulated_reasoning,
        saw_unavailable_tool=saw_unavailable_tool,
    )
