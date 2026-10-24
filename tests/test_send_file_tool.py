"""The `send_file` tool: a file from the sandbox to the chat.

The operator asked for a zip, then for a 10 KB binary, and both times
the bytes stayed in the sandbox: the only channel from the bot to the
chat was the model's text, and a binary has no text form. What
arrived instead was 180 lines of base64 in a message and two `.txt`
documents full of console output.

This tool is the other channel. The model creates the file in the
sandbox and calls `send_file` with its path; the bot fetches the bytes
through the same side-car and sends them as a Telegram document.

What is pinned here:

  * the document that goes out is the file's bytes, not its text;
  * the fetch goes through the sandbox's own exec, so the reach is
    the model's own reach;
  * a path that is not a file, a fetch that fails, and a file that is
    too big for the channel all produce a message the model can read
    and act on, instead of an exception;
  * the tool is advertised only when the sandbox is -- no sandbox, no
    way to send a file out of it.
"""
import base64
import shlex
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import llm_dispatch  # noqa: E402
from llm_dispatch import DispatchDeps, execute_tool  # noqa: E402


class _FakeBot:
    def __init__(self):
        self.documents = []

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)
        return SimpleMessage()


class SimpleMessage:
    message_id = 1


def _deps(bot=None):
    return DispatchDeps(
        bot=bot or _FakeBot(),
        chat_id=-1004461679108,
        current_message_id=4242,
        chat_type="supergroup",
        iteration=0,
        custom_tools={},
        donsetch_tools={},
    )


@pytest.fixture
def sandbox(monkeypatch):
    """A stand-in for the sandbox: the file the model 'created'."""
    state = {"files": {}, "commands": []}

    async def fake_call(command, *, session_id=None, timeout=70.0,
                        chat_type="private", chat_id=None):
        state["commands"].append(command)
        if command.startswith("base64 -w0 "):
            # shlex, not strip(): the path is quoted, and a quote inside
            # the path is escaped by the quoting.
            path = shlex.split(command)[-1]
            if path not in state["files"]:
                return {"ok": False, "error": "No such file"}
            return {"ok": True, "stdout": base64.b64encode(
                state["files"][path]).decode(), "stderr": "",
                "exit_code": 0, "duration_ms": 1}
        return {"ok": True, "stdout": "", "stderr": "",
                "exit_code": 0, "duration_ms": 1}

    import personal_tools.bash_command as bc
    monkeypatch.setattr(bc, "call_bash_command", fake_call)
    monkeypatch.setattr(bc, "is_enabled_for_active_profile", lambda: True)
    return state


async def test_the_file_is_sent_as_a_document(sandbox):
    """The whole point: the bytes travel, not a description of them."""
    sandbox["files"]["/workspace/random10k.bin"] = bytes(range(256)) * 40
    deps = _deps()

    outcome = await execute_tool("send_file", {"path": "/workspace/random10k.bin"},
                                 deps)

    assert outcome.empty is False, (
        "sending a file is progress; an empty result would count the "
        "turn as fruitless and the loop would give up")
    assert "sent random10k.bin" in outcome.text
    assert len(deps.bot.documents) == 1
    sent = deps.bot.documents[0]
    payload = sent["document"].input_file_content
    assert payload == sandbox["files"]["/workspace/random10k.bin"], (
        "the document has to carry the file's bytes")
    assert sent["chat_id"] == deps.chat_id


def _sent_bytes(sent):
    return sent["document"].input_file_content


async def test_the_document_lands_in_the_topic_of_the_question(sandbox):
    """A forum supergroup: a plain send lands in General, a reply lands
    in the topic the question came from."""
    sandbox["files"]["/workspace/a.bin"] = b"x" * 10
    deps = _deps()
    await execute_tool("send_file", {"path": "/workspace/a.bin"}, deps)
    sent = deps.bot.documents[0]
    assert sent.get("reply_to_message_id") == deps.current_message_id, (
        "without the reply the document lands in General, not in the "
        "topic the user asked in")


async def test_the_fetch_goes_through_the_sandbox(sandbox):
    """The bot does not read the file itself; it asks the sandbox, the
    same way the model does."""
    sandbox["files"]["/workspace/a.bin"] = b"y" * 10
    deps = _deps()
    await execute_tool("send_file", {"path": "/workspace/a.bin"}, deps)
    assert sandbox["commands"], "the file was never fetched"
    assert "base64 -w0" in sandbox["commands"][0]


async def test_a_path_with_a_quote_in_it_is_quoted(sandbox):
    """The path comes from the model and goes into a shell command."""
    weird = "/workspace/it's here.bin"
    sandbox["files"][weird] = b"z" * 4
    deps = _deps()
    outcome = await execute_tool("send_file", {"path": weird}, deps)
    assert "sent" in outcome.text, (
        f"a path with a quote broke the fetch: {outcome.text}")
    assert sandbox["commands"][0].count("'") >= 2


async def test_a_missing_file_says_so(sandbox):
    deps = _deps()
    outcome = await execute_tool("send_file", {"path": "/workspace/nope.bin"},
                                 deps)
    assert "sent" not in outcome.text
    assert "nope.bin" in outcome.text, (
        "the model has to be able to tell the user WHICH file failed")


async def test_a_file_that_is_too_big_says_so(monkeypatch):
    """The channel has a ceiling: the sandbox caps its captured output
    at 1 MB, and base64 is four thirds of the file."""
    import personal_tools.bash_command as bc

    async def fake_call(command, *, session_id=None, timeout=70.0,
                        chat_type="private", chat_id=None):
        return {"ok": True, "stdout": "A" * (llm_dispatch.MAX_SEND_FILE_BASE64_CHARS + 10),
                "stderr": "", "exit_code": 0, "duration_ms": 1}

    monkeypatch.setattr(bc, "call_bash_command", fake_call)
    monkeypatch.setattr(bc, "is_enabled_for_active_profile", lambda: True)
    deps = _deps()
    outcome = await execute_tool("send_file", {"path": "/workspace/big.bin"},
                                 deps)
    assert "too large" in outcome.text
    assert deps.bot.documents == []


async def test_no_path_says_so(monkeypatch):
    import personal_tools.bash_command as bc
    monkeypatch.setattr(bc, "is_enabled_for_active_profile", lambda: True)
    deps = _deps()
    outcome = await execute_tool("send_file", {}, deps)
    assert "path" in outcome.text


def test_the_tool_is_advertised_only_with_the_sandbox():
    """No sandbox, no way to send a file out of it: the model must not
    be told about a tool that cannot work."""
    from llm_sendfile import send_file_tool_def

    defn = send_file_tool_def()
    assert defn["function"]["name"] == "send_file"
    assert "path" in defn["function"]["parameters"]["properties"]
    assert defn["function"]["parameters"]["required"] == ["path"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
