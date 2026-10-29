"""The door's other half: a file goes INTO the sandbox.

The download half has its own tests (`test_file_transport.py`). These
are for the upload half -- the one the operator needed on 2026-10-29,
when he uploaded a 47.6 MiB binary, asked for it to be put in the
sandbox, and was told the file was too large. The door only opened
outwards, so the chat was a one-way street that ended at a refusal.

What is pinned here:

  * the bytes that land in the sandbox are the bytes that were sent,
    and the digest is of what was actually written;
  * the door refuses to overwrite: a delivery that removes what it was
    not told to remove is a surprise, and the surprise is what the user
    has to undo;
  * the refusals say what happened -- a token the sandbox will not
    take, a name that is taken, a directory that is not there, a file
    over the limit;
  * the name the user gave is made safe before it reaches the door.
"""
import hashlib
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import personal_tools.file_upload as fu  # noqa: E402
from file_door_fake import FakeSandbox, install  # noqa: E402

PAYLOAD = bytes(range(256)) * 40          # 10240 bytes, not text


@pytest.fixture
def door(monkeypatch, tmp_path):
    sandbox = FakeSandbox()
    install(monkeypatch, sandbox, tmp_path)
    return sandbox


@pytest.mark.asyncio
async def test_the_bytes_land_in_the_sandbox(door):
    uploaded = await fu.upload_to_sandbox(PAYLOAD, "big_50m.bin")
    assert door.files["/workspace/big_50m.bin"] == PAYLOAD
    assert uploaded.name == "big_50m.bin"
    assert uploaded.path == "/workspace/big_50m.bin"
    assert uploaded.size == len(PAYLOAD)
    assert uploaded.sha256 == hashlib.sha256(PAYLOAD).hexdigest()


@pytest.mark.asyncio
async def test_the_upload_is_a_post_of_the_bytes(door):
    await fu.upload_to_sandbox(PAYLOAD, "big.bin")
    posts = door.calls("POST")
    assert len(posts) == 1
    assert posts[0][1].endswith("/files")
    assert posts[0][2] == "/workspace/big.bin"
    assert door.tokens[-1] == "Bearer test-token"


@pytest.mark.asyncio
async def test_the_door_does_not_overwrite(door):
    """The door refuses; the client finds another name.

    Two things are true at once, and they are not in conflict: the
    door never overwrites (the file that was there is still the file
    that was there), and the upload still lands -- under a numbered
    name, the way a browser stores the second copy of a download.

    The operator hit this on 2026-10-29: the chat already held a file
    with that name, and the answer was a refusal with no way out.
    """
    await fu.upload_to_sandbox(b"first", "artifact.bin")
    uploaded = await fu.upload_to_sandbox(b"second", "artifact.bin")

    # The file that was there is still the file that was there.
    assert door.files["/workspace/artifact.bin"] == b"first"
    # The second copy landed beside it, under a numbered name.
    assert uploaded.name == "artifact (1).bin"
    assert uploaded.path == "/workspace/artifact (1).bin"
    assert door.files["/workspace/artifact (1).bin"] == b"second"
    # And the door was asked three times: the first upload, the
    # refused name, and the name that was free.
    assert door.paths("POST") == [
        "/workspace/artifact.bin",
        "/workspace/artifact.bin",
        "/workspace/artifact (1).bin"]


@pytest.mark.asyncio
async def test_the_numbering_keeps_counting(door):
    """`x`, `x (1)`, `x (2)` -- the number follows the files that are there."""
    door.files["/workspace/thing.bin"] = b"one"
    door.files["/workspace/thing (1).bin"] = b"two"
    uploaded = await fu.upload_to_sandbox(b"three", "thing.bin")
    assert uploaded.name == "thing (2).bin"
    assert door.files["/workspace/thing.bin"] == b"one"
    assert door.files["/workspace/thing (1).bin"] == b"two"
    assert door.files["/workspace/thing (2).bin"] == b"three"


@pytest.mark.asyncio
async def test_the_numbering_ends_rather_than_loops(door, monkeypatch):
    """A hundred files with one name is not a collision, it is a loop."""
    monkeypatch.setattr(fu, "MAX_NAME_ATTEMPTS", 3)
    door.always_conflict = True
    with pytest.raises(fu.FileUploadError) as exc:
        await fu.upload_to_sandbox(b"payload", "taken.bin")
    assert exc.value.status == 409
    assert len(door.paths("POST")) == 3


@pytest.mark.asyncio
async def test_a_refusal_that_is_not_a_name_conflict_is_not_retried(door):
    """A 403 is not a taken name: retrying it would send the bytes again
    for a reason that retrying cannot fix."""
    door.reject_token = True
    with pytest.raises(fu.FileUploadError) as exc:
        await fu.upload_to_sandbox(PAYLOAD, "big.bin")
    assert exc.value.status == 401
    assert len(door.paths("POST")) == 1


def test_the_number_goes_before_the_extension():
    """The way a browser does it: the extension stays last."""
    assert fu.numbered_name("report.pdf", 1) == "report (1).pdf"
    assert fu.numbered_name("archive.tar.gz", 2) == "archive.tar (2).gz"
    assert fu.numbered_name("README", 3) == "README (3)"
    assert fu.numbered_name("name.with.dots.bin", 1) == "name.with.dots (1).bin"


def test_the_number_is_ours_and_the_callers_name_is_not():
    """The number is added after the name is made safe, so a stranger
    cannot smuggle a space or a parenthesis into a file name."""
    assert fu.safe_file_name("a b.bin") == "a_b.bin"
    assert fu.numbered_name(fu.safe_file_name("a b.bin"), 1) == "a_b (1).bin"


@pytest.mark.asyncio
async def test_a_file_over_the_limit_is_refused_with_its_size(door):
    door.limit = 1024
    with pytest.raises(fu.FileUploadError) as exc:
        await fu.upload_to_sandbox(PAYLOAD, "big.bin")
    assert str(len(PAYLOAD)) in exc.value.message
    assert "1024" in exc.value.message
    assert "/workspace/big.bin" not in door.files


@pytest.mark.asyncio
async def test_a_refused_token_is_named(door):
    door.reject_token = True
    with pytest.raises(fu.FileUploadError) as exc:
        await fu.upload_to_sandbox(PAYLOAD, "big.bin")
    assert "AGENT_STACK_TOKEN" in exc.value.message


@pytest.mark.asyncio
async def test_a_missing_directory_is_named(door):
    with pytest.raises(fu.FileUploadError) as exc:
        await fu.upload_to_sandbox(PAYLOAD, "big.bin", directory="/nowhere")
    assert "no such directory" in exc.value.message


@pytest.mark.asyncio
async def test_an_unreachable_sandbox_is_not_swallowed(door):
    door.unreachable = True
    with pytest.raises(fu.FileUploadError) as exc:
        await fu.upload_to_sandbox(PAYLOAD, "big.bin")
    assert "could not be reached" in exc.value.message


def test_the_name_is_made_safe():
    assert fu.safe_file_name("../../etc/passwd") == "passwd"
    assert fu.safe_file_name("a b.bin") == "a_b.bin"
    assert fu.safe_file_name("") == "file"
    assert fu.safe_file_name("...") == "file"
    assert fu.safe_file_name("report.pdf") == "report.pdf"
