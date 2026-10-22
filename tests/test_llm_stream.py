"""llm_stream.stream_iteration -- the streaming branch, driven directly.

TECH-DEBT #8 block 3. Before this module existed the branch was a
`for` loop with six `return`s inside `call_llama`, and nothing drove it.
`test_c2_sse` covered `StreamAccumulator` and `test_c2_golden` covered
the accumulator against a real captured stream, but the glue around it --
the two abort checks, the push_thinking wiring, the framing, the
fallback -- was reachable only by running a whole tool loop.

That is why block C2 could delete all three SSE framing guards with a
green suite, and why this file exists now.

The property this file is really about

Both abort paths end with `None`. A caller written as

    if outcome.terminal is not None:
        return outcome.terminal

treats "the user pressed Stop" as "keep going", and the loop carries on
with whatever the previous iteration left in the buffers -- answering a
cancelled question with the model's earlier text. So every test below
that exercises an abort asserts `is_terminal` and the value separately.
Inferring one from the other is the bug this module's docstring names.
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

import llm_stream as ls  # noqa: E402

pytestmark = pytest.mark.tech_debt(
    item=8,
    reason="Pins TECH-DEBT #8 block 3: the streaming branch's six exits, "
           "its SSE framing, and the non-streaming fallback it owns.")


# --- doubles ---------------------------------------------------------


class _Resp:
    """What `client.stream(...)` yields."""

    def __init__(self, lines, fail=False):
        self._lines = lines
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise httpx.HTTPError("stream died mid-flight")

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *_exc):
        return False


class _Client:
    def __init__(self, lines, fail=False):
        self._resp = _Resp(lines, fail)

    def stream(self, *_a, **_kw):
        return _Ctx(self._resp)


def _lines(*chunks, done=True):
    out = [f"data: {json.dumps(c)}" for c in chunks]
    if done:
        out.append("data: [DONE]")
    return out


def _chunk(content=None, reasoning=None, finish=None, tools=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tools is not None:
        delta["tool_calls"] = tools
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


class _Pushes:
    def __init__(self):
        self.calls = []

    async def __call__(self, text, *, thinking_msg, silent, throttle,
                       force=False):
        self.calls.append({"text": text, "silent": silent,
                           "throttle": throttle, "force": force})


def _abort():
    class _Ev:
        def is_set(self):
            return True
    return _Ev()


class _LateAbort:
    """Set only after the first check, so the pre-stream guard passes.

    My first version of the between-chunk test used the always-set
    event, which returned at the PRE-STREAM check -- the between-chunk
    branch was never executed and the test passed on a mutation that
    removed `is_terminal=True` from exactly that return. A test that
    cannot reach the line it names is a comment.
    """

    def __init__(self, after=1):
        self._n = 0
        self._after = after

    def is_set(self):
        self._n += 1
        return self._n > self._after


async def _run(lines, *, abort=None, pushes=None, client=None, monkeypatch=None,
               tool_calls_buf=None, finish_reason="", final_fallback=None,
               saw_unavailable_tool=False):
    pushes = pushes or _Pushes()
    if monkeypatch is not None:
        monkeypatch.setattr(ls, "push_thinking", pushes)
    outcome = await ls.stream_iteration(
        client or _Client(lines), {"model": "m", "messages": []},
        iteration=3, abort_event=abort, accumulated_reasoning="prior ",
        thinking_msg=None, silent=False, throttle=object(),
        tool_calls_buf=tool_calls_buf, finish_reason=finish_reason,
        final_fallback=final_fallback,
        saw_unavailable_tool=saw_unavailable_tool)
    return outcome, pushes


# --- the exits -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_normal_stream_is_not_terminal_and_carries_the_text():
    out, _ = await _run(_lines(_chunk(content="привет"),
                                _chunk(finish="stop")))
    assert not out.is_terminal
    assert out.content_buf == "привет"
    assert out.finish_reason == "stop"
    assert out.append_msg is None, (
        "the streaming path does not append; the shared code below the "
        "branch builds the assistant message itself")


@pytest.mark.asyncio
async def test_the_pre_stream_abort_is_terminal_even_though_the_text_is_none():
    """The Stop button, before the first chunk.

    `terminal` is None because there is no reply to send. `is_terminal`
    is True because the turn is over. Checking the first instead of the
    second is how a cancelled turn answers with the previous one.
    """
    out, pushes = await _run(_lines(_chunk(content="x")), abort=_abort())
    assert out.is_terminal is True
    assert out.terminal is None
    assert pushes.calls == [], "nothing was streamed, so nothing to push"


@pytest.mark.asyncio
async def test_the_between_chunk_abort_is_terminal_even_though_the_text_is_none():
    """Same trap, second site.

    One chunk has already been consumed here, so the buffers hold a
    partial answer. If the caller treats None as "not terminal" it will
    ship that fragment to a user who asked to stop.

    The abort fires on the SECOND check: the pre-stream guard passes,
    one chunk is folded in, and the between-chunk guard fires. Passing
    an already-set event instead returns before this branch runs at all.
    """
    out, pushes = await _run(_lines(_chunk(content="partial"),
                                    _chunk(content="more")),
                             abort=_LateAbort())
    assert out.is_terminal is True
    assert out.terminal is None
    assert pushes.calls == [], (
        "the second chunk must not be folded in -- that is the fragment "
        "the user asked us to stop sending")


@pytest.mark.asyncio
async def test_an_untouched_stream_is_never_terminal_with_no_reasoning():
    out, _ = await _run(["", ": comment", "data: ", "data: [DONE]"])
    assert not out.is_terminal
    assert out.content_buf == ""
    assert out.finish_reason == ""


# --- the framing -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_done_sentinel_is_never_parsed_as_json():
    """`[DONE]` is the last line, and the buffers stop growing after it."""
    lines = [f"data: {json.dumps(_chunk(content='one'))}",
             "data: [DONE]",
             f"data: {json.dumps(_chunk(content='two'))}"]
    out, _ = await _run(lines)
    assert out.content_buf == "one", (
        "the sentinel must stop the stream, not just be skipped as "
        "unparseable -- otherwise the tail after it is folded in too")


@pytest.mark.asyncio
async def test_a_malformed_chunk_is_skipped_rather_than_raising():
    lines = ["data: {not json",
             f"data: {json.dumps(_chunk(content='ok'))}",
             "data: [DONE]"]
    out, _ = await _run(lines)
    assert out.content_buf == "ok"


@pytest.mark.asyncio
async def test_tool_calls_come_back_in_the_indexed_shape_the_loop_expects():
    """{idx: {...}}, the same shape the non-streaming branch produces.

    If this drifted, the shared reconstruction further down would either
    raise or silently dispatch nothing.
    """
    tools = [{"index": 0, "id": "call_1", "function": {"name": "bash_command",
                                                       "arguments": "{}"}}]
    out, _ = await _run(_lines(_chunk(tools=tools), _chunk(finish="tool_calls")))
    assert out.tool_calls_buf == {
        0: {"id": "call_1", "name": "bash_command", "arguments": "{}"}}


# --- the reasoning wire ----------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_growth_pushes_and_the_final_flush_forces(monkeypatch):
    out, pushes = await _run(_lines(_chunk(reasoning="thinking..."),
                                    _chunk(content="answer")),
                              monkeypatch=monkeypatch)
    assert [c["force"] for c in pushes.calls] == [False, True], (
        "one push as it grows, one forced flush at the end")
    assert pushes.calls[0]["text"].startswith("prior ")
    assert all(c["throttle"] is not None for c in pushes.calls)
    assert all(c["silent"] is False for c in pushes.calls)


@pytest.mark.asyncio
async def test_a_stream_with_no_reasoning_pushes_nothing(monkeypatch):
    _, pushes = await _run(_lines(_chunk(content="plain answer")),
                           monkeypatch=monkeypatch)
    assert pushes.calls == []


# --- the fallback ----------------------------------------------------


class _FallbackClient:
    """Stands in for the second httpx client the fallback opens."""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, *_a, **_kw):
        return _PostResp(self._payload)


class _PostResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _patch_fallback(monkeypatch, payload):
    monkeypatch.setattr(ls.httpx, "AsyncClient",
                        lambda **_kw: _FallbackClient(payload))


@pytest.mark.asyncio
async def test_a_dead_stream_falls_back_to_non_streaming(monkeypatch):
    """No tool calls on the fallback means it has its own answer, and an
    answer ends the turn -- that is the pre-existing behaviour, not a
    continuation into the dispatch loop."""
    _patch_fallback(monkeypatch, {"choices": [{"message": {
        "content": "from the fallback", "reasoning_content": "because"}}]})
    out, _ = await _run(_lines(), client=_Client([], fail=True))
    assert out.is_terminal is True
    assert out.terminal == "from the fallback"


@pytest.mark.asyncio
async def test_the_fallback_answers_when_there_are_no_tool_calls(monkeypatch):
    _patch_fallback(monkeypatch, {"choices": [{"message": {
        "content": "plain", "finish_reason": "stop"}}]})
    out, _ = await _run(_lines(), client=_Client([], fail=True))
    assert out.is_terminal is True
    assert out.terminal == "plain"


@pytest.mark.asyncio
async def test_the_fallback_classifies_a_context_overflow(monkeypatch):
    """The fifth empty-choices site, and the one that had none.

    The split `if not X and is_context_overflow(...)` / `if not X:`
    shape satisfied no test that asked whether THIS branch checked.
    """
    _patch_fallback(monkeypatch, {
        "error": {"message": "the request exceeds the context window"}})
    out, _ = await _run(_lines(), client=_Client([], fail=True))
    assert out.is_terminal is True
    assert out.terminal == ls.OVERFLOW_MESSAGE


@pytest.mark.asyncio
async def test_the_fallback_explains_other_empty_choices(monkeypatch):
    _patch_fallback(monkeypatch, {"error": {"message": "rate limit"}})
    out, _ = await _run(_lines(), client=_Client([], fail=True))
    assert out.is_terminal is True
    assert "no choices on fallback" in out.terminal
    assert out.terminal != ls.OVERFLOW_MESSAGE


@pytest.mark.asyncio
async def test_the_fallback_joins_the_assistant_message_when_tools_ran(
        monkeypatch):
    _patch_fallback(monkeypatch, {"choices": [{"message": {
        "content": "ran a tool",
        "tool_calls": [{"id": "c1", "function": {"name": "bash_command",
                                                 "arguments": "{}"}}]}}]})
    out, _ = await _run(_lines(), client=_Client([], fail=True))
    assert not out.is_terminal
    assert out.append_msg is not None
    assert out.append_msg["tool_calls"][0]["id"] == "c1"
    assert out.content_buf == "ran a tool"


@pytest.mark.asyncio
async def test_a_dead_fallback_reports_both_errors(monkeypatch):
    class _Boom:
        async def __aenter__(self):
            raise httpx.HTTPError("fallback died too")

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(ls.httpx, "AsyncClient", lambda **_kw: _Boom())
    out, _ = await _run(_lines(), client=_Client([], fail=True))
    assert out.is_terminal is True
    assert "both streaming and non-streaming failed" in out.terminal


# --- the seam --------------------------------------------------------


def test_the_caller_branches_on_is_terminal_and_not_on_the_text():
    """The property the whole outcome type exists for.

    A structural check on the call site in call_llama: if this ever
    reads `if _stream.terminal is not None`, both abort paths fall
    through and the loop answers a cancelled question.
    """
    import ast
    src = (BOT_DIR / "call_llama.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")
    for node in ast.walk(fn):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Attribute)
                and node.test.attr == "is_terminal"):
            continue
        assert isinstance(node.test.value, ast.Name)
        body = " ".join(ast.dump(n) for n in ast.walk(node))
        assert "assert_terminal" in body, (
            f"call_llama:{node.lineno} branches on is_terminal but does "
            f"not go through assert_terminal(), which is what rejects an "
            f"outcome that is both terminal and a continuation")
        return
    pytest.fail("call_llama never branches on outcome.is_terminal")


def test_the_remaining_buffers_are_copied_off_the_outcome():
    """Every field the loop carries forward, assigned explicitly.

    Assigning only some of them is silent: the loop keeps whatever the
    previous iteration left, and the user gets a turn with no tool trace
    and no explanation.
    """
    import ast
    src = (BOT_DIR / "call_llama.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")
    assigned = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)):
            assigned.add(node.value.attr)
    # `terminal` is not in the list because it is RETURNED through
    # assert_terminal(), not assigned to a loop variable.
    missing = {"saw_unavailable_tool", "final_fallback",
               "reasoning_buf", "content_buf", "tool_calls_buf",
               "finish_reason"} - assigned
    assert not missing, f"call_llama reads the outcome but never assigns {missing}"

# --- the carry-through ------------------------------------------------

_TOOL_CALLS_FALLBACK = {"choices": [{"message": {
    "content": "ran a tool",
    "tool_calls": [{"id": "c1", "function": {"name": "bash_command",
                                             "arguments": "{}"}}]}}]}


@pytest.mark.asyncio
async def test_the_fallback_carries_the_tool_call_map_through(monkeypatch):
    """A stream that filled the buffer and then died must not lose it.

    Both mutations here -- dropping `tool_calls_buf` and dropping
    `finish_reason` from the fallback's return -- survived a full pass
    of this file. They are silent: the fallback's OWN tool calls travel
    in append_msg, so nothing looks wrong, and the loop below reads a
    None or an empty string where a value it owns used to be.
    """
    filled = {0: {"id": "c0", "name": "bash_command", "arguments": "{}"}}
    _patch_fallback(monkeypatch, _TOOL_CALLS_FALLBACK)
    out, _ = await _run(_lines(), client=_Client([], fail=True),
                        tool_calls_buf=filled)
    assert out.tool_calls_buf is filled, (
        "the fallback must hand back the buffer it was given, not drop it")


@pytest.mark.asyncio
async def test_the_fallback_carries_the_finish_reason_through(monkeypatch):
    """Same reason, and it reaches the user.

    `finish_reason` appears verbatim in the "empty response" and
    "reasoning-only" messages the loop ends with. Losing it leaves
    `finish_reason=` with nothing after it, which reads as a bug report
    from a user describing a perfectly normal turn.
    """
    _patch_fallback(monkeypatch, _TOOL_CALLS_FALLBACK)
    out, _ = await _run(_lines(), client=_Client([], fail=True),
                        finish_reason="length")
    assert out.finish_reason == "length"


@pytest.mark.asyncio
async def test_the_streaming_path_round_trips_what_it_was_handed():
    """finish_reason, final_fallback and saw_unavailable_tool come back.

    NOT tool_calls_buf: on the streaming path the accumulator owns that
    one and overwrites it with its own map on every chunk, which is what
    the inline code did too. Carrying the caller's value forward there
    would resurrect tool calls from a previous iteration -- the exact
    bug the accumulator-per-iteration test in test_c2_sse exists for.
    My first version of this test asserted a round-trip there and was
    wrong about the code, not the code about the test.
    """
    out, _ = await _run(_lines(_chunk(content="ok")), finish_reason="stop",
                        final_fallback="previous", saw_unavailable_tool=True)
    assert out.finish_reason == "stop"
    assert out.final_fallback == "previous"
    assert out.saw_unavailable_tool is True
    assert out.tool_calls_buf == {}, (
        "the accumulator's own map, not the one handed in")
