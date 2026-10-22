"""Golden master for the engine request body. TECH-DEBT #8, final block.

`llm_request.build_request_body` replaces the dict literal that used to
be inline in `call_llama`. The dict is the entire contract between the
bot and llama-server: change a key and the engine either 400s or, worse,
silently changes behaviour.

So the property is pinned by value, not by shape. The expected bodies
below were captured from the inline version before extraction; if this
test passes after the move, the request the bot sends is byte-identical.

Two things this deliberately does NOT do, both learned here:

  * It does not compare keys in order. Dict ordering is not a contract
    with an HTTP server and pinning it would redden on a harmless
    reordering.
  * It does not call the live engine. `tests/capture_sse_golden.py`
    captures; this replays the capture. A CI run that reached for a
    running llama-server would fail for reasons that have nothing to do
    with the code under test.
"""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

from llm_request import (StreamAccumulators, build_request_body,
                         new_stream_accumulators)

MODEL = "test-model"
MSGS = [{"role": "system", "content": "s"},
        {"role": "user", "content": [{"type": "text", "text": "hi"},
                                     {"type": "image_url",
                                      "image_url": {"url": "data:...;base64,AAA"}}]}]


def test_plain_request_without_tools():
    body = build_request_body(model=MODEL, msgs=MSGS, max_tokens=512,
                              use_stream=True, req_tools=None)
    assert body == {
        "model": "test-model",
        "messages": MSGS,
        "max_tokens": 512,
        "stream": True,
    }
    assert "tools" not in body
    assert "tool_choice" not in body
    assert "parallel_tool_calls" not in body


def test_empty_tool_list_is_not_the_same_as_none():
    """The `is not None` check, pinned.

    `req_tools=[]` means "this is a tools request with no tools" and must
    still carry the three tool keys, because llama-server decides
    whether to expect a tools array from their presence. Folding that
    into a truthiness test is a one-character edit that changes what the
    engine sees.
    """
    tools = [{"type": "function", "function": {"name": "x"}}]
    with_tools = build_request_body(model=MODEL, msgs=MSGS, max_tokens=1,
                                    use_stream=False, req_tools=tools)
    assert with_tools["tools"] == tools
    assert with_tools["tool_choice"] == "auto"
    assert with_tools["parallel_tool_calls"] is False

    empty = build_request_body(model=MODEL, msgs=MSGS, max_tokens=1,
                               use_stream=False, req_tools=[])
    assert empty["tools"] == []
    assert empty["tool_choice"] == "auto"
    assert empty["parallel_tool_calls"] is False


def test_stream_flag_is_carried_through_verbatim():
    for flag in (True, False):
        body = build_request_body(model=MODEL, msgs=MSGS, max_tokens=7,
                                  use_stream=flag, req_tools=None)
        assert body["stream"] is flag


def test_the_message_list_is_deliberately_aliased():
    """build_request_body aliases the caller's list. That is the contract.

    My first version of this file asserted the opposite -- a shallow copy
    -- and it failed, because the inline version it replaced assigned
    `msgs` straight into the dict. The docstring above had already
    claimed the copy was "what the original produced", which was simply
    untrue.

    Kept as-is on purpose. Making it a copy is a one-line improvement and
    also a behaviour change, and "move the block" is the whole claim of
    this extraction. The aliasing is not reachable in practice: `msgs` is
    appended to only between iterations, and req_body is rebuilt from it
    at the top of every iteration, so no request is ever POSTed after its
    message list has moved on.

    Asserted explicitly so that if someone later adds the copy, this
    test says so rather than letting the change ride along undocumented.
    """
    msgs = [{"role": "user", "content": "a"}]
    body = build_request_body(model=MODEL, msgs=msgs, max_tokens=1,
                              use_stream=True, req_tools=None)
    assert body["messages"] is msgs, \
        "build_request_body now copies the message list -- if that was " \
        "deliberate, update this test AND the module docstring together"


def test_new_accumulators_are_at_the_documented_starting_point():
    acc = new_stream_accumulators()
    assert acc.reasoning_buf == ""
    assert acc.content_buf == ""
    assert acc.tool_calls_buf == {}
    assert acc.finish_reason is None


def test_accumulators_are_fresh_each_call():
    """The saw_unavailable_tool lesson, applied to the reset.

    Two calls must not share the dict. The original code assigned a fresh
    `{}` on every iteration; a module-level default would be the same
    class of bug -- state that exists on one path only.
    """
    a = new_stream_accumulators()
    a.tool_calls_buf["x"] = 1
    b = new_stream_accumulators()
    assert b.tool_calls_buf == {}, "the accumulator dict is shared between calls"
    assert a.tool_calls_buf == {"x": 1}


def test_the_dataclass_is_frozen():
    acc = new_stream_accumulators()
    with pytest.raises(Exception):
        acc.content_buf = "written"


def test_dataclass_default_and_factory_differ_documentedly():
    """The dataclass default is None; the factory returns {}.

    Worth pinning because it looks like an inconsistency and is not:
    dataclasses reject a mutable default, so the field default has to
    be None. The factory is the only supported constructor and always
    yields {}. Without this test the next reader "fixes" the default to
    {} and gets ValueError at import.
    """
    direct = StreamAccumulators()
    factory = new_stream_accumulators()
    assert direct.tool_calls_buf is None
    assert factory.tool_calls_buf == {}
    for field in ("reasoning_buf", "content_buf", "finish_reason"):
        assert getattr(direct, field) == getattr(factory, field)
