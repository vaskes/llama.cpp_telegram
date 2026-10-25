"""A cut says so.

Every truncation in this codebase used to be silent: the text was
shorter and the reader could not tell that anything was missing. A
model that reads a truncated tool result cannot tell that there was
more, and answers as if it had seen the whole thing. A user who reads
a truncated transcript believes the command printed exactly that.

The sandbox side fixed its half of this in v2.7.0: its stdout carries
two markers, one for a cut tail and one -- on the FIRST line -- for an
output whose beginning did not survive the scrollback. The bot's half
is this file: every cut the bot makes is marked, and the sandbox's own
marker survives the bot's cut instead of being dropped with the head.

Four cuts are pinned here, because there are four places that cut:

  * the tool result the model reads (`llm_iteration`);
  * the stdout the ledger keeps (`llm_dispatch`);
  * the transcript the chat shows (`llm_result`);
  * the command output the model reads (`bash_command.tool_result_to_text`).
"""
import sys
import time
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import llm_dispatch  # noqa: E402
import llm_iteration as LI  # noqa: E402
from llm_dispatch import MAX_TOOL_RESULT_CHARS  # noqa: E402
from llm_iteration import ToolLoop  # noqa: E402
from llm_loop_guard import LoopGuard  # noqa: E402
from llm_nonstream import NonStreamResult  # noqa: E402
from llm_result import unwrap_answer  # noqa: E402

#: The marker the sandbox prints on the first line when the beginning of
#: an output did not fit in the session scrollback. Its wording is the
#: sandbox's (v2.7.0, `agent_api.py`); the bot has to keep it.
SANDBOX_HEAD_MARKER = (
    "[the beginning of this output no longer fits in the session "
    "scrollback — what follows is only its tail]"
)


#: A tool name the isolation layer knows. The loop soft-rejects a name
#: it does not know, and a rejected call never reaches the dispatcher --
#: so a test that invents a name tests the isolation layer, not the cap.
KNOWN_TOOL = "donsetch_web_search"


def _loop():
    return ToolLoop(
        msgs=[{"role": "user", "content": "сделай файл"}],
        max_tokens=256,
        req_tools=[{"type": "function", "function": {"name": KNOWN_TOOL}}],
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


def _tool_call(name, arguments):
    return {"id": "call_1", "type": "function",
            "function": {"name": name, "arguments": arguments}}


# --- the tool result the model reads ---------------------------------------

async def test_the_tool_result_the_model_reads_is_marked(monkeypatch):
    """The real loop, a real tool call, and the message the model
    actually gets."""
    long_text = "z" * (MAX_TOOL_RESULT_CHARS * 3)
    calls = {"n": 0}

    async def fake_engine(req_body, *, iteration, abort_event=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return NonStreamResult(
                "loop", content="", reasoning="",
                tool_calls=[_tool_call(KNOWN_TOOL, "{}")],
                finish_reason="tool_calls")
        return NonStreamResult(
            "loop", content="Готово.", reasoning="",
            tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(LI, "non_stream_iteration", fake_engine)

    async def handler(args):
        return long_text

    # The registry of the web tools is what the loop hands to the
    # dispatcher, so that is where the fake goes.
    monkeypatch.setattr(LI, "DONSETCH_TOOLS", {KNOWN_TOOL: handler})

    loop = _loop()
    await loop.run()

    tool_messages = [m for m in loop.msgs if m.get("role") == "tool"]
    assert tool_messages, "the loop did not append a tool message"
    content = tool_messages[-1]["content"]
    assert len(content) < len(long_text), "the result was not cut at all"
    assert "characters were dropped from the middle" in content, content[-200:]


# --- the stdout the ledger keeps -------------------------------------------

async def test_the_ledger_stdout_is_marked(monkeypatch):
    """The ledger goes to the database and to the log. It has to be
    bounded, and it has to say that it was."""
    from llm_dispatch import _ledger_stdout

    long_stdout = "line of output\n" * 5000
    stored = _ledger_stdout(long_stdout)

    assert len(stored) <= llm_dispatch.MAX_LEDGER_STDOUT_CHARS
    assert "characters were dropped from the middle" in stored


def test_the_ledger_keeps_both_ends():
    """The beginning carries the sandbox's marker; the end carries what
    the command printed last, and what the model quotes back."""
    from llm_dispatch import _ledger_stdout

    long_stdout = ("HEAD_MARKER\n" + "filler\n" * 5000
                   + "TAIL_MARKER\n")
    stored = _ledger_stdout(long_stdout)

    assert "HEAD_MARKER" in stored, (
        "the head was dropped -- the sandbox's own marker lives there")
    assert "TAIL_MARKER" in stored, (
        "the tail was dropped -- that is what the model quotes back")


def test_the_sandbox_head_marker_survives_the_ledger_cut():
    """The whole point of keeping the head: the sandbox says on its
    FIRST line that the beginning did not survive. A cut that keeps the
    tail throws that line away, and the chat is shown a fragment as if
    it were the whole thing."""
    from llm_dispatch import _ledger_stdout

    stdout = (SANDBOX_HEAD_MARKER + "\n" + "filler\n" * 5000)
    stored = _ledger_stdout(stdout)

    assert SANDBOX_HEAD_MARKER in stored, (
        "the sandbox's marker did not survive -- the reader cannot tell "
        "that what they are looking at is a tail")


# --- the transcript the chat shows -----------------------------------------

def test_the_transcript_is_marked():
    """The chat shows what the command printed, and it has to say when
    what it shows is not all of it."""
    result = {
        "text": "Готово.",
        "executed_tools": [
            {"name": "bash_command",
             "args": {"command": "cat big.log"},
             "iter": 0,
             "stdout": "line of output\n" * 5000},
        ],
    }
    answer = unwrap_answer(result)

    assert "characters were dropped from the middle" in answer.text, (
        "the transcript shows a cut as if it were the whole output")


def test_a_short_output_is_not_marked():
    """Nothing was dropped, so nothing is said. A marker on every
    transcript would be noise, and noise is what makes people stop
    reading the markers."""
    result = {
        "text": "Готово.",
        "executed_tools": [
            {"name": "bash_command",
             "args": {"command": "echo hi"},
             "iter": 0,
             "stdout": "hi\n"},
        ],
    }
    answer = unwrap_answer(result)

    assert "characters were dropped" not in answer.text, answer.text


# --- the command output the model reads ------------------------------------

def test_the_model_view_of_the_output_is_marked():
    """The model sees the tail of a long output. It has to be told that
    the beginning is not there."""
    import personal_tools.bash_command as bc

    text = bc.tool_result_to_text({
        "ok": True,
        "stdout": "HEAD_MARKER\n" + "filler\n" * 5000 + "TAIL_MARKER\n",
        "stderr": "",
        "exit_code": 0,
        "duration_ms": 1,
    })

    assert "characters were dropped from the middle" in text, text[-300:]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
