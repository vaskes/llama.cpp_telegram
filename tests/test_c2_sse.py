"""C2 -- StreamAccumulator, driven by the captured engine stream.

These are the tests that had to be possible to write. Before the
extraction the merge logic lived inline in an `async for` inside a
`try` inside an `if`, so checking a tool call's arguments were reassembled
correctly meant building a fake httpx response, an abort event, a
push_thinking callback and an event loop. Now it is a dict and a loop.

The fixtures are the live ones from Block A, so the assertions are about
what llama-server actually emits -- including the two things that look
like bugs and are not:

  the opening chunk sends `content: null`, and a tool call's name
  arrives alone in the chunk that opens its index, with arguments
  dribbled out one or two characters at a time.
"""
import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BOT_DIR))

from llm_sse import StreamAccumulator  # noqa: E402

pytestmark = pytest.mark.tech_debt(
    item=3,
    reason="Pins TECH-DEBT #3 C2: SSE delta merging, replayed from a "
           "captured llama-server stream.")

STREAM = json.loads((FIXTURES / "llama_stream.json").read_text(encoding="utf-8"))
TOOL_STREAM = json.loads(
    (FIXTURES / "llama_stream_toolcalls.json").read_text(encoding="utf-8"))
NONSTREAM = json.loads(
    (FIXTURES / "llama_nonstream.json").read_text(encoding="utf-8"))


def chunks(doc):
    """The `data: {...}` payloads, minus the [DONE] sentinel.

    Mirrors what the caller hands to feed(). The sentinel is excluded
    here exactly as the caller's `payload.strip() == "[DONE]"` check
    excludes it -- feeding it is a JSONDecodeError in production.
    """
    out = []
    for raw in doc["raw_lines"]:
        if not raw.startswith("data: "):
            continue
        body = raw[6:]
        if body.strip() == "[DONE]":
            continue
        out.append(json.loads(body))
    return out


def replay(doc):
    acc = StreamAccumulator()
    for c in chunks(doc):
        acc.feed(c)
    return acc


# --- the captured streams -------------------------------------------------

def test_the_fixture_really_opens_with_a_null_content():
    """The assertion the other tests lean on.

    If this ever stops holding, the `if cc:` check below is no longer
    being exercised and a test that claims to cover it is lying.
    """
    first = chunks(STREAM)[0]["choices"][0]["delta"]
    assert "content" in first
    assert first["content"] is None, (
        "the fixture no longer opens with content: null, so the "
        "truthiness check is no longer under test"
    )


def test_the_fixture_splits_arguments_not_the_name():
    """What the capture actually shows, recorded so the next reader
    does not have to re-derive it.

    llama-server sends the tool name whole in the chunk that opens the
    index, then dribbles the arguments out in six fragments. So this
    fixture proves nothing about `name` using `+=` -- that distinction is
    covered synthetically in
    test_a_split_tool_name_is_concatenated_not_replaced, and saying
    otherwise here would be a claim the capture does not support.
    """
    names, args = [], []
    for c in chunks(TOOL_STREAM):
        for ch in c.get("choices") or []:
            for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name"):
                    names.append(fn["name"])
                if fn.get("arguments"):
                    args.append(fn["arguments"])

    assert names == ["get_weather"], f"unexpected name deltas: {names}"
    assert len(args) >= 3, f"arguments were not fragmented: {args}"
    assert json.loads("".join(args)) == {"city": "Moscow"}


def test_streamed_and_non_streamed_answers_agree():
    """The strongest available check: the same prompt, two transports.

    If merging dropped or duplicated a delta, the streamed content
    diverges from the non-streaming content for the same request. This
    is the property that actually matters, and it is not visible from
    any single-chunk assertion.
    """
    streamed = replay(STREAM).content
    direct = NONSTREAM["body"]["choices"][0]["message"]["content"]
    assert streamed == direct, (
        f"streamed {streamed!r} != non-streamed {direct!r}"
    )
    assert streamed, "the fixture produced no content at all"


def test_reasoning_accumulates_in_stream_order():
    """Order is the property, not equality with another run.

    The first draft compared this against the non-streamed response and
    it failed -- correctly. The two fixtures are two separate engine
    invocations of the same prompt, so the reasoning differs word for
    word. The final answer matched; the scratchpad behind it did not and
    never had to.

    What actually matters is that the fragments land in the order the
    server sent them. Reversing them still produces one long string.
    """
    fragments = []
    for c in chunks(STREAM):
        for ch in c.get("choices") or []:
            rc = (ch.get("delta") or {}).get("reasoning_content")
            if rc:
                fragments.append(rc)

    assert len(fragments) > 1, "the fixture does not split reasoning at all"
    assert replay(STREAM).reasoning == "".join(fragments)
    assert "".join(fragments) != "".join(reversed(fragments)), (
        "order is indistinguishable here, so this assertion is vacuous"
    )


def test_finish_reason_is_captured():
    """Both channels: the Delta the caller reads, and the attribute.

    The attribute was the dead one. It is returned in the Delta and never
    stored, so `acc.finish_reason` was None on every stream while the
    production caller kept working off the Delta. A live attribute that
    never updates is a trap for the next reader.
    """
    acc = replay(STREAM)
    assert acc.finish_reason == "stop", "the stored attribute stayed None"
    assert acc.feed({"choices": [{"delta": {"content": "x"}}]}).finish_reason is None, \
        "a chunk without finish_reason must not clear the stored value"


def test_finish_reason_arrives_only_on_its_own_trailing_chunk():
    """Why self.finish_reason is assigned inside feed() and not read back
    per chunk: the server sends it exactly once, at the end."""
    seen = [c["choices"][0].get("finish_reason") for c in chunks(STREAM)
            if c.get("choices")]
    non_null = [f for f in seen if f]
    assert non_null == ["stop"], f"expected one terminal finish_reason, got {non_null}"


# --- tool call reassembly -------------------------------------------------

def test_fragmented_arguments_reassemble_into_valid_json():
    """The one that matters most. `{"city":"Moscow"}` arriving as six
    fragments has to come out parseable, or every tool call on the
    streaming path fails at dispatch."""
    acc = replay(TOOL_STREAM)
    calls = acc.tool_calls_list()
    assert calls, "the tool fixture produced no tool calls"

    for call in calls:
        args = call["arguments"]
        assert args, "arguments came back empty"
        parsed = json.loads(args)          # raises if fragments were lost
        assert isinstance(parsed, dict)
        assert call["name"], "the tool name did not survive the merge"
        assert call["id"], "the tool id did not survive the merge"


def test_a_split_tool_name_is_concatenated_not_replaced():
    """`+=` versus `=` for the name, tested directly.

    OpenAI's own servers send the name in one piece, and so does
    llama-server -- see the capture above. But the streaming protocol
    permits fragmenting it, and a `=` here silently truncates the name
    to its last chunk, which then fails to match a known tool and gets
    soft-rejected. Synthetic on purpose: the live capture cannot produce
    this case, so a fixture-only test would leave the branch untested.
    """
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "get_wea"}}]}}]})
    acc.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "ther"}}]}}]})

    assert acc.tool_calls_list()[0]["name"] == "get_weather", (
        "a name split across chunks must reassemble; `=` would leave 'ther'"
    )
    assert acc.tool_calls_list()[0]["id"] == "c1", (
        "id arrives once and must survive the later fragment"
    )


def test_tool_calls_are_ordered_by_index_not_by_arrival():
    """Parallel tool calls: index 1 can be fully emitted before index 0
    has finished. The caller iterates the list to build tool messages,
    so ordering is a contract."""
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 1, "id": "b", "function": {"name": "second"}},
    ]}}]})
    acc.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "a", "function": {"name": "first"}},
    ]}}]})

    assert [c["name"] for c in acc.tool_calls_list()] == ["first", "second"], (
        "index order is the contract; arrival order would pair tool "
        "results with the wrong call"
    )


# --- edge cases the wire actually produces --------------------------------

def test_a_chunk_with_no_choices_does_not_raise():
    """llama-server emits a usage-only object at the end of some
    responses. Raising on it would discard the entire reply."""
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"content": "hi"}}]})
    acc.feed({"usage": {"total_tokens": 12}})       # no choices at all
    acc.feed({})                                    # nor this
    assert acc.content == "hi"


def test_empty_and_null_deltas_do_not_grow_the_buffers():
    acc = StreamAccumulator()
    for chunk in ({"choices": [{"delta": {}}]},
                  {"choices": [{"delta": {"content": None}}]},
                  {"choices": [{"delta": {"content": ""}}]},
                  {"choices": [{}]}):
        acc.feed(chunk)
    assert acc.content == ""
    assert acc.reasoning == ""
    assert acc.tool_calls == {}


def test_grew_reasoning_is_true_only_on_reasoning_deltas():
    """This flag gates an awaited Telegram edit per delta. Firing it on
    content-only chunks means an API call per token."""
    acc = StreamAccumulator()
    assert acc.feed({"choices": [{"delta": {"content": "a"}}]}).grew_reasoning is False
    assert acc.feed({"choices": [{"delta": {"reasoning_content": "t"}}]}).grew_reasoning is True
    assert acc.feed({"choices": [{"delta": {"content": "b"}}]}).grew_reasoning is False


def test_a_non_string_content_is_skipped_not_concatenated():
    """What actually separates `if cc:` from `if cc is not None:`.

    Measured, not assumed: for every falsy string the two are identical
    because `+= ""` is a no-op, so the opening `content: null` does not
    distinguish them and a test built on that chunk would pass either
    way. A server sending `content: 0` does distinguish them, and the
    wrong one raises TypeError inside the stream loop -- discarding
    every token received so far.
    """
    acc = StreamAccumulator()
    acc.feed({"choices": [{"delta": {"content": "keep"}}]})
    for junk in (0, [], {}, False):
        acc.feed({"choices": [{"delta": {"content": junk}}]})
    assert acc.content == "keep"


def test_reusing_one_accumulator_across_iterations_would_leak():
    """Not a bug in the accumulator -- a documented trap.

    call_llama creates a fresh one per streaming block. If someone
    hoists it to reuse it, iteration 2 would answer with iteration 1's
    text. Pins the reason the instantiation sits inside the block.
    """
    first = replay(STREAM)
    assert first.chunk_count == len(chunks(STREAM))
    second = StreamAccumulator()
    assert second.content == "" and second.chunk_count == 0


# --- the SSE framing stayed behind ----------------------------------------

def test_the_data_prefix_and_done_sentinel_are_handled_by_the_caller():
    """feed() takes parsed chunks. The line protocol is the transport's
    business.

    The first draft of this extraction folded `json.loads(line[6:])`
    straight into the call and deleted all three framing guards, so
    `[DONE]` was parsed as JSON on every single stream. Nothing failed
    loudly -- the suite was green -- until a real reply came through.
    """
    import ast

    tree = ast.parse((BOT_DIR / "call_llama.py").read_text(encoding="utf-8"))

    # A substring check for "[DONE]" is not a check. Deleting the guard
    # leaves the word in the comment above it, so the first version of
    # this test stayed green with the sentinel parsing as JSON on every
    # stream. It has to be the break statement, in the right place.
    call = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")
    loop = next(n for n in ast.walk(call)
                if isinstance(n, ast.AsyncFor) and "aiter_lines" in ast.dump(n))

    def line_of(node):
        # ctx nodes (Store/Del) carry no lineno, and ast.walk yields them.
        return getattr(node, "lineno", 10 ** 9)

    fed = [n for n in ast.walk(loop) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == "feed"]
    assert fed, "the loop no longer feeds the accumulator"
    fed_at = line_of(fed[0])

    breaks = [b for b in ast.walk(loop)
              if isinstance(b, ast.Break) and line_of(b) < fed_at]
    assert breaks, (
        "no `break` before acc.feed(): the [DONE] sentinel would be "
        "handed to json.loads on every stream"
    )
    # ...and the break it guards must be the sentinel comparison.
    # ast.dump takes ONE node; handing it a list raised TypeError, which
    # the mutation harness had been scoring as a detection.
    before_feed = " ".join(
        ast.dump(n) for n in ast.walk(loop) if line_of(n) < fed_at
    )
    assert "DONE" in before_feed, (
        "the break before feed() is not guarding the [DONE] sentinel"
    )

    # the prefix guard and the decode guard, structurally
    dumps = ast.dump(loop)
    assert 'data: ' in dumps, "the data: prefix guard is gone"
    assert "JSONDecodeError" in dumps, "malformed chunk guard is gone"

    # and the accumulator itself must not know about the wire format
    assert "data:" not in (BOT_DIR / "llm_sse.py").read_text(encoding="utf-8")


def test_a_done_sentinel_would_break_a_naive_replay():
    """Positive control.

    Every replay helper above strips `[DONE]` by hand. If that strip were
    removed, does anything go red? If not, the tests above are not
    testing the framing after all -- they are testing the helper.
    """
    body = json.loads((FIXTURES / "llama_stream.json").read_text(encoding="utf-8"))
    sentinel = next((r for r in body["raw_lines"]
                     if r.startswith("data: ") and r[6:].strip() == "[DONE]"), None)
    assert sentinel is not None, "the fixture has no [DONE]; the control is moot"

    with pytest.raises(json.JSONDecodeError):
        json.loads(sentinel[6:])


def test_the_streaming_loop_feeds_the_accumulator_it_creates():
    """The accumulator is created inside the streaming block, not hoisted.

    Hoisting it reuses buffers across iterations and iteration 2 answers
    with iteration 1's text.
    """
    src = (BOT_DIR / "call_llama.py").read_text(encoding="utf-8")
    assert src.count("acc = StreamAccumulator()") == 1
    loop_at = src.index("aiter_lines()")
    created_at = src.index("acc = StreamAccumulator()")
    assert created_at < loop_at, (
        "the accumulator must be created before the loop that feeds it"
    )