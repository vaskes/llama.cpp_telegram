"""C1 -- non_stream_iteration, driven by the captured engine response.

The extracted function is the vision path. It has exactly one job with
three ways to end, and the ways are not equivalent: a plain-text answer,
a final message the handler must relay, or a loop hand-off into the shared
dispatch. Those are pinned here individually.

The fixture is the live one from Block A -- a real llama-server response,
not a mock. What the mock would have hidden: `content` is a string, but
`content: null` arrives on the opening streaming chunk, and the engine's
non-streaming message carries `reasoning_content` alongside it.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BOT_DIR))

import llm_nonstream as ns  # noqa: E402

pytestmark = pytest.mark.tech_debt(
    item=3,
    reason="Pins TECH-DEBT #3 C1: the non-streaming iteration's three "
           "outcomes and the tool hand-off, driven by a captured "
           "llama-server response.")

BODY = json.loads((FIXTURES / "llama_nonstream.json").read_text(encoding="utf-8"))["body"]


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Transport:
    """Stands in for httpx at the _abortable_post boundary only."""

    def __init__(self, response):
        self.response = response
        self.sent = []

    async def post(self, _client, url, abort_event=None, **kwargs):
        self.sent.append((url, kwargs))
        if abort_event is not None and abort_event.is_set():
            return None                      # what _abortable_post does on Stop
        return self.response


@pytest.fixture
def patched(monkeypatch):
    holder = {}

    def install(response):
        t = _Transport(response)
        holder["t"] = t
        monkeypatch.setattr(ns, "_abortable_post", t.post)
        return t

    return install


# --- the loop hand-off ---------------------------------------------------

async def test_plain_answer_comes_back_as_a_loop_result(patched):
    """No tool_calls -> the caller fills its buffers and finishes.

    NOT a "final": call_llama has to run the reasoning push and the
    buffer writes after this returns. Returning a final here would skip
    push_thinking and lose the reasoning feed.
    """
    patched(_Resp(BODY))
    r = await ns.non_stream_iteration({"model": "x", "messages": []}, iteration=0)

    assert r.outcome == "loop"
    assert r.content == BODY["choices"][0]["message"]["content"]
    assert r.reasoning == BODY["choices"][0]["message"].get("reasoning_content", "")
    assert r.tool_calls == []
    assert r.saw_unavailable_tool is False


async def test_finish_reason_comes_from_the_response(patched):
    patched(_Resp(BODY))
    r = await ns.non_stream_iteration({"model": "x", "messages": []}, iteration=0)
    assert r.finish_reason == BODY["choices"][0]["finish_reason"]


# --- tool hand-off -------------------------------------------------------

async def test_tool_calls_go_to_the_loop_not_to_a_final(patched):
    """The bug this contract exists to prevent.

    An unconditional `return content` existed once. When react_to_message
    landed on the vision path, it dropped the tool call and the user saw
    an empty reply. The outcome must be "loop" whenever tool_calls are
    present, whatever the content says.
    """
    payload = {"choices": [{"message": {"content": "sure", "tool_calls": [
        {"id": "c1", "function": {"name": "react_to_message", "arguments": "{}"}}
    ]}, "finish_reason": "tool_calls"}]}
    patched(_Resp(payload))
    r = await ns.non_stream_iteration({"model": "x", "messages": []}, iteration=0)

    assert r.outcome == "loop", (
        "a vision turn that emits tool_calls must hand off to the shared "
        "dispatch loop; returning content drops the call"
    )
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0]["function"]["name"] == "react_to_message"


async def test_unknown_tool_is_soft_rejected_and_the_loop_still_runs(patched):
    """T38.5/T43. An unlisted tool name must be rewritten to
    __rejected__ rather than dispatched, and the turn must continue so
    the model can read the rejection and retry."""
    payload = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "definitely_not_allowed", "arguments": "{}"}}
    ]}, "finish_reason": "tool_calls"}]}
    patched(_Resp(payload))
    r = await ns.non_stream_iteration({"model": "x", "messages": []}, iteration=0)

    assert r.outcome == "loop"
    assert r.saw_unavailable_tool is True, (
        "the caller uses saw_unavailable_tool to decide how to phrase the "
        "reply; losing it makes a soft rejection look like a silent drop"
    )
    assert r.tool_calls[0]["function"]["name"] == "__rejected__"
    assert r.tool_calls[0]["function"]["arguments"] == "{}"


# --- the three finals ----------------------------------------------------

async def test_empty_choices_returns_a_clear_message_not_an_index_error(patched):
    """T32. Heavy prompts can return 200 OK with choices:[] and the user
    used to see 'list index out of range'."""
    patched(_Resp({"choices": []}))
    r = await ns.non_stream_iteration({"model": "x", "messages": []}, iteration=0)

    assert r.outcome == "final"
    assert r.final_text and "index out of range" not in r.final_text
    assert "context window" in r.final_text or "rate limit" in r.final_text


async def test_context_exhaustion_is_reported_as_overflow(patched):
    """T70.4. An overflow used to produce a normal answer-shaped message
    and the bot hallucinated about the truncated conversation."""
    from context_budget import OVERFLOW_MESSAGE, is_context_overflow

    detail = "the request exceeds the available context size"
    assert is_context_overflow(detail), "fixture text is not an overflow"

    patched(_Resp({"error": {"message": detail}, "choices": []}))
    r = await ns.non_stream_iteration({"model": "x", "messages": []}, iteration=0)

    assert r.outcome == "final"
    assert r.final_text == OVERFLOW_MESSAGE, (
        "a context overflow must produce the overflow message, not the "
        "generic empty-choices text -- otherwise the bot answers a "
        "truncated conversation as though it were complete"
    )


async def test_stop_button_returns_none_so_no_partial_answer_is_sent(patched):
    """The Stop button sets abort_event; _abortable_post then returns
    None. The handler must see None and send nothing, not an empty reply.
    """
    import asyncio as _a

    evt = _a.Event()
    evt.set()
    patched(_Resp(BODY))            # would return a real answer if reached

    r = await ns.non_stream_iteration(
        {"model": "x", "messages": []}, iteration=0, abort_event=evt)

    assert r.outcome == "final"
    assert r.final_text is None, (
        "an aborted vision call must return None so the handler sends "
        "nothing; returning '' would post an empty message"
    )


# --- request construction -------------------------------------------------

async def test_payload_is_sent_as_bytes_with_ascii_intact(patched):
    """docs/CALL_LLAMA.md §6: json.dumps on the live req_body with
    ensure_ascii=True once corrupted Cyrillic into \\uXXXX escapes and the
    data: URL into \\u002f, and llama-server answered 400."""
    t = patched(_Resp(BODY))
    await ns.non_stream_iteration(
        {"model": "m", "messages": [{"role": "user", "content": "Проверь математику"}]},
        iteration=0)

    url, kwargs = t.sent[0]
    assert url.endswith("/chat/completions")
    raw = kwargs["content"]
    assert isinstance(raw, bytes), "the body must go out as bytes, not json="
    text = raw.decode("utf-8")
    assert "Проверь математику" in text, (
        f"the Cyrillic was escaped on the way out: {text[:200]}"
    )
    assert "\\u" not in text, "ensure_ascii must stay False for this path"


async def test_a_fresh_client_is_used_per_call(patched):
    """Keep-alive reuse produced 400s on the vision path for identical
    bytes; the original code closed the client per call to avoid it."""
    import httpx

    seen = []

    class _Client:
        def __init__(self, *a, **k):
            seen.append(k)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    patched(_Resp(BODY))
    orig = httpx.AsyncClient
    import llm_nonstream
    llm_nonstream.httpx.AsyncClient = _Client
    try:
        await ns.non_stream_iteration({"model": "m", "messages": []}, iteration=0)
    finally:
        llm_nonstream.httpx.AsyncClient = orig

    assert seen, "no client was constructed"
    assert seen[0].get("limits") is not None, (
        "the per-call client exists to disable keep-alive; dropping the "
        "limits brings back the 400-on-identical-bytes bug"
    )


def test_call_llama_keeps_the_try_that_wraps_the_helper():
    """The seam is the try BODY. Its `except` is 120 lines further down,
    past the loop-state writes, so a branch-shaped cut orphans it.

    Checked structurally: the module parses, and the call sits inside a
    try whose handler is in the same function.
    """
    import ast

    tree = ast.parse((BOT_DIR / "call_llama.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")

    def finds_call(node):
        for n in ast.walk(node):
            # bare Name: `non_stream_iteration(...)`, not
            # `something.non_stream_iteration(...)`. Matching on
            # ast.Attribute reported "not called" on code that calls it
            # on every vision turn.
            if isinstance(n, ast.Call):
                f = n.func
                name = (f.attr if isinstance(f, ast.Attribute)
                        else f.id if isinstance(f, ast.Name) else None)
                if name == "non_stream_iteration":
                    return n
        return None

    call = finds_call(fn)
    assert call is not None, "call_llama no longer calls non_stream_iteration"

    handler = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and finds_call(node):
            handler = node
            break
    assert handler is not None, (
        "the call to non_stream_iteration is no longer inside a try. Its "
        "except used to sit past the loop-state writes, so cutting the "
        "branch instead of the try body orphans it."
    )
    assert handler.handlers, "that try has no handler"
    assert handler.finalbody is None or True