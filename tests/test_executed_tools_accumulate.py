"""The tools a turn ran must reach the answer.

Measured on production, 2026-10-25. The operator asked the bot for a
10 KB binary. The model made the file in the sandbox, called the new
`send_file` tool, and the document arrived -- with the right bytes,
md5 for md5. But the chat showed no trace of the commands: not the
`dd`, not the `ls`, not what they printed. The operator's rule of
2026-10-24 was that the console output belongs in the chat, and the
chat did not have it.

The cause was one line of scope. The accumulator of executed tools was
a local of `ToolLoop.run_one` -- the ITERATION -- while the tools of
the turn are spread over the iterations. Every time an iteration
returned, its list went with it, so the answer that came out of the
last iteration carried no tools at all, and the transcript with the
console output was empty.

The unit tests did not catch it, and that is the part worth keeping:
they handed `executed_tools` to the formatter by hand. The hand-made
shape was a shape the production loop never produces, so the tests
proved the formatter works and the loop never was asked.

So the test here drives the real loop -- a real `ToolLoop`, a real
iteration, a real tool call -- and looks at what comes out at the end.
"""


import asyncio
import sys
import time
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import llm_iteration as LI  # noqa: E402
from llm_iteration import ToolLoop  # noqa: E402
from llm_loop_guard import LoopGuard  # noqa: E402
from llm_nonstream import NonStreamResult  # noqa: E402
from llm_stream import StreamOutcome  # noqa: E402
from llm_result import unwrap_answer  # noqa: E402

STDOUT = (
    "-rw-r--r-- 1 agent agent 10240 Sep 25 00:50 random10k.bin\n"
    "e6e347b8b54cb9cedd747b5026d38ad0  random10k.bin\n"
)


def _loop():
    """A ToolLoop with the smallest honest inputs, on the streaming path."""
    return ToolLoop(
        msgs=[{"role": "user", "content": "сделай бинарник и пришли"}],
        max_tokens=256,
        req_tools=[{"type": "function", "function": {"name": "bash_command"}}],
        bot=object(),
        chat_id=-1004461679108,
        current_message_id=42,
        chat_type="supergroup",
        shutdown_event=None,
        abort_event=None,
        thinking_msg=None,
        silent=True,
        use_stream=True,
        throttle=None,
        guard=LoopGuard(budget_sec=300.0),
        loop_start=time.monotonic(),
        custom_tools={},
    )


def _tool_call(name, arguments):
    return {"id": "call_1", "type": "function",
            "function": {"name": name, "arguments": arguments}}


@pytest.mark.asyncio
async def test_the_tools_of_the_turn_reach_the_answer(monkeypatch):
    """A tool call in the first iteration, the answer in the second.

    The answer is what the chat gets, and the transcript with the
    console output is part of it. If the accumulator dies with the
    iteration, the transcript is empty and the user sees an answer
    with no sign that anything ran.
    """
    calls = {"n": 0}

    async def fake_engine(req_body, *, iteration, abort_event=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return NonStreamResult(
                "loop", content="", reasoning="",
                tool_calls=[_tool_call(
                    "bash_command",
                    '{"command": "ls -l /workspace/random10k.bin"}')],
                finish_reason="tool_calls")
        return NonStreamResult(
            "loop", content="Готово, файл 10 КБ.", reasoning="",
            tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(LI, "non_stream_iteration", fake_engine)

    async def fake_bash(command, *, session_id=None, timeout=70.0,
                        chat_type="private", chat_id=None):
        return {"ok": True, "stdout": STDOUT, "stderr": "",
                "exit_code": 0, "duration_ms": 1}

    import personal_tools.bash_command as bc
    monkeypatch.setattr(bc, "call_bash_command", fake_bash)
    monkeypatch.setattr(bc, "is_enabled_for_active_profile", lambda: True)

    loop = ToolLoop(
        msgs=[{"role": "user", "content": "сделай бинарник и пришли"}],
        max_tokens=256,
        req_tools=[{"type": "function", "function": {"name": "bash_command"}}],
        bot=object(),
        chat_id=-1004461679108,
        current_message_id=42,
        chat_type="supergroup",
        shutdown_event=None,
        abort_event=None,
        thinking_msg=None,
        silent=True,
        use_stream=False,
        throttle=None,
        guard=LoopGuard(budget_sec=300.0),
        loop_start=time.monotonic(),
        custom_tools={},
    )

    result = await loop.run()

    assert isinstance(result, dict), (
        f"the turn ended as {type(result).__name__}, not as an answer "
        f"with the tools in it")
    executed = result.get("executed_tools") or []
    assert len(executed) == 1, (
        f"the turn ran {len(executed)} tools, expected the one from the "
        f"first iteration: the accumulator died with the iteration")
    assert executed[0].get("name") == "bash_command"

    answer = unwrap_answer(result)
    assert "```bash" in (answer.text or ""), (
        "the transcript is not in the answer -- the chat shows no trace "
        "of what ran")
    assert "ls -l /workspace/random10k.bin" in (answer.text or "")
    assert "e6e347b8b54cb9cedd747b5026d38ad0" in (answer.text or ""), (
        "the console output is not in the chat -- the operator's rule of "
        "2026-10-24")


@pytest.mark.asyncio
async def test_the_same_on_the_streaming_path(monkeypatch):
    """Production streams. The fallback path and the streaming path must
    agree on what the answer carries, or the transcript appears only
    when the engine happens to fail over to the other path."""
    calls = {"n": 0}

    async def fake_stream(client, req_body, *, iteration, abort_event=None,
                          accumulated_reasoning="", thinking_msg=None,
                          silent=True, throttle=None,
                          saw_unavailable_tool=False, final_fallback=None,
                          finish_reason="", tool_calls_buf=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return StreamOutcome(
                content_buf="", reasoning_buf="", finish_reason="tool_calls",
                tool_calls_buf={0: {
                    "id": "call_1", "name": "bash_command",
                    "arguments": '{"command": "ls -l /workspace/random10k.bin"}'}},
                append_msg={"role": "assistant", "content": None,
                            "tool_calls": []})
        return StreamOutcome(
            content_buf="Готово, файл 10 КБ.", reasoning_buf="",
            finish_reason="stop", tool_calls_buf={})

    monkeypatch.setattr(LI, "stream_iteration", fake_stream)

    async def fake_bash(command, *, session_id=None, timeout=70.0,
                        chat_type="private", chat_id=None):
        return {"ok": True, "stdout": STDOUT, "stderr": "",
                "exit_code": 0, "duration_ms": 1}

    import personal_tools.bash_command as bc
    monkeypatch.setattr(bc, "call_bash_command", fake_bash)
    monkeypatch.setattr(bc, "is_enabled_for_active_profile", lambda: True)

    loop = _loop()
    result = await loop.run()

    assert isinstance(result, dict), (
        f"the turn ended as {type(result).__name__}, not as an answer "
        f"with the tools in it")
    assert len(result.get("executed_tools") or []) == 1
    answer = unwrap_answer(result)
    assert "```bash" in (answer.text or "")
    assert "e6e347b8b54cb9cedd747b5026d38ad0" in (answer.text or ""), (
        "the console output is not in the chat")


@pytest.mark.asyncio
async def test_the_accumulator_belongs_to_the_turn_not_the_iteration():
    """The scope, stated directly.

    The comment in `__init__` says what the attribute is for; this
    checks the attribute is there and is the same object across the
    iterations of one turn. A local of `run_one` cannot be: it is
    created anew every time the iteration starts.
    """
    loop = ToolLoop(
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
        silent=True,
        use_stream=False,
        throttle=None,
        guard=LoopGuard(budget_sec=300.0),
        loop_start=0.0,
        custom_tools={},
    )
    assert isinstance(loop.executed_tools, list)
    first = loop.executed_tools
    loop.executed_tools.append({"name": "bash_command", "args": {}})
    assert loop.executed_tools is first
    assert len(loop.executed_tools) == 1
