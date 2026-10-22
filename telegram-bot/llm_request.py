"""Engine request construction and diagnostics. TECH-DEBT #8, final block.

Moved out of `call_llama` lines 487-556: building `req_body`, the
request-shape logging, and the per-iteration reset of the stream
accumulators.

Why this block and not the two beside it. The loop body is three
pieces:

    487-556   this module: build the request, log it, reset state
    558-640   non-streaming POST   -> llm_nonstream.py already owns it
    641-770   streaming POST       -> llm_sse.py already owns it

This was the last piece still inline, and it is the safest of the three
to move: it contains no `return`, no await, and no I/O. It builds one
dict and writes to the log. The other two are where the 13 returns and
the mixed "final answer" / "engine response, continue" semantics live,
and that is a different piece of work with a different risk.

The seam that bit last time. `saw_unavailable_tool` became a local of an
extracted module and existed only on its error path, giving
UnboundLocalError on every photo call. So this module has no module-level
mutable state, no `locals()` introspection, and returns everything the
caller needs through a frozen dataclass rather than by assignment.
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from llm_json import dumps_body
from logconf import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class StreamAccumulators:
    """The per-iteration state a stream branch reads and writes.

    Frozen because the extraction is the point: the previous code
    assigned these four names directly in the loop body, and a caller
    that got them back as a tuple had to remember which was which. Named
    fields make a mismatch a TypeError at the seam instead of a silent
    swap at line 553.
    """
    reasoning_buf: str = ""
    content_buf: str = ""
    tool_calls_buf: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None


def build_request_body(
    model: str,
    msgs: List[Dict[str, Any]],
    max_tokens: int,
    use_stream: bool,
    req_tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The exact dict POSTed to the engine.

    `req_tools is not None` rather than truthiness is deliberate and is
    preserved from the original: an empty list means "the caller
    decided there are no tools" and must still emit the three tool keys,
    while None means "not a tools request" and must not.

    `msgs` is aliased into the result, not copied -- also preserved.
    The inline version this replaces did the same, and a copy is a
    behaviour change, which is the one thing a move must not be. Not
    reachable in practice: the loop appends to `msgs` only between
    iterations and rebuilds req_body at the top of each one.
    """
    req_body: Dict[str, Any] = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_tokens,
        "stream": use_stream,
    }
    if req_tools is not None:
        req_body["tools"] = req_tools
        req_body["tool_choice"] = "auto"
        req_body["parallel_tool_calls"] = False
    return req_body


def log_request_shape(
    req_body: Dict[str, Any],
    iteration: int,
    req_tools: Optional[List[Dict[str, Any]]],
    use_stream: bool,
) -> None:
    """Log the request's structure. Never its bytes.

    The `dumps_body` call here serialises to COUNT bytes and to take a
    400-byte head when debug is on. It deliberately does not log the
    result: for a vision request that would be the base64 image, which is
    PII and would land in journald.

    W2.1 removed a deepcopy of the whole req_body that existed only to
    mask image bytes for a log line the debug print never used. Do not
    reintroduce a copy of `req_body` here -- on a 10-iteration run that
    was ten times the full request size for nothing.
    """
    if iteration == 0 and not use_stream:
        try:
            raw = dumps_body(req_body)
            log.info(f"[LLAMA] iter={iteration} payload_bytes={len(raw)} (image bytes masked)")
            if os.environ.get("LLAMABOT_DEBUG_PAYLOAD") == "1":
                head = raw[:400].decode('utf-8', errors='replace')
                # Find the column where the bad JSON starts by scanning
                # for known key patterns. Most useful for diagnosing
                # 500 from llama-server: this prints the head so we can
                # see what we sent before the parser gave up.
                log.info(f"[LLAMA-DEBUG] iter={iteration} first 400 bytes:\n{head}")
        except Exception as e:
            log.info(f"[LLAMA] iter=0 cannot size payload: {e}")

    _msgs = req_body.get("messages", [])
    _imgs = sum(
        1 for m in _msgs
        if isinstance(m.get("content"), list)
        for it in m["content"]
        if isinstance(it, dict) and "image_url" in it
    )
    log.info(f"[LLAMA] iter={iteration} req_body_keys={list(req_body.keys())} "
             f"tools={'yes ('+str(len(req_tools))+' defs)' if req_tools else 'no'} "
             f"msgs_count={len(_msgs)} image_msgs={_imgs}")

    if iteration == 0:
        # On first iter, also print the first AND last user message
        # structure -- the first to confirm history is loaded, the last
        # to confirm the current speaker was tagged "From: <name>: ".
        user_msgs = [m for m in req_body.get("messages", [])
                     if m.get("role") == "user"]
        if not user_msgs:
            return
        m0 = user_msgs[0]
        c0 = m0.get("content")
        if isinstance(c0, list):
            log.info(f"[LLAMA]   first user msg content types: "
                     f"{[item.get('type') for item in c0 if isinstance(item, dict)]}")
        else:
            log.info(f"[LLAMA]   first user msg content head: {str(c0)[:150]!r}")
        ml = user_msgs[-1]
        cl = ml.get("content")
        if isinstance(cl, list):
            first_part = cl[0] if cl else {}
            first_text = first_part.get("text", "") if isinstance(first_part, dict) else ""
            log.info(f"[LLAMA]   last user msg first text part head: {first_text[:150]!r}")
        else:
            log.info(f"[LLAMA]   last user msg content head: {str(cl)[:150]!r}")


def new_stream_accumulators() -> StreamAccumulators:
    """Reset the four per-iteration names the stream branches share."""
    return StreamAccumulators(
        reasoning_buf="",
        content_buf="",
        tool_calls_buf={},
        finish_reason=None,
    )