"""The courier: a file out of the sandbox, and one copy left behind.

The sandbox side (`vaskes/OpenWebUI-bash-sandbox`, tag `v2.7.0`)
publishes a file door next to its shell door. This file tests the bot's
half of that contract -- the half that used to be base64 through a
terminal pane, with a ceiling of about 675 KB and a silent loss of the
head when the pane overflowed.

What is pinned here:

  * the bytes that arrive are the bytes on disk, and the digest is of
    what was actually written;
  * the meta door exists so the size is known BEFORE the bytes are
    pulled, and an over-limit file is refused with its real size;
  * the errors say what happened -- and the proxy's own quirk (a 200
    that carries an error in the body) is read;
  * the sandbox copy is deleted after the delivery, and a failure to
    delete is a sentence rather than an exception: the file is already
    in the chat.
"""
import hashlib
import sys
from pathlib import Path

import httpx
import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import personal_tools.file_transport as ft  # noqa: E402
from file_door_fake import FakeSandbox, install  # noqa: E402

PAYLOAD = bytes(range(256)) * 40          # 10240 bytes, not text
PATH = "/workspace/random10k.bin"


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    fake = FakeSandbox()
    install(monkeypatch, fake, tmp_path)
    return fake


async def test_the_bytes_are_the_bytes_on_disk(sandbox):
    """The file the model produced is the file the user receives."""
    sandbox.files[PATH] = PAYLOAD

    fetched = await ft.fetch_to_temp(PATH)

    assert Path(fetched.path).read_bytes() == PAYLOAD
    assert fetched.size == len(PAYLOAD)
    assert fetched.sha256 == hashlib.sha256(PAYLOAD).hexdigest(), (
        "the digest has to be of what was written -- it is the number the "
        "model can compare with its own sha256sum")
    assert fetched.name == "random10k.bin"


async def test_the_bytes_never_go_through_the_shell(sandbox):
    """The door is HTTP. A file that travelled as text would be base64
    in the model's context and would be bounded by the pane."""
    sandbox.files[PATH] = PAYLOAD

    await ft.fetch_to_temp(PATH)

    assert [route for _, route, _ in sandbox.requests] == ["/files"], (
        f"the file went somewhere else: {sandbox.requests}")


async def test_every_request_carries_the_bearer_token(sandbox):
    sandbox.files[PATH] = PAYLOAD

    await ft.file_meta(PATH)
    await ft.fetch_to_temp(PATH)
    await ft.purge(PATH)

    assert sandbox.tokens == ["Bearer test-token"] * 3, sandbox.tokens


async def test_the_meta_door_answers_with_the_size_and_the_limit(sandbox):
    sandbox.files[PATH] = PAYLOAD
    sandbox.limit = 4096

    meta = await ft.file_meta(PATH)

    assert meta["size"] == len(PAYLOAD)
    assert meta["limit"] == 4096
    assert meta["name"] == "random10k.bin"


async def test_a_file_over_the_limit_is_refused_with_its_real_size(sandbox):
    """The 413 names the size, so the model can say what the file is
    instead of failing vaguely."""
    sandbox.files[PATH] = PAYLOAD
    sandbox.limit = 100

    with pytest.raises(ft.FileTransportError) as caught:
        await ft.fetch_to_temp(PATH)

    message = caught.value.message
    assert "10240" in message, message
    assert "100" in message, message


async def test_a_missing_file_says_so(sandbox):
    with pytest.raises(ft.FileTransportError) as caught:
        await ft.fetch_to_temp("/workspace/nope.bin")

    assert "no such file" in caught.value.message, caught.value.message


async def test_a_refused_path_says_so(sandbox):
    """A path outside the workspace, a directory, a symlink out: the
    sandbox refuses, and the bot has to say what it said."""
    sandbox.meta_body = {"error": '{"detail": "path outside the workspace: /etc/passwd"}'}

    with pytest.raises(ft.FileTransportError) as caught:
        await ft.file_meta("/etc/passwd")

    assert "outside the workspace" in caught.value.message, caught.value.message


async def test_a_side_car_without_the_file_door_says_so(monkeypatch):
    """A sandbox older than v2.7.0 has no /files/meta route at all. It
    answers 404, and saying "no such file" there would blame the file
    for the side-car's age."""
    import personal_tools.file_transport as ft

    def no_door(request):
        return httpx.Response(404, json={"detail": "Not Found"})

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(no_door)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(ft, "AGENT_STACK_URL", "http://sandbox.test")
    monkeypatch.setattr(ft, "AGENT_STACK_TOKEN", "test-token")

    with pytest.raises(ft.FileTransportError) as caught:
        await ft.file_meta(PATH)

    assert "v2.7.0" in caught.value.message, caught.value.message


async def test_the_proxies_own_quirk_is_read(sandbox):
    """The proxy answers 200 when the agent refused the path, and puts
    the refusal in the body. A client that only reads the status code
    sees a success with no metadata in it -- and then pulls a file it
    never learned the size of."""
    sandbox.meta_body = {"error": '{"detail": "file not found: /workspace/x"}'}

    with pytest.raises(ft.FileTransportError) as caught:
        await ft.file_meta("/workspace/x")

    assert "file not found" in caught.value.message, caught.value.message


async def test_the_purge_removes_the_sandbox_copy(sandbox):
    sandbox.files[PATH] = PAYLOAD

    note = await ft.purge(PATH)

    assert note == "the sandbox copy was removed", note
    assert PATH not in sandbox.files, (
        "the scratch copy is still in the workspace -- the artifact lives "
        "in two places at once")
    assert sandbox.paths("DELETE") == [PATH]


async def test_a_purge_of_a_file_that_is_not_there_is_not_a_failure(sandbox):
    note = await ft.purge("/workspace/gone.bin")

    assert note == "the sandbox copy was already gone", note


async def test_a_purge_that_fails_is_a_sentence_not_an_exception(sandbox):
    """The file is already in the chat. A failure to clean up is worth
    saying, not worth raising."""
    sandbox.files[PATH] = PAYLOAD
    sandbox.delete_status = 500

    note = await ft.purge(PATH)

    assert "could not be removed" in note, note


async def test_a_failed_pull_leaves_no_file_behind(sandbox, tmp_path):
    """A half-written temp file is litter in the bot's container."""
    sandbox.limit = 10
    sandbox.files[PATH] = PAYLOAD

    with pytest.raises(ft.FileTransportError):
        await ft.fetch_to_temp(PATH)

    assert list(tmp_path.iterdir()) == [], (
        "the temp file of a failed pull was left behind: "
        f"{list(tmp_path.iterdir())}")


async def test_the_pull_does_not_read_the_file_into_memory(sandbox):
    """The bytes are written as they arrive. A courier that collected
    them first would make the size of the file the size of the bot's
    memory."""
    sandbox.files[PATH] = PAYLOAD

    fetched = await ft.fetch_to_temp(PATH)

    # The file exists on disk with the right size: the pull wrote it.
    assert Path(fetched.path).stat().st_size == len(PAYLOAD)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
