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
from typing import NamedTuple

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
