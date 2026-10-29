"""Putting a file INTO the sandbox -- the door's other half.

The door the bot already spoke to only opened outwards: `GET
/files/meta`, `GET /files`, `DELETE /files`. A file could leave the
sandbox and could not come back in.

The operator found the gap with a 47.6 MiB binary: he uploaded it to
the chat and asked for it to be put in the sandbox, and the bot could
not even take the file off Telegram. The chat had no way in at all.

So the door grew its other half -- `POST /files`, sandbox v2.15.0 --
and this module is the bot's half of it: the same bearer token, the
same proxy, the same workspace, and the same honesty about what the
sandbox answered.

The direction of the trip matters for one reason. A file that goes out
is a file the model has already made; a file that comes in is a file
the model has never seen and cannot read -- it is bytes, not text, and
the only way to look at it is with the sandbox's own tools. So what
lands here is named, hashed and reported, and the model is told where
it is.

Nothing here deletes. The door refuses to overwrite a file that is
already there (409), because a delivery that removes what it was not
told to remove is a surprise, and the surprise is what the user has to
undo.
"""
from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

import httpx

from personal_tools.bash_command import AGENT_STACK_TOKEN, AGENT_STACK_URL

# The download half has the unwrapping of the sandbox's JSON envelope
# already: the agent answers an error as {"detail": ...}, and the proxy
# relays it as the body of its own error. One copy of that unwrapping
# lives in one place.
from personal_tools.file_transport import _detail

__all__ = [
    "FileUploadError",
    "UploadedFile",
    "safe_file_name",
    "upload_to_sandbox",
]

#: Where the sandbox keeps its files -- the same root the model is told
#: to use when it writes something the bot should send out.
SANDBOX_WORKSPACE = "/workspace"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class FileUploadError(Exception):
    """The sandbox refused the upload, or could not be reached.

    The message says what happened and what the sandbox said: "the
    sandbox already has a file with that name" is something the user
    can act on, and "something went wrong" is not.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class UploadedFile(NamedTuple):
    """What landed in the sandbox."""

    name: str      # the name the file has in the sandbox
    path: str      # its absolute path there
    size: int      # bytes written
    sha256: str    # digest of what was written


def safe_file_name(name: str) -> str:
    """A file name the door will take, whatever the user called it.

    The name comes from Telegram, which means it comes from a stranger:
    a path in it ("../../etc/passwd"), a name the filesystem cannot
    hold, a name that is only dots. The basename is taken, the rest is
    flattened, and the result is never empty.
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _UNSAFE.sub("_", base).lstrip(".")[:200]
    return base or "file"


def _error_for_status(status: int, body: str) -> str:
    """A sentence about a refusal, for the model and for the user.

    The statuses mean different things, and the difference is what the
    user has to act on: a token the sandbox will not take is a
    configuration problem, a name that is already taken is a choice,
    and a directory that is not there is a path.
    """
    detail = _detail(body)
    if status == 401:
        return (f"the sandbox rejected AGENT_STACK_TOKEN (HTTP 401); "
                f"check the token in the bot's environment")
    if status == 403:
        return f"the sandbox refused to write the file: {detail}"
    if status == 404:
        return f"the sandbox has no such directory: {detail}"
    if status == 409:
        return f"the sandbox already has a file with that name: {detail}"
    if status == 413:
        return f"the sandbox refused the file: {detail}"
    if status == 502:
        return f"the sandbox agent is unreachable: {detail}"
    return f"HTTP {status}: {detail}"


async def upload_to_sandbox(
    content: bytes,
    name: str,
    *,
    directory: str = SANDBOX_WORKSPACE,
    timeout: float = 300.0,
) -> UploadedFile:
    """Put the bytes into the sandbox, and say what landed.

    The body is the file's bytes -- no base64, no envelope: the door
    takes the same stream the download gives, so a file crosses the
    proxy without paying a third of its size for the encoding.
    """
    file_name = safe_file_name(name)
    path = f"{directory.rstrip('/')}/{file_name}"
    headers = {
        "Authorization": f"Bearer {AGENT_STACK_TOKEN}",
        "Content-Type": "application/octet-stream",
    }
    url = f"{AGENT_STACK_URL}/files"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                url, params={"path": path}, content=content, headers=headers)
    except httpx.HTTPError as e:
        raise FileUploadError(
            f"the sandbox could not be reached ({e!r})") from e
    body = r.text or ""
    if r.status_code != 200:
        raise FileUploadError(_error_for_status(r.status_code, body),
                              status=r.status_code)
    data: object
    try:
        data = r.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        # The proxy answers 200 with {"error": ...} when the agent
        # refused the path -- the quirk the download half survives too.
        if "error" in data and "path" not in data:
            raise FileUploadError(_detail(body))
        size = int(data.get("size") or len(content))
        path = str(data.get("path") or path)
        file_name = str(data.get("name") or file_name)
    else:
        size = len(content)
    return UploadedFile(
        name=file_name,
        path=path,
        size=size,
        sha256=hashlib.sha256(content).hexdigest(),
    )
