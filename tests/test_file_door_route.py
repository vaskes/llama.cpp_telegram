"""The file door, and the model's way of reaching it.

Measured on production, 2026-11-01, 19:20-19:26 MSK. The operator sent
a 603 675-byte JSON file, had it put in the sandbox, and asked for it to
be read into the model's context WHOLE. Two things happened, and neither
of them was the file door:

  * the model did not try. It read a window size out of a screenshot the
    operator had posted earlier in the chat, did the arithmetic on the
    file's size against that number, and answered that reading the file
    whole is "physically impossible" -- without calling one tool;
  * when it did try, in the next turn, it called `bash_command` with
    `cat`, and the console cut the file twice: the side-car at 524 288
    bytes and the bot itself at 8 000 characters. The model saw 1 742
    characters of a 474 071-character file.

The door was there the whole time. `sandbox_read_file` was in the tool
list, the side-car answered 200 with the whole file, and the side-car's
read budget (1 MiB) was four times the size of the file. Measured the
same day through the bot's own transport: the file read through the
door grew the prompt from 7 077 to 130 478 tokens -- it arrives whole,
and the engine's window (460 776 tokens) takes it.

So the file was never lost on the way to the model. It was lost on the
way to the TOOL, and the bot's answer to that is one section in the
system prompt: it names the door this chat has, says the door delivers
a file whole, says the console cannot, and says the server -- not the
model's own arithmetic about a window size it cannot see -- decides
whether a file fits.

The tests here drive the REAL `call_llama` and read the system message
the model is shown. Nothing in this file imports the module that
carries the text: a tripwire that cannot be collected on the old tree
cannot say which behaviour broke, and a tripwire that imports the new
code can only fail with an ImportError -- which is red, but says
nothing about the defect.

The wire is faked at the MCP client boundary and at the engine's
transport; the registry, the tool builder and the prompt are the real
ones.
"""
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import call_llama  # noqa: E402
import llm_tools  # noqa: E402
import mcp_registry  # noqa: E402
from mcp_client import McpResult, McpServerConfig  # noqa: E402


# === the fakes ==========================================================


class FakeClient:
    """A server that answers with what the test told it to."""

    def __init__(self, config):
        self.config = config
        self.tools = []

    async def initialize(self):
        return "sess"

    async def list_tools(self):
        return list(self.tools)

    async def call_tool(self, name, arguments):
        return McpResult(text=f"{self.config.name}:{name}")


def _def(name):
    return {"type": "function",
            "function": {"name": name, "description": "d", "parameters": {}}}


def _registry_with(monkeypatch, tools, **config_kwargs):
    """A real registry over a server that offers `tools`."""
    config_kwargs.setdefault("gate", "always")
    config = McpServerConfig(name="srv", url="http://srv.invalid/mcp",
                             **config_kwargs)

    def factory(cfg):
        client = FakeClient(cfg)
        client.tools = list(tools)
        return client

    monkeypatch.setattr(mcp_registry, "McpClient", factory)
    return mcp_registry.build_registry([config])


async def _registry(monkeypatch, tools=("read_file",), **config_kwargs):
    registry = _registry_with(monkeypatch, [
        {"name": name, "description": "d"} for name in tools
    ], **config_kwargs)
    await registry.refresh()
    return registry


async def _system_prompt(monkeypatch, tool_defs, registry):
    """The system message the model is shown, through the real
    `call_llama`. The engine is not reached: the tool loop is replaced
    by a box that keeps the messages."""
    monkeypatch.setattr(mcp_registry, "get_registry", lambda: registry)
    monkeypatch.setattr(call_llama, "build_tool_defs",
                        _fake_build_tool_defs(tool_defs))
    monkeypatch.setattr(call_llama, "MODEL", "test-model")
    kept = {}

    class FakeLoop:
        def __init__(self, msgs, **kwargs):
            kept["msgs"] = msgs

        async def run(self):
            return {"text": "ok", "executed_tools": [], "files": []}

    monkeypatch.setattr(call_llama, "ToolLoop", FakeLoop)
    await call_llama.call_llama(
        [{"role": "user", "content": "прочитай файл целиком"}],
        bot=None, chat_id=111, chat_type="supergroup")
    return kept["msgs"][0]["content"]


def _fake_build_tool_defs(defs):
    async def build(*, bot, chat_id, chat_type):
        return list(defs)
    return build


# === the registry: which imported tool is the door ======================


@pytest.mark.asyncio
async def test_the_registry_names_the_servers_file_door(monkeypatch):
    """The side-car's own name for the door is `read_file`, and the bot
    shows it as `<server>_read_file`. The registry knows the bare name
    behind every public name it handed out, so the answer is exact."""
    registry = await _registry(monkeypatch, ("read_file", "file_info"))
    assert registry.file_door_public_names() == ("srv_read_file",)


@pytest.mark.asyncio
async def test_a_tool_that_is_not_the_door_is_not_a_door(monkeypatch):
    """`file_info` is not a door: it reports a file's size, it does not
    deliver the file. A door that is not a door is a tool the model
    calls and gets a stat instead of the file."""
    registry = await _registry(monkeypatch, ("file_info",))
    assert registry.file_door_public_names() == ()


@pytest.mark.asyncio
async def test_a_door_the_chat_may_not_use_is_not_named(monkeypatch):
    """The gate is the same gate the advertisement uses. A chat the
    operator did not enable the server for is not told about the door
    -- the model would call it and be refused."""
    # The dev-mode allowlist is read at import time, so the gate is
    # exercised by patching the two constants it is built from.
    from personal_tools import bash_command as _bc
    monkeypatch.setattr(_bc, "_DEV_MODE_PROFILE", True)
    monkeypatch.setattr(_bc, "_DEV_ALLOWED_CHAT_IDS", frozenset({111}))
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "dev")
    registry = await _registry(monkeypatch, ("read_file",), gate="dev-chat")
    assert registry.file_door_public_names(chat_id=111) == ("srv_read_file",)
    assert registry.file_door_public_names(chat_id=222) == ()


@pytest.mark.asyncio
async def test_only_a_door_that_is_in_the_tool_list_is_named(monkeypatch):
    """Two questions, and the answer is their intersection: the
    registry knows which imported tool is the door, and the tool list
    knows which of them this chat is shown. A name in only one of them
    is a tool the model cannot call -- and a name it cannot call is a
    name it invents."""
    registry = await _registry(monkeypatch, ("read_file",))
    monkeypatch.setattr(mcp_registry, "get_registry", lambda: registry)

    assert llm_tools.file_door_names([_def("srv_read_file")], 111) == \
        ("srv_read_file",)
    assert llm_tools.file_door_names([_def("bash_command")], 111) == ()


# === the prompt the model actually reads ===============================


@pytest.mark.asyncio
async def test_the_system_prompt_carries_the_section(monkeypatch):
    """The section is in the SYSTEM message -- the bot's own surface,
    where the rules live. The chat's messages are data, and the model
    weighs the rules against them."""
    registry = await _registry(monkeypatch, ("read_file",))
    prompt = await _system_prompt(
        monkeypatch,
        [_def("srv_read_file"), _def("bash_command")],
        registry)
    assert "## Reading a file whole" in prompt
    assert "srv_read_file" in prompt


@pytest.mark.asyncio
async def test_the_section_says_the_console_is_not_a_door(monkeypatch):
    """The console is the way the file was lost: it caps the output and
    drops the middle. The model has to be told that the console cannot
    do what the door does."""
    registry = await _registry(monkeypatch, ("read_file",))
    prompt = await _system_prompt(
        monkeypatch,
        [_def("srv_read_file"), _def("bash_command")],
        registry)
    assert "not a file door" in prompt
    assert "dropped" in prompt


@pytest.mark.asyncio
async def test_the_section_says_the_door_delivers_a_file_whole(monkeypatch):
    registry = await _registry(monkeypatch, ("read_file",))
    prompt = await _system_prompt(
        monkeypatch,
        [_def("srv_read_file"), _def("bash_command")],
        registry)
    assert "WHOLE" in prompt
    assert "first byte" in prompt and "last" in prompt


@pytest.mark.asyncio
async def test_the_section_says_the_server_decides_whether_a_file_fits(
        monkeypatch):
    """The model decided the file would not fit, from a window size
    quoted in the chat, and never called a tool. The section says who
    the arbiter is: the server, which refuses by name and says the size
    and the limit."""
    registry = await _registry(monkeypatch, ("read_file",))
    prompt = await _system_prompt(
        monkeypatch,
        [_def("srv_read_file"), _def("bash_command")],
        registry)
    assert "refused BY NAME" in prompt
    assert "Do not decide in advance" in prompt
    assert "the only way to know is to call the door" in prompt


@pytest.mark.asyncio
async def test_the_section_says_to_call_the_tool(monkeypatch):
    """The operator's chat on 2026-11-01 held 49 198 characters of the
    model writing a command out as text -- and then answering from a
    file it had never read. The section says that a written-out command
    was never run, so what the model says about the file afterwards is
    a guess and not a reading."""
    registry = await _registry(monkeypatch, ("read_file",))
    prompt = await _system_prompt(
        monkeypatch,
        [_def("srv_read_file"), _def("bash_command")],
        registry)
    assert "never run" in prompt
    assert "Call it" in prompt


@pytest.mark.asyncio
async def test_the_tools_block_says_a_written_out_command_was_never_run(
        monkeypatch):
    """The rule is not only about files: a command written out in an
    answer is not a tool call, and the chat shows what the tools
    printed. This one lives in the tools block, so it is there even in
    a chat with no door."""
    registry = await _registry(monkeypatch, ("read_file",))
    prompt = await _system_prompt(monkeypatch, [_def("bash_command")],
                                  registry)
    assert "CALL the tool" in prompt
    assert "never run" in prompt


@pytest.mark.asyncio
async def test_the_door_says_in_its_own_description_that_it_is_the_door():
    """The server's own words about its `read_file` send the model to
    bash: "use bash (head, tail, sed) to read a piece of a big file
    instead". That bash is NOT imported -- the side-car's bash is
    excluded on purpose, and the bot's own console is the nearest
    thing in the model's list, and the console cuts the file. So the
    door's description has to say what the door is in the bot's world.

    The schema stays the server's, and so do the words about what the
    server refuses. The bot adds its own facts."""
    from mcp_registry import _tool_def
    defn = _tool_def("sandbox_read_file", {
        "name": "read_file",
        "description": ("Read a file from the sandbox workspace. A file "
                        "over the read limit is refused -- use bash "
                        "(head, tail, sed) to read a piece of a big file "
                        "instead."),
    }, "sandbox")
    description = defn["function"]["description"]
    assert "This is the file door" in description
    assert "WHOLE file" in description
    assert "1048576 bytes" in description
    assert "The console is not a door" in description
    # The server's own words are still there.
    assert "use bash (head, tail, sed)" in description


@pytest.mark.asyncio
async def test_the_section_gives_the_limit(monkeypatch):
    """The model needs the number to choose the door.

    On 2026-11-01 it read the side-car's own description -- "use bash
    to read a piece of a big file instead" -- and decided that 603 675
    bytes was "a big file", without knowing that the door's limit is
    1 048 576 bytes. It could not have known: the limit was nowhere in
    the prompt. The size of the file IS in the conversation (the
    pointer says it), so the section that names the door has to carry
    the limit too -- otherwise the comparison cannot be made."""
    registry = await _registry(monkeypatch, ("read_file",))
    prompt = await _system_prompt(
        monkeypatch,
        [_def("srv_read_file"), _def("bash_command")],
        registry)
    assert "1048576 bytes" in prompt
    assert "Compare the file's size" in prompt


@pytest.mark.asyncio
async def test_the_console_says_it_is_not_a_way_to_read_a_file(monkeypatch):
    """The console's own description said "inspecting files in
    /workspace", which is the hook the model reached for. The console
    is the bot's own tool, so its description is the bot's to write --
    and what it says now is what the console is FOR."""
    from personal_tools.bash_command import BASH_COMMAND_TOOL_DEF
    description = BASH_COMMAND_TOOL_DEF["function"]["description"]
    assert "NOT a way to read a file" in description
    assert "capped" in description


@pytest.mark.asyncio
async def test_a_chat_without_a_door_is_told_about_no_door(monkeypatch):
    """The public profile has no sandbox and no door. A section that
    named one there would be the bot advertising a tool the model
    cannot call."""
    registry = await _registry(monkeypatch, ("read_file",))
    prompt = await _system_prompt(monkeypatch, [_def("bash_command")],
                                  registry)
    assert "## Reading a file whole" not in prompt
    assert "srv_read_file" not in prompt


@pytest.mark.asyncio
async def test_the_section_does_not_move_from_turn_to_turn(monkeypatch):
    """The engine renders the tools block and the system message BEFORE
    the history, so a system message that moves from turn to turn moves
    the very beginning of the prompt and the prefix cache never matches
    (CODEBOOK 3.17). The section depends on the tool list and on
    nothing else, so it is the same bytes on every turn."""
    registry = await _registry(monkeypatch, ("read_file",))
    first = await _system_prompt(
        monkeypatch,
        [_def("srv_read_file"), _def("bash_command")],
        registry)
    second = await _system_prompt(
        monkeypatch,
        [_def("srv_read_file"), _def("bash_command")],
        registry)
    assert first == second
