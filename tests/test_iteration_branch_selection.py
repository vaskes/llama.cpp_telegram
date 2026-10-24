"""Which POST does an iteration make -- and does the body match the parse?

Production, 2026-10-22. 11 of 35 requests failed with

    [non-stream err iter=0] JSONDecodeError: Expecting value: line 1
    column 1 (char 0); body=''

which read like a flaky engine returning empty bodies. It was not the
engine. The request body was built with `"stream": use_stream` and then
handed to the non-streaming POST regardless of `use_stream`, so every
`handle_text` turn -- which leaves `use_stream` at its default True --
asked llama-server for SSE and then tried to parse the result as JSON.
The server complied exactly:

    status=200 bytes=30330 transfer-encoding='chunked'
    body='data: {"choices":[{"finish_reason":null,...'

`handle_photo` and `handle_document` pass `use_stream=False`, which is
why the failures lined up with text turns and nobody connected them to
request building.

The condition was in `ac79264` and again in `a079e3c`; `0673c5c` dropped
it, `b8d8c11` dropped it again. Neither loss raised anything: the branch
was simply always taken.

What is pinned here:

  1. use_stream=True  -> no non-streaming POST at all.
  2. use_stream=False -> the non-streaming POST, and no streaming one.
  3. `ns_handled` is bound before the branch that reads it. With the
     guard in place its only set-site is conditional, so the read at
     `if not ns_handled` is an UnboundLocalError waiting for the branch
     nobody exercised -- the same shape as the saw_unavailable_tool bug
     in the same function.
  4. the two sites agree: the body is built with `use_stream`, so the
     branch that reads `r.json()` must be the branch that sent
     `stream=False`.
"""
import ast
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import llm_iteration as LI  # noqa: E402
from llm_iteration import IterationResult, ToolLoop  # noqa: E402
from llm_nonstream import NonStreamResult  # noqa: E402


def make_loop(**over):
    kw = dict(
        msgs=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        req_tools=None,
        bot=object(),
        chat_id=1,
        current_message_id=2,
        chat_type="private",
        shutdown_event=None,
        abort_event=None,
        thinking_msg=None,
        silent=False,
        use_stream=False,
        throttle=None,
        guard=object(),
        loop_start=0.0,
        custom_tools={},
    )
    kw.update(over)
    return ToolLoop(**kw)


class _Calls:
    """Records which POST helper ran, and refuses to run the other one."""

    def __init__(self, forbidden):
        self.forbidden = forbidden
        self.nonstream = 0
        self.stream = 0

    def install(self, monkeypatch):
        async def _nonstream(req_body, *, iteration, abort_event=None):
            self.nonstream += 1
            assert req_body["stream"] is False, (
                f"the non-streaming POST was sent stream={req_body['stream']!r}; "
                f"the engine will answer SSE and r.json() will raise"
            )
            return NonStreamResult("tools", content="ok", tool_calls=[])

        async def _stream(*a, **kw):
            self.stream += 1
            raise AssertionError(
                f"{self.forbidden} must not reach the streaming branch")

        # Both helpers are bound into llm_iteration's own namespace by
        # `from ... import`, so patching the defining module would not
        # change what run_one calls.
        monkeypatch.setattr(LI, "non_stream_iteration", _nonstream)
        monkeypatch.setattr(LI, "stream_iteration", _stream)
        return self


@pytest.mark.asyncio
async def test_use_stream_false_takes_the_non_streaming_branch(monkeypatch):
    calls = _Calls("use_stream=False").install(monkeypatch)
    result = await make_loop(use_stream=False).run_one(0)
    assert calls.nonstream == 1
    # The non-streaming path ends the turn with the answer, and the
    # answer carries the tools the turn ran -- the same shape the
    # streaming path produces, so the transcript with the console
    # output is in the chat on both paths. (Before 2026-10-25 this
    # path returned a bare string, and the tools were lost with it.)
    assert isinstance(result.terminal, dict), (
        f"the non-streaming turn ended as {type(result.terminal).__name__}, "
        f"not as an answer")
    assert result.terminal["text"] == "ok"
    assert result.terminal["executed_tools"] == []


@pytest.mark.asyncio
async def test_use_stream_true_never_takes_the_non_streaming_branch(monkeypatch):
    """The regression. The non-streaming POST used to run first,
    unconditionally, so this is the call that has to stop happening."""
    seen = {"nonstream": 0}

    async def _nonstream(*a, **kw):
        seen["nonstream"] += 1
        return NonStreamResult("tools", content="should not happen", tool_calls=[])

    class _Outcome:
        is_terminal = True

        def assert_terminal(self):
            return "from the streaming branch"

    async def _stream(*a, **kw):
        return _Outcome()

    monkeypatch.setattr(LI, "non_stream_iteration", _nonstream)
    monkeypatch.setattr(LI, "stream_iteration", _stream)

    result = await make_loop(use_stream=True).run_one(0)
    assert seen["nonstream"] == 0, (
        "a streaming turn sent a non-streaming POST; the body still "
        "carries stream=True, so the engine answers SSE and the parse "
        "raises JSONDecodeError on every text message"
    )
    assert result.terminal == "from the streaming branch"


# --- structural: the two sites must keep agreeing -------------------------

def _run_one_tree():
    """run_one's body, as AST.

    Parsed out of the module file rather than `inspect.getsource`: the
    latter returns a method indented inside its class, which `ast.parse`
    rejects outright, and dedenting it is a string operation standing
    where an AST operation belongs.
    """
    tree = ast.parse(Path(LI.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolLoop":
            for sub in node.body:
                if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name == "run_one"):
                    return sub.body
    raise AssertionError(f"ToolLoop.run_one not found in {LI.__file__}")


def _all_nodes(body):
    """Every node in run_one's body, in source order.

    Recursive on purpose. The branch selection is nested inside the
    `async with httpx.AsyncClient(...)` block, so a search over the
    statement list alone finds nothing and reports "no guard" -- a
    tripwire that pins a position instead of a property.
    """
    out = []
    for node in body:
        out.append(node)
        for field in ("body", "orelse", "finalbody"):
            child = getattr(node, field, None)
            if isinstance(child, list):
                out.extend(_all_nodes(child))
    return sorted(out, key=lambda n: (getattr(n, "lineno", 0),
                                      getattr(n, "col_offset", 0)))


def _guard_for_nonstream_branch():
    """The `if` whose body holds the non_stream_iteration await."""
    for node in _all_nodes(_run_one_tree()):
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Await)
                    and isinstance(sub.value, ast.Call)
                    and isinstance(sub.value.func, ast.Name)
                    and sub.value.func.id == "non_stream_iteration"):
                return node
    return None


def test_the_non_streaming_branch_is_guarded_by_use_stream():
    guard = _guard_for_nonstream_branch()
    assert guard is not None, (
        "no `if` guards the non_stream_iteration await any more; without "
        "one it runs on every iteration and the engine answers SSE to a "
        "parse that expects JSON"
    )
    test = guard.test
    negated = (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not))
    if negated:
        test = test.operand          # `not self.use_stream`
    assert negated, (
        f"the guard is {ast.dump(guard.test)}; the non-streaming POST has "
        f"to run when streaming is OFF. Inverting it sends SSE-only turns "
        f"to the plain POST and JSON-only turns to the SSE parser"
    )
    assert isinstance(test, ast.Attribute), ast.dump(guard.test)
    assert (test.attr == "use_stream"
            and isinstance(test.value, ast.Name)
            and test.value.id == "self"), (
        f"the guard tests {ast.dump(guard.test)}; it has to test "
        f"self.use_stream, or the branch selection is not the one the "
        f"request body was built for"
    )


def test_ns_handled_is_bound_before_the_read():
    """Bound where the read is, not at the conditional set-site."""
    nodes = _all_nodes(_run_one_tree())
    read_at = next(
        (n.lineno for n in nodes
         if isinstance(n, ast.If)
         and isinstance(n.test, ast.UnaryOp)
         and isinstance(n.test.operand, ast.Name)
         and n.test.operand.id == "ns_handled"),
        None,
    )
    assert read_at is not None, "`if not ns_handled:` is gone from run_one"
    # ns_handled = False specifically, not "an assignment to ns_handled".
    # There is another one inside the non-streaming branch
    # (`ns_handled = True`), and accepting that as the binding made this
    # test survive deleting the line it is named after: a
    # `ns_handled = True` inside a branch that does not run leaves the
    # name unbound on the path that reaches the read. Mutation-checked.
    bind_at = next(
        (n.lineno for n in nodes
         if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "ns_handled"
                 for t in n.targets)
         and isinstance(n.value, ast.Constant)
         and n.value.value is False),
        None,
    )
    assert bind_at is not None, (
        "nothing assigns ns_handled = False before the read; the "
        "assignment inside the non-streaming branch does not bind it on "
        "the streaming path"
    )
    # Before the GUARD, not merely before the read. Binding it between
    # the two was live for one commit: the non-streaming branch assigns
    # True at its end, the binding then reset it to False, and every turn
    # -- including the use_stream=False ones that had just taken the
    # non-streaming path -- fell through to the streaming branch. It is
    # caught behaviourally (selftest_llm's check_nonstream_tool_call
    # reaches stream_iteration with a client that has no .stream), but
    # the ordering is worth stating here too: "before the read" was
    # satisfiable and wrong.
    guard = _guard_for_nonstream_branch()
    assert guard is not None and bind_at < guard.lineno, (
        f"ns_handled = False is at line {bind_at} but the non-streaming "
        f"branch starts at {getattr(guard, 'lineno', None)}; binding "
        f"after that resets the True the branch just set"
    )
    assert bind_at < read_at, (
        "ns_handled is assigned after the branch that reads it; with the "
        "non-streaming branch now conditional, use_stream=True reaches "
        "the read with the name unbound"
    )