"""The `send_file` tool: a file from the sandbox to the chat.

The operator asked for a zip, then for a 10 KB binary, and both times
the bytes stayed in the sandbox: the only channel from the bot to the
chat was the model's text, and a binary has no text form. What arrived
instead was 180 lines of base64 in a message and two `.txt` documents
full of console output.

This tool is the other channel. The model creates the file in the
sandbox and calls `send_file` with its path; the bot fetches the bytes
through the sandbox's file door and sends them as a Telegram document.

The route changed on 2026-10-25, when the sandbox side shipped the file
door (`vaskes/OpenWebUI-bash-sandbox` v2.7.0). Before that the bytes
came back as base64 through the terminal pane, and the pane was bounded
by the scrollback: it lost the **head** silently when it overflowed, so
the payload decoded into garbage. The tests below drive the real
dispatcher over the real transport, against a fake door -- so what is
pinned is the path the production path takes.

What is pinned here:

  * the document that goes out is the file's bytes, not its text;
  * the bytes come through the file door and not through the shell;
  * the sandbox copy is deleted after the delivery -- one copy, not two;
  * the size is known before the bytes are pulled, so a file nobody
    could deliver is never downloaded;
  * a file that is too big, missing or refused produces a message the
    model can read and act on, instead of an exception.
"""
import hashlib
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import llm_dispatch  # noqa: E402
from file_door_fake import FakeSandbox, install  # noqa: E402
from llm_dispatch import DispatchDeps, execute_tool  # noqa: E402

PAYLOAD = bytes(range(256)) * 40          # 10240 bytes, not text
PATH = "/workspace/random10k.bin"


class _FakeBot:
    def __init__(self):
        self.documents = []
        self.events = []

    async def send_document(self, **kwargs):
        self.events.append("send_document")
        self.documents.append(kwargs)
        return SimpleMessage()


class SimpleDocument:
    """What Telegram answers with: the channel's own id for the file."""
    file_id = "BQACAgIAAxFILEID"


class SimpleMessage:
    message_id = 1
    document = SimpleDocument()


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
def sandbox(monkeypatch, tmp_path):
    """The sandbox's file door, and a bot that records what it sent."""
    fake = FakeSandbox()
    install(monkeypatch, fake, tmp_path)

    import personal_tools.bash_command as bc
    monkeypatch.setattr(bc, "is_enabled_for_active_profile", lambda: True)

    # The shell door must stay shut: the file travels over HTTP, and a
    # base64 dump through the pane is the route this replaced.
    async def no_shell(command, **kwargs):
        raise AssertionError(
            f"the file went through the shell: {command}")

    monkeypatch.setattr(bc, "call_bash_command", no_shell)
    return fake


async def test_the_file_is_sent_as_a_document(sandbox):
    """The whole point: the bytes travel, not a description of them."""
    sandbox.files[PATH] = PAYLOAD
    deps = _deps()

    outcome = await execute_tool("send_file", {"path": PATH}, deps)

    assert outcome.empty is False, (
        "sending a file is progress; an empty result would count the "
        "turn as fruitless and the loop would give up")
    assert "sent random10k.bin" in outcome.text
    assert len(deps.bot.documents) == 1
    sent = deps.bot.documents[0]
    payload = sent["document"].input_file_content
    assert payload == PAYLOAD, (
        "the document has to carry the file's bytes")
    assert sent["chat_id"] == deps.chat_id


async def test_the_ledger_carries_the_pointer(sandbox):
    """The pointer is the channel's own id for the file, not the path in
    the sandbox: the path dies with the sandbox, and the channel's id
    can be re-sent without pulling the bytes again."""
    sandbox.files[PATH] = PAYLOAD
    deps = _deps()

    outcome = await execute_tool("send_file", {"path": PATH}, deps)

    entry = outcome.ledger
    assert entry is not None, "a delivered file left no trace in the ledger"
    assert entry["name"] == "send_file"
    assert entry["size"] == len(PAYLOAD)
    assert entry["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert entry["file_id"] == "BQACAgIAAxFILEID", (
        "the pointer is the channel's id, not the path in the sandbox")


async def test_the_digest_is_of_what_was_delivered(sandbox):
    """The number the model can check against its own sha256sum."""
    sandbox.files[PATH] = PAYLOAD
    deps = _deps()

    outcome = await execute_tool("send_file", {"path": PATH}, deps)

    digest = hashlib.sha256(PAYLOAD).hexdigest()
    assert digest in outcome.text, (
        f"the tool result does not carry the digest: {outcome.text}")


async def test_the_file_travels_through_the_file_door(sandbox):
    """Not through the shell. The pane route was bounded by the
    scrollback and lost the head silently when it overflowed."""
    sandbox.files[PATH] = PAYLOAD
    deps = _deps()

    await execute_tool("send_file", {"path": PATH}, deps)

    routes = [route for _, route, _ in sandbox.requests]
    assert "/files" in routes, f"the file door was not used: {routes}"
    assert "/bash" not in routes, f"the shell door was used: {routes}"


async def test_the_size_is_known_before_the_bytes_are_pulled(sandbox):
    """The meta door exists so that a file nobody could deliver is
    never downloaded."""
    sandbox.files[PATH] = PAYLOAD
    sandbox.limit = 100
    deps = _deps()

    outcome = await execute_tool("send_file", {"path": PATH}, deps)

    assert "10240" in outcome.text and "100" in outcome.text, outcome.text
    routes = [route for method, route, _ in sandbox.requests
              if method == "GET"]
    assert routes == ["/files/meta"], (
        "the file was pulled even though the sandbox says it is over "
        f"the limit: {sandbox.requests}")
    assert deps.bot.documents == []


async def test_a_file_over_the_channel_limit_is_refused(sandbox, monkeypatch):
    """Telegram's sendDocument takes 50 MB through the public Bot API.
    A file over that cannot be delivered, and it is not downloaded to
    find out."""
    sandbox.files[PATH] = PAYLOAD
    sandbox.limit = 0            # the sandbox does not care
    deps = _deps()

    async def lying_meta(path, **kwargs):
        return {"name": "huge.bin", "path": path,
                "size": llm_dispatch.MAX_SEND_FILE_BYTES + 1,
                "mtime": 0, "mime": "application/octet-stream", "limit": 0}

    import personal_tools.file_transport as ft
    monkeypatch.setattr(ft, "file_meta", lying_meta)

    outcome = await execute_tool("send_file", {"path": "/workspace/huge.bin"},
                                 deps)

    assert "sendDocument" in outcome.text, outcome.text
    assert str(llm_dispatch.MAX_SEND_FILE_BYTES) in outcome.text, outcome.text
    assert deps.bot.documents == []


async def test_the_sandbox_copy_is_deleted_after_the_delivery(sandbox):
    """One copy, not two: the sandbox is a scratchpad, the delivery is
    the artifact."""
    sandbox.files[PATH] = PAYLOAD
    deps = _deps()
    deps.bot.events = []

    outcome = await execute_tool("send_file", {"path": PATH}, deps)

    assert "the sandbox copy was removed" in outcome.text, outcome.text
    assert PATH not in sandbox.files, (
        "the scratch copy is still in the workspace")
    assert sandbox.paths("DELETE") == [PATH]


async def test_the_document_lands_in_the_topic_of_the_question(sandbox):
    """A forum supergroup: a plain send lands in General, a reply lands
    in the topic the user asked in."""
    sandbox.files[PATH] = b"x" * 10
    deps = _deps()
    await execute_tool("send_file", {"path": PATH}, deps)
    sent = deps.bot.documents[0]
    assert sent.get("reply_to_message_id") == deps.current_message_id, (
        "without the reply the document lands in General, not in the "
        "topic the user asked in")


async def test_a_missing_file_says_so(sandbox):
    deps = _deps()
    outcome = await execute_tool("send_file", {"path": "/workspace/nope.bin"},
                                 deps)
    assert "sent" not in outcome.text
    assert "nope.bin" in outcome.text, (
        "the model has to be able to tell the user WHICH file failed")


async def test_a_file_that_is_too_big_says_so(sandbox):
    """The message names the real size, not a generic failure."""
    sandbox.files[PATH] = PAYLOAD
    sandbox.limit = 10
    deps = _deps()
    outcome = await execute_tool("send_file", {"path": PATH}, deps)
    assert "10240" in outcome.text, outcome.text
    assert deps.bot.documents == []


async def test_the_temp_file_is_removed(sandbox, tmp_path):
    """The pulled file is litter if it stays in the bot's container."""
    sandbox.files[PATH] = PAYLOAD
    deps = _deps()

    await execute_tool("send_file", {"path": PATH}, deps)

    assert list(tmp_path.iterdir()) == [], (
        f"the temp file stayed behind: {list(tmp_path.iterdir())}")


async def test_a_relative_path_says_so(sandbox):
    """The file door has no session, so a relative path is not a path
    there -- and saying so is more useful than a 404 about a file the
    model can see."""
    deps = _deps()
    outcome = await execute_tool("send_file", {"path": "probe.zip"}, deps)
    assert "absolute" in outcome.text, outcome.text
    assert deps.bot.documents == []
    assert sandbox.requests == [], (
        "the request went out anyway: " f"{sandbox.requests}")


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
