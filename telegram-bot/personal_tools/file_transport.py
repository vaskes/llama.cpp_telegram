"""Getting a file out of the sandbox, and leaving one copy behind.

The sandbox side (`vaskes/OpenWebUI-bash-sandbox`, at the version this
repo pins -- see `sandbox_contract.py`) publishes a file door next to
its shell door:

| Method | Path             | What it does                             |
|--------|------------------|------------------------------------------|
| GET    | `/files/meta?path=P` | size, name, mtime, and the sandbox's own limit |
| GET    | `/files?path=P`  | the file's **raw bytes**, streamed        |
| DELETE | `/files?path=P`  | remove the file from the sandbox (204)   |

A refusal is a status, and the statuses mean different things:

| Status | What it means |
|---|---|
| 401 | the bearer token was refused -- the bot's own configuration is wrong |
| 403 | the agent cannot read the file: its owner or mode does not allow it |
| 404 | there is no such file |
| 413 | the file is over the sandbox's own size limit |

The 403 is the one this module used to get wrong, and it got it wrong
because the sandbox did. Before the sandbox v2.9.1 the door opened the
file lazily, after the 200 and the Content-Length were already on the
wire, so a file the agent could not read came back as a truncated body.
The client read that as a broken connection and said "the sandbox
side-car is unreachable" -- which sends the operator to look at the
network instead of at the file's mode. A short read is a transport; a
403 is a permission.

Before that, the only way out was base64 through the terminal pane, and
that path had a ceiling of roughly 675 KB of real file: base64 is four
thirds of the file, the pane is bounded, and when the pane overflowed it
dropped the *head* silently -- so the payload came back as a tail that
decoded into garbage, with nothing in the answer to say so.

This module is the courier. It asks for the metadata first, so a file
that is over the sandbox's own limit is refused before a single byte is
pulled; it streams the bytes into a temp file while hashing them; and
it deletes the sandbox copy once the caller says the file was
delivered, because a file that lives in two places at once lives in the
one nobody is looking at as often as in the one they are.

Everything here speaks HTTP to the same proxy the shell tool speaks to,
with the same bearer token: `AGENT_STACK_URL` and `AGENT_STACK_TOKEN`.
The reach is the model's own reach -- the sandbox user can already read
whatever the model can read -- so this hands the model nothing it did
not have.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, NamedTuple

import httpx

from personal_tools.bash_command import AGENT_STACK_TOKEN, AGENT_STACK_URL

__all__ = [
    "FileTransportError",
    "FetchedFile",
    "file_meta",
    "fetch_to_temp",
    "purge",
]


class FileTransportError(Exception):
    """The sandbox refused the file, or could not be reached.

    The message is written for the model that will read it: it says what
    happened and what the numbers were. "Something went wrong" is not an
    answer anybody can act on.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class FetchedFile(NamedTuple):
    """A file that has been pulled out of the sandbox onto local disk."""

    path: str      # a temp file; the caller owns it and must unlink it
    name: str      # the name the user will see
    size: int      # bytes written
    sha256: str    # digest of what was written


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AGENT_STACK_TOKEN}"}


def _detail(body: str) -> str:
    """The sandbox's error message, unwrapped from its JSON envelope.

    The agent answers an error as ``{"detail": "..."}``. The proxy
    relays that as the body of its own error, and its meta route
    answers 200 with ``{"error": "..."}`` when the agent refused the
    path. Both carry the sentence that says what happened -- for a file
    over the limit it names the real size, which is the number the model
    needs.
    """
    text = (body or "").strip()
    if not text:
        return "no detail"
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(parsed, dict):
        for key in ("detail", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                # The proxy's meta route puts the agent's JSON document
                # into "error" as a STRING, so one more level to unwrap.
                if value.lstrip().startswith("{"):
                    return _detail(value)
                return value[:500]
    return text[:500]


def _error_for_status(status: int, body: str) -> str:
    """A sentence about a refusal, for the model and for the user."""
    detail = _detail(body)
    if status == 404:
        return f"there is no such file in the sandbox ({detail})"
    if status == 413:
        return f"the sandbox refused the file: {detail}"
    if status == 400:
        return f"the sandbox refused the path: {detail}"
    if status == 401:
        return (f"the sandbox rejected AGENT_STACK_TOKEN (HTTP 401); "
                f"check the token in the bot's environment")
    if status == 403:
        # The token is not in question: the proxy answers 401 for a bad
        # token, and the agent answers 403 for a file it cannot read.
        # Before the sandbox v2.9.1 this was a truncated body, and the
        # client called it an unreachable side-car -- a diagnosis that
        # sends the operator to the network instead of to the file's
        # mode. The detail names the file and says what to do about it.
        return f"the sandbox refused to read the file: {detail}"
    if status == 502:
        return f"the sandbox agent is unreachable: {detail}"
    return f"HTTP {status}: {detail}"


def _parse_version(value: Any) -> tuple[int, ...] | None:
    """``"2.7.1"`` -> ``(2, 7, 1)``; anything else -> ``None``."""
    if not isinstance(value, str):
        return None
    parts: list[int] = []
    for chunk in value.strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or None


async def _sidecar_version(*, timeout: float = 10.0) -> tuple[int, ...] | None:
    """The side-car's own version, if it will say.

    The proxy answers ``/api/config`` with its version. It is the only
    place that says how old the side-car is, and the difference is not
    cosmetic: before v2.7.0 there was no file door at all, and from
    v2.7.1 a missing file is a plain 404. One 404 means "there is no
    such file"; the other means "this side-car has never heard of a
    door". A client that confuses them blames the file for the age of
    the side-car -- and the age of the side-car is the thing to fix.
    """
    url = f"{AGENT_STACK_URL}/api/config"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=_headers())
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return _parse_version(data.get("version"))


async def file_meta(path: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """What the sandbox knows about the file: its size, name and limit.

    The proxy answers 200 with ``{"error": ...}`` when the agent refused
    the path, so the body has to be read as well as the status.
    """
    url = f"{AGENT_STACK_URL}/files/meta"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params={"path": path}, headers=_headers())
    except httpx.TimeoutException:
        raise FileTransportError("the sandbox side-car timed out") from None
    except httpx.HTTPError as e:
        raise FileTransportError(
            f"the sandbox side-car is unreachable: {e!r}") from None
    if r.status_code != 200:
        if r.status_code == 404:
            # Two different 404s live here. From v2.7.1 the proxy relays
            # the agent's status, so a 404 means "no such file". Before
            # the file door existed the route itself was missing, and the
            # side-car answers 404 for that too. The side-car's own
            # version tells the two apart -- and if it will not say, the
            # file is the more likely explanation.
            version = await _sidecar_version(timeout=timeout)
            if version is not None and version < (2, 7, 0):
                raise FileTransportError(
                    "the sandbox side-car has no file door (it answered 404 "
                    "for /files/meta and it is version "
                    f"{'.'.join(str(n) for n in version)}) -- it is older "
                    "than sandbox v2.7.0",
                    status=404)
        # Any other refusal belongs here, not inside the 404 branch: a
        # 403 (the agent cannot read the file), a 401 (the token) and a
        # 502 (the agent is down) were all falling through to the
        # metadata parser, which read the error document as if it were
        # a description of the file. The bot then believed it knew the
        # size of a file it had not been told about.
        raise FileTransportError(
            _error_for_status(r.status_code, r.text), status=r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise FileTransportError(
            f"the sandbox sent non-JSON metadata: {r.text[:200]}") from None
    if not isinstance(data, dict):
        raise FileTransportError(
            f"the sandbox sent unexpected metadata: {r.text[:200]}")
    if data.get("error"):
        raise FileTransportError(_detail(str(data["error"])))
    return data


async def fetch_to_temp(path: str, *, timeout: float = 120.0) -> FetchedFile:
    """Pull the file out of the sandbox into a temp file, hashing it.

    The bytes are streamed: the proxy relays them as they come off the
    disk and this writes them to a temp file as they arrive, so the
    transfer does not depend on the file fitting in the bot's memory.
    The digest is computed on the way through -- the digest of what was
    delivered is then the digest of what is on disk, and the model can
    compare it with its own ``sha256sum``.
    """
    url = f"{AGENT_STACK_URL}/files"
    fd, tmp_path = tempfile.mkstemp(prefix="llamabot-sendfile-")
    os.close(fd)
    digest = hashlib.sha256()
    written = 0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET", url, params={"path": path}, headers=_headers()
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    raise FileTransportError(
                        _error_for_status(r.status_code, body),
                        status=r.status_code)
                with open(tmp_path, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        fh.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
    except FileTransportError:
        _unlink(tmp_path)
        raise
    except httpx.TimeoutException:
        _unlink(tmp_path)
        raise FileTransportError(
            "the sandbox side-car timed out while sending the file") from None
    except httpx.HTTPError as e:
        _unlink(tmp_path)
        raise FileTransportError(
            f"the sandbox side-car is unreachable: {e!r}") from None
    except BaseException:
        _unlink(tmp_path)
        raise
    return FetchedFile(
        path=tmp_path,
        name=os.path.basename(path) or "file.bin",
        size=written,
        sha256=digest.hexdigest(),
    )


async def purge(path: str, *, timeout: float = 30.0) -> str:
    """Delete the sandbox copy, and say what happened.

    The sandbox is a scratchpad, not storage. Once the file has been
    delivered, the copy in the workspace is the second copy of the same
    artifact -- and the one nobody is looking at.

    A failure here is not a failure of the delivery: the file is already
    in the chat. So the outcome is a sentence for the model, not an
    exception.
    """
    url = f"{AGENT_STACK_URL}/files"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.delete(url, params={"path": path},
                                    headers=_headers())
    except httpx.TimeoutException:
        return "the sandbox copy could not be removed (the side-car timed out)"
    except httpx.HTTPError as e:
        return f"the sandbox copy could not be removed ({e!r})"
    if r.status_code == 204:
        return "the sandbox copy was removed"
    if r.status_code == 404:
        return "the sandbox copy was already gone"
    return ("the sandbox copy could not be removed ("
            f"{_error_for_status(r.status_code, r.text)})")


def _unlink(path: str) -> None:
    """Remove a temp file, and do not care if it is already gone."""
    try:
        os.unlink(path)
    except OSError:
        pass
