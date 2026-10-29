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
    await fu.upload_to_sandbox(b"first", "artifact.bin")
    with pytest.raises(fu.FileUploadError) as exc:
        await fu.upload_to_sandbox(b"second", "artifact.bin")
    assert "already has a file with that name" in exc.value.message
    # The file that was there is still the file that was there.
    assert door.files["/workspace/artifact.bin"] == b"first"


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
