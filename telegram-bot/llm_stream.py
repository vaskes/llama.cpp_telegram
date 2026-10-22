"""The streaming iteration: one SSE POST, and the non-streaming fallback.

TECH-DEBT #8 block 3 (T3.2). Extracted from call_llama's tool loop.

Why this one is harder than block 2

`non_stream_iteration` returned a parsed result and nothing else. This
branch does three things that do not reduce to a return value:

  * it exits the loop at six places, and two of those exits are `None`
    (the Stop button, before the first chunk and between chunks), so a
    caller that asks "is `terminal` empty?" gets the wrong answer on
    exactly the path a user triggers;
  * it writes loop state on the way -- `saw_unavailable_tool`,
    `final_fallback`, and one `msgs.append`;
  * it hands its buffers to the SAME dispatch code the non-streaming
    path uses, so the shapes have to match.

So the return value carries both meanings explicitly. `is_terminal` is
set by the decision; `terminal` may legitimately be None. Inferred
terminality was my first version and it is the same mistake
`NonStreamStep` already documents: abort ends the turn with no reply,
and that is a decision, not an absence.

Why `finish_reason` and `tool_calls_buf` go IN as well as out

The fallback path reuses `tool_calls_buf` and `finish_reason` from the
streaming attempt it just failed. Before the cut those were plain
locals of the loop; now they arrive as arguments and leave again in
the outcome. Forgetting either one is silent: the loop carries on with
an empty tool-call map and an empty finish_reason, and the user gets a
turn with no tool trace and no explanation.

Why the SSE framing is here and not in `StreamAccumulator.feed`

The accumulator parses JSON objects. `data: ` prefixes, the `[DONE]`
sentinel and the skip-on-malformed are the transport's business, and
they are the three guards the first cut of block C2 deleted -- a green
suite, because nothing drove this branch. They live here, and they are
now driven by tests that call this function instead.
"""
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

import limits
from config import API_KEY, LLAMA_URL
from context_budget import OVERFLOW_MESSAGE, is_context_overflow
from llm_finish import finish_fallback_empty
from llm_sse import StreamAccumulator
from llm_thinking import push_thinking
from llm_toolcalls import reject_unknown_names
from logconf import get_logger
from tool_isolation import (KNOWN_TOOL_NAMES, UnknownToolNameError,
                            validate_tool_calls)

log = get_logger(__name__)


@dataclass(frozen=True)
class StreamOutcome:
    """Either the turn ends with `terminal`, or the loop continues.

    `is_terminal` is set by the DECISION, never inferred from `terminal`
    being non-empty. Both abort paths here return None: a missing
    `is_terminal` would treat them as "keep going" and the loop would
    carry on with whatever the previous iteration left in the buffers,
    replying with the model's earlier text to a question the user
    already cancelled.
    """

    is_terminal: bool = False
    terminal: Optional[str] = None
    content_buf: str = ""
    reasoning_buf: str = ""
    tool_calls_buf: Optional[Dict[int, Dict[str, Any]]] = None
    finish_reason: str = ""
    saw_unavailable_tool: bool = False
    final_fallback: Optional[str] = None
    append_msg: Optional[dict] = None

    def assert_terminal(self) -> Optional[str]:
        """Reject the one contradiction this type can actually reach.

        There is no "neither" case here, and adding one would be a lie:
        unlike `NonStreamStep`, a non-terminal streaming outcome is
        always a real continuation -- an empty answer still reaches
        `finish_without_tool_calls`, which is what turns it into the
        `config.EMPTY_RESPONSE_PREFIX` text the user sees. A check
        that could only be false on a bug nobody can write is a check
        that reads as coverage and is not.
        """
        if self.is_terminal and self.append_msg is not None:
            raise AssertionError(
                "StreamOutcome is both terminal and a continuation; the "
                "loop would return and also append the assistant message")
        return self.terminal


async def _fallback_to_nonstream(
    req_body: dict, *,
    iteration: int,
    stream_err: Exception,
    saw_unavailable_tool: bool,
    final_fallback: Optional[str],
    finish_reason: str,
    tool_calls_buf: Optional[Dict[int, Dict[str, Any]]],
) -> StreamOutcome:
    """The stream died; ask again without it and parse a whole response.

    Reached only from `except httpx.HTTPError`, so the caller has
    already logged and decided to try once more.
    """
    try:
        async with httpx.AsyncClient(
                timeout=limits.LLAMA_HTTP_TIMEOUT_SEC) as client2:
            r2 = await client2.post(
                f"{LLAMA_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json={**req_body, "stream": False},
            )
            r2.raise_for_status()
            data = r2.json()
        # T32 (2026-10-13): same guard as the primary non-streaming path
        # above -- 'choices' may be empty on rate-limit / context-overflow
        # / filter responses. Without this, the user gets a 'list index
        # out of range' on the fallback path too, even though we already
        # guarded the primary path.
        # One `if not _choices2:` containing the classification, the same
        # shape as the other four sites. The first draft split it in two
        # -- `if not X and is_context_overflow(...)` and then a second
        # bare `if not X` -- which reads fine and satisfies no test that
        # asks whether THIS branch checked. Splitting it also made the
        # two halves easy to reorder past each other.
        _choices2 = data.get("choices") or []
        if not _choices2:
            if is_context_overflow(
                    json.dumps(data.get("error") or data)[:500]):
                log.info("[call_llama] CONTEXT OVERFLOW on fallback")
                return StreamOutcome(is_terminal=True,
                                     terminal=OVERFLOW_MESSAGE)
            return StreamOutcome(
                is_terminal=True,
                terminal=("[model returned no choices on fallback — "
                          "the prompt may have exceeded the context window, "
                          "hit a rate limit, or been blocked by a content "
                          "filter. Try a shorter prompt or try again in a "
                          "minute.]"))
        msg = _choices2[0].get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        # T38.5: tool isolation (see validate_tool_calls docstring).
        try:
            validate_tool_calls(tool_calls)
        except UnknownToolNameError:
            # T43: soft-reject (same as site 1).
            bad_names = reject_unknown_names(
                tool_calls, known=KNOWN_TOOL_NAMES)
            if bad_names:
                log.info(f"[TOOL-ISOLATION] iter={iteration} soft-reject "
                         f"{sorted(bad_names)!r}; injecting tool result "
                         f"feedback")
                saw_unavailable_tool = True
        if not tool_calls:
            return StreamOutcome(
                is_terminal=True,
                terminal=finish_fallback_empty(
                    content=msg.get("content"),
                    reasoning=msg.get("reasoning_content"),
                    finish_reason=msg.get("finish_reason")),
                saw_unavailable_tool=saw_unavailable_tool,
                final_fallback=final_fallback)
        if msg.get("content"):
            final_fallback = msg["content"]
        return StreamOutcome(
            content_buf=msg.get("content") or "",
            reasoning_buf=msg.get("reasoning_content") or "",
            # carried through, not replaced: the streaming attempt that
            # failed may already have populated these.
            tool_calls_buf=tool_calls_buf,
            finish_reason=finish_reason,
            saw_unavailable_tool=saw_unavailable_tool,
            final_fallback=final_fallback,
            append_msg=msg)
    except Exception as e2:
        log.info(f"[fallback err iter={iteration}] {type(e2).__name__}: {e2}")
        return StreamOutcome(
            is_terminal=True,
            terminal=(f"[both streaming and non-streaming failed: "
                      f"stream_err={stream_err!r}, fallback_err={e2!r}]"))


async def stream_iteration(
    client, req_body: dict, *,
    iteration: int,
    abort_event=None,
    accumulated_reasoning: str,
    thinking_msg=None,
    silent: bool,
    throttle,
    saw_unavailable_tool: bool = False,
    final_fallback: Optional[str] = None,
    finish_reason: str = "",
    tool_calls_buf: Optional[Dict[int, Dict[str, Any]]] = None,
) -> StreamOutcome:
    """One streaming POST, folded into the four buffers.

    `client` comes from the caller so the HTTP connection is shared
    with the non-streaming attempt above, exactly as it was when this
    was inline. `req_body` too: building it is block 1's job and it is
    already logged by then.

    Every exit is stated as an outcome rather than a `return`, because
    two of the six are None and the loop needs to know which meaning it
    is looking at.
    """
    reasoning_buf = ""
    content_buf = ""
    try:
        async with client.stream("POST", f"{LLAMA_URL}/chat/completions",
                                 headers={"Authorization": f"Bearer {API_KEY}",
                                          "Content-Type": "application/json"},
                                 json=req_body) as r:
            r.raise_for_status()
            # Pre-check for abort before entering the line iteration.
            # This catches the case where the user clicks Stop RIGHT
            # after the bot sends the thinking message but before the
            # LLM has produced any chunks.
            #
            # IMPORTANT: do NOT use `asyncio.wait_for` with a short
            # timeout on `__anext__()`. Cancelling the read task
            # mid-stream corrupts httpx's internal state and causes the
            # LLM to return empty responses on subsequent calls. Plain
            # `async for` is safe; the only downside is a small window
            # (typically <1s) where an abort set after the LLM has
            # started but before the first chunk is not seen until the
            # first chunk arrives. The between-chunk check below handles
            # that.
            if abort_event is not None and abort_event.is_set():
                log.info(f"[call_llama] abort_event set pre-stream "
                         f"iter={iteration}")
                return StreamOutcome(is_terminal=True, terminal=None)
            acc = StreamAccumulator()
            async for line in r.aiter_lines():
                # Between-chunk abort check. Fires as soon as a chunk
                # arrives if the user clicked Stop during the previous
                # chunk's processing.
                if abort_event is not None and abort_event.is_set():
                    log.info(f"[call_llama] abort_event set between chunks "
                             f"iter={iteration}")
                    return StreamOutcome(is_terminal=True, terminal=None)
                # SSE framing stays here, NOT in feed(): the accumulator
                # parses JSON objects, and the line protocol is the
                # transport's business. The first draft of block C2
                # dropped all three guards below, which sent the
                # "[DONE]" sentinel into json.loads on every stream.
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                d = acc.feed(chunk)
                if d.grew_reasoning:
                    await push_thinking(
                        accumulated_reasoning + acc.reasoning,
                        thinking_msg=thinking_msg, silent=silent,
                        throttle=throttle)
                reasoning_buf = acc.reasoning
                content_buf = acc.content
                tool_calls_buf = acc.tool_calls
                if d.finish_reason:
                    finish_reason = d.finish_reason
            # Final flush: ensure the latest reasoning text is on Telegram
            if reasoning_buf:
                await push_thinking(
                    accumulated_reasoning + reasoning_buf,
                    thinking_msg=thinking_msg, silent=silent,
                    throttle=throttle, force=True)
    except httpx.HTTPError as e:
        log.info(f"[stream err iter={iteration}] {type(e).__name__}: {e}; "
                 f"falling back to non-streaming")
        return await _fallback_to_nonstream(
            req_body, iteration=iteration, stream_err=e,
            saw_unavailable_tool=saw_unavailable_tool,
            final_fallback=final_fallback, finish_reason=finish_reason,
            tool_calls_buf=tool_calls_buf)

    return StreamOutcome(
        content_buf=content_buf,
        reasoning_buf=reasoning_buf,
        tool_calls_buf=tool_calls_buf,
        finish_reason=finish_reason,
        saw_unavailable_tool=saw_unavailable_tool,
        final_fallback=final_fallback,
        # append_msg stays None: the shared code below the branch builds
        # the assistant message from content_buf and appends it when
        # there are tool calls. Only the FALLBACK appends here, because
        # it is the one that produces a `message` object rather than
        # buffers. Writing an append for the streaming path would double
        # the assistant turn in the history.
    )