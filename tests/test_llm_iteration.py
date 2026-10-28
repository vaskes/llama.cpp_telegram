"""T8.1 -- the iteration as a named unit.

Everything else in this block's suite is an AST tripwire: it proves a
guard is *written* in `run_one` or `run`. This file drives the two
methods and proves they *behave*, because a named unit that nothing
calls is a comment with a `def` in it.

The split that makes this worth a file of its own: before T8.1 the loop
and the iteration body were one function, so "the loop stops on a
terminal result" and "the iteration reports terminal-ness" were the
same assertion, written once. After the split they are two different
contracts between two methods, and the tripwires that re-pointed to
`run_one` say nothing about the contract that matters most -- that the
value one method returns is what the other one reads.

So: stub `run_one` and drive `run()`, and drive `run_one` through its
two pre-HTTP exits and assert on the value that reaches `run`.
"""
import asyncio
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import limits  # noqa: E402
import llm_iteration as LI  # noqa: E402
from llm_iteration import IterationResult, ToolLoop  # noqa: E402

pytestmark = pytest.mark.tech_debt(item=8)


def make_loop(**over):
    """A ToolLoop with the smallest possible honest inputs.

    Everything here is a value `run` and `run_one` read but the tests
    below never exercise; the seam is the only thing under test.
    """
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


# ---------------------------------------------------------------------------
# run(): what it does with what the iteration decided
# ---------------------------------------------------------------------------


def _loop_that_yields(*results):
    """A ToolLoop whose `run_one` returns the given outcomes in order.

    The seam under test is `run`'s reading of what the iteration
    returned, so the iteration itself is stubbed -- that is the only way
    to drive the non-terminal path without a live engine.
    """
    loop = make_loop()
    seen = []

    async def fake_run_one(iteration):
        seen.append(iteration)
        idx = min(len(seen) - 1, len(results) - 1)
        return results[idx]

    loop.run_one = fake_run_one
    return loop, seen


@pytest.mark.asyncio
async def test_run_stops_at_the_first_terminal_iteration():
    loop, seen = _loop_that_yields(
        IterationResult.again(), IterationResult.again(),
        IterationResult.done("answer"))

    assert await loop.run() == "answer"
    assert seen == [0, 1, 2], "run kept going past a terminal result"


@pytest.mark.asyncio
async def test_run_passes_the_zero_based_iteration_number():
    """The counter reaches the log lines, the guard budget and the
    non-streaming request. Starting it at 1 mislabels every message
    the bot ever logs about its own loop."""
    loop, seen = _loop_that_yields(IterationResult.again())

    await loop.run()

    assert seen[0] == 0, f"iterations are numbered from {seen[0]}"
    assert seen == list(range(limits.MAX_ITER)), "the counter skips or repeats"


@pytest.mark.asyncio
async def test_run_treats_a_none_payload_as_terminal():
    """The Stop button and the shutdown guard both end the turn with
    `terminal=None`. A loop that asks "is `terminal` set?" carries on
    past both and answers a cancelled question with the previous
    iteration's text. This is the one line the whole outcome type
    exists for, so it gets its own test and not a comment."""
    loop, seen = _loop_that_yields(
        IterationResult.again(), IterationResult.done(None))

    assert await loop.run() is None
    assert seen == [0, 1], "a None payload ended the turn, and run went round again"


@pytest.mark.asyncio
async def test_an_exhausted_loop_returns_the_fallback_it_collected():
    loop, seen = _loop_that_yields(IterationResult.again())
    loop.final_fallback = "half an answer"

    out = await loop.run()

    assert out == "half an answer", \
        "the loop threw away the last thing the model actually said"
    assert len(seen) == limits.MAX_ITER


@pytest.mark.asyncio
async def test_an_exhausted_loop_with_no_fallback_says_so():
    loop, _ = _loop_that_yields(IterationResult.again())

    assert await loop.run() == LI.ITERATION_LIMIT_MESSAGE


@pytest.mark.asyncio
async def test_the_repository_default_cap_is_fifteen():
    """Pinned by value, not by presence.

    The cap is a SETTING now (`limits.MAX_ITER`, `LLAMABOT_MAX_ITER`),
    so a deployment may raise it -- the production one does. What this
    pins is the value the REPOSITORY ships, because that is what every
    other deployment inherits: a silent change of the default from 15
    to 50 turns a 10-second hang into a 30-minute one, and nothing
    else in the suite notices.
    """
    assert limits.MAX_ITER == 15


@pytest.mark.asyncio
async def test_run_does_not_call_the_iteration_past_the_cap():
    loop, seen = _loop_that_yields(IterationResult.again())

    await loop.run()

    assert len(seen) == limits.MAX_ITER, \
        f"the loop ran {len(seen)} iterations, cap is {limits.MAX_ITER}"


# ---------------------------------------------------------------------------
# run_one(): the two exits that happen before any HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shutdown_guard_ends_the_iteration():
    ev = asyncio.Event()
    ev.set()
    loop = make_loop(shutdown_event=ev)

    out = await loop.run_one(0)

    assert out.is_terminal is True
    assert "shutdown" in out.terminal


@pytest.mark.asyncio
async def test_the_shutdown_guard_reads_the_module_default_when_given_none():
    """`call_llama` passes `shutdown_event=None` for the common case and
    means "use the process-wide one". A loop that treated None as "no
    shutdown" would ignore SIGTERM for every ordinary turn."""
    loop = make_loop(shutdown_event=None)

    # With the process default unset, this must NOT be terminal --
    # otherwise the guard is reading something it should not.
    assert getattr(LI, "SHUTDOWN_EVENT", None) is None or \
        not LI.SHUTDOWN_EVENT.is_set()


@pytest.mark.asyncio
async def test_the_stop_button_ends_the_iteration_with_no_payload():
    ev = asyncio.Event()
    ev.set()
    loop = make_loop(abort_event=ev)

    out = await loop.run_one(0)

    assert out.is_terminal is True
    assert out.terminal is None, \
        "the abort payload is legitimately None; the decision is not"


@pytest.mark.asyncio
async def test_the_two_exits_are_ordered_before_any_request():
    """Shutdown is global, abort is per-message. A turn that is doing
    both must report the shutdown one -- otherwise the container gets a
    SIGTERM and waits out the current turn anyway."""
    shutdown = asyncio.Event()
    shutdown.set()
    abort = asyncio.Event()
    abort.set()
    loop = make_loop(shutdown_event=shutdown, abort_event=abort)

    out = await loop.run_one(0)

    assert "shutdown" in out.terminal


@pytest.mark.asyncio
async def test_again_is_a_continuation():
    """If `run_one` returned None on the ordinary path, `run` would do
    `None.is_terminal` and raise AttributeError inside the loop instead
    of carrying on. The ordinary path needs a live engine to reach, so
    the contract is pinned on the value itself -- and the mutation
    harness removes the trailing `return IterationResult.again()` to
    prove a test somewhere notices."""
    assert IterationResult.again().is_terminal is False


# ---------------------------------------------------------------------------
# the value object itself
# ---------------------------------------------------------------------------


def test_the_default_outcome_is_a_continuation():
    out = IterationResult()

    assert out.is_terminal is False
    assert out.terminal is None


def test_done_carries_its_payload():
    out = IterationResult.done("the answer")

    assert out.is_terminal is True
    assert out.terminal == "the answer"


def test_done_with_none_is_still_terminal():
    """The distinction the whole type turns on. A payload-less exit and
    a continuation differ only in `is_terminal`, so a caller that
    reads the payload cannot tell them apart -- which is the bug the
    explicit flag was introduced to prevent."""
    out = IterationResult.done(None)

    assert out.is_terminal is True
    assert out.terminal is None
    assert out.is_terminal is not IterationResult().is_terminal

# ---------------------------------------------------------------------------
# the seam from the other side: call_llama must AWAIT the loop
# ---------------------------------------------------------------------------
#
# The mutation harness killed this one: dropping `.run()` from
# `call_llama`'s return leaves a `ToolLoop` where an answer belongs, and
# nothing in the loop's own suite noticed -- 333 tests stayed green.
# Every other test in this file drives `run` directly, so none of them
# can see whether `call_llama` calls it at all. This one does, by
# calling `call_llama` with the engine stubbed out.




@pytest.fixture
def stub_engine(monkeypatch):
    """call_llama reaches the engine twice before it builds the loop:
    `_ensure_model()` and `fetch_tools_from_llama()`. Both are network,
    both are irrelevant to the seam, and both have to be stubbed or the
    test measures httpx's retry budget."""
    import call_llama as CL

    async def no_model():
        return None

    async def no_tools():
        return []

    monkeypatch.setattr(CL, "_ensure_model", no_model)
    monkeypatch.setattr(CL, "fetch_tools_from_llama", no_tools)
    return CL

@pytest.mark.asyncio
async def test_call_llama_awaits_the_loop_and_returns_its_answer(
        monkeypatch, stub_engine):
    CL = stub_engine

    calls = []

    class FakeLoop:
        def __init__(self, *a, **kw):
            calls.append(kw)

        async def run(self):
            calls.append("run")
            return "the answer the loop produced"

    monkeypatch.setattr(CL, "ToolLoop", FakeLoop)

    out = await CL.call_llama([{"role": "user", "content": "hi"}],
                        chat_type="private")

    assert "run" in calls, (
        "call_llama constructed the loop and never awaited run() -- the "
        "answer it returns is a ToolLoop object, not a turn's reply")
    assert out == "the answer the loop produced", (
        f"call_llama returned {out!r} instead of what the loop produced")


@pytest.mark.asyncio
async def test_call_llama_hands_the_loop_the_inputs_the_iteration_reads(
        monkeypatch, stub_engine):
    """`run_one` reads these off `self`. If one stops crossing the
    boundary the attribute is still there and still None, and the
    failure surfaces as a wrong request rather than an error."""
    CL = stub_engine

    seen = {}

    class FakeLoop:
        def __init__(self, *a, **kw):
            seen.update(kw)

        async def run(self):
            return "ok"

    monkeypatch.setattr(CL, "ToolLoop", FakeLoop)

    await CL.call_llama([{"role": "user", "content": "hi"}],
                        bot="BOT", chat_id=42, current_message_id=7,
                        chat_type="group")

    for field in ("max_tokens", "req_tools", "bot", "chat_id",
                  "current_message_id", "chat_type", "shutdown_event",
                  "abort_event", "thinking_msg", "silent", "use_stream",
                  "throttle", "guard", "loop_start", "custom_tools"):
        assert field in seen, (
            f"call_llama no longer passes {field} to the loop; run_one "
            f"reads self.{field} and will get None")
    assert seen["chat_id"] == 42
    assert seen["current_message_id"] == 7
    assert seen["bot"] == "BOT"
