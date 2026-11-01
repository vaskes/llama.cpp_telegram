"""MCP client: the protocol half of the bot's MCP support.

What this module is
-------------------
One class, one server. It speaks MCP's JSON-RPC over HTTP to a
single MCP server and knows three methods: ``initialize``,
``tools/list`` and ``tools/call``. It hands back what the server
said; it does not decide what the model may call -- that is the
registry's job (``mcp_registry``).

Why the session header is optional
----------------------------------
MCP's streamable HTTP transport hands out an ``Mcp-Session-Id`` on
``initialize`` and expects it back on every later call. The sandbox
side-car does not: measured on 2026-10-27 against the live proxy,
its ``initialize`` answer carries no such header and ``tools/list``
works without one. A client that insists on the header therefore
breaks on one of the two servers the bot actually talks to. So the
header is sent when the server gave one and omitted when it did not.

Why one re-initialize and no more
---------------------------------
A server that answers "I do not know this session" has forgotten us,
and the one thing that fixes it is a fresh ``initialize``. Anything
else -- a transport error, a JSON-RPC error, an HTTP status -- is the
server's answer and belongs to the user. One retry, then it is named.

What counts as "I do not know this session"
-------------------------------------------
Only what the WIRE said. The detector reads the status code, the body
of a failed request and a JSON-RPC error envelope -- and it never
reads the content of a successful answer, because that content is
data, and data may talk about sessions.

Measured on production, 2026-11-01: the file
``/workspace/characters.json`` carries the words "session", "unknown"
and "expired", and the detector -- reading the answer of a read that
had SUCCEEDED -- called the session forgotten, re-initialized, and
killed the file door. The model then fell back to the shell and got a
fragment of the file, and the operator was told it had read the file.

What this module does NOT do
----------------------------
* It does not know the tools by name. The registry decides which of
  them the model may see.
* It does not retry in a loop. A server that fails twice is down,
  and the bot has other things to do.
* It does not write anything anywhere. It reads and it calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from logconf import get_logger

log = get_logger(__name__)

#: The MCP revision the bot asks for. The servers in production
#: (donsetch-http and the sandbox side-car) answer 2024-11-05.
PROTOCOL_VERSION = "2024-11-05"

#: Who we say we are. The MCP handshake carries this; the sandbox
#: prints it in its diagnostics.
CLIENT_NAME = "llama.cpp_telegram_bot"
CLIENT_VERSION = "1.0"

#: The gates the config block may name. They are policy, not
#: protocol, but they belong to the server's record -- one entry, one
#: set of properties -- so they live next to the record and the
#: registry reads them from there.
GATE_ALWAYS = "always"
GATE_PROFILE = "profile"
GATE_DEV_CHAT = "dev-chat"


class McpError(Exception):
    """A failure we can name: the server said so, or the wire did."""


class McpSessionExpired(McpError):
    """The server no longer knows our session.

    The one error worth retrying: a fresh ``initialize`` is exactly
    what the server is asking for.
    """


@dataclass(frozen=True)
class McpFile:
    """A file the server sent us, and we can prove we hold all of it.

    The three fields are the server's own: it named the file, it said
    how many bytes the file has, and it said what type it is. What the
    client adds is the check -- the text it holds encodes to exactly
    ``size`` bytes, so this is the whole file and not a piece of one.

    A file we cannot vouch for is not a file: it is a tool output like
    any other, and it goes through the ordinary budget.
    """

    name: str
    size: int
    mime: str


@dataclass(frozen=True)
class McpResult:
    """One MCP tool call, answered.

    ``text`` is what the model will read. ``file`` is set only when the
    answer carried a complete file, so that the layers above can give
    that file its own budget instead of the ordinary tool budget.
    """

    text: str
    file: Optional[McpFile] = None


@dataclass(frozen=True)
class McpServerConfig:
    """One MCP server, as the operator configured it.

    ``include`` and ``exclude`` are filters on the BARE tool name --
    the name the server gave it, before the bot prefixes it. An empty
    ``include`` means "every tool the server offers"; ``exclude``
    always wins over ``include``.
    """

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    #: How long a TOOL CALL may take. A crawl is slow on purpose.
    timeout: float = 60.0
    #: How long the HANDSHAKE may take (initialize + tools/list). This
    #: one is short on purpose: it runs while the model is waiting for
    #: its tool list, and a server that cannot be reached in five
    #: seconds is not going to be reached in sixty either.
    connect_timeout: float = 5.0
    enabled: bool = True
    #: Which chats may use this server's tools: ``profile`` (every
    #: chat the deployment profile allows) or ``dev-chat`` (only the
    #: allowlisted chats, in dev mode). The registry enforces it.
    gate: str = GATE_PROFILE

    def wants(self, tool_name: str) -> bool:
        """True iff this server's tool is one the operator left on."""
        if self.include and tool_name not in self.include:
            return False
        return tool_name not in self.exclude


def _names_a_forgotten_session(text: str) -> bool:
    """True iff a TRANSPORT-level message says the session is gone.

    This reads what the wire said about the session, and nothing else.
    It must never be pointed at the content of a successful answer:
    that content is DATA, and data may talk about sessions.

    Measured on production, 2026-11-01: the file
    ``/workspace/characters.json`` carries the words "session",
    "unknown" and "expired", and the detector -- reading the ANSWER --
    called the session forgotten and killed the file door on a read
    that had succeeded.  The detector now looks at the status code, at
    the body of a failed request, and at a JSON-RPC error envelope.
    A successful result is never read for words.
    """
    lowered = (text or "").lower()
    return "session" in lowered and ("unknown" in lowered or "expired" in lowered)


def _sse_payloads(text: str):
    """Yield the JSON payloads of an SSE body.

    The streamable HTTP transport may answer with
    ``text/event-stream`` instead of a JSON body. Frames look like
    ``data: {...}``; a frame may hold more than one line, and the
    ``[DONE]`` sentinel is not JSON.
    """
    for block in (text or "").split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if not chunk or chunk == "[DONE]":
                continue
            yield chunk


def _parse_body(r: httpx.Response, text: str) -> dict:
    """Turn an MCP answer into a dict, whatever it arrived as.

    JSON is the normal case. SSE is the alternative the transport
    allows, and the sandbox side-car has been seen answering that
    way. An answer we cannot parse is an error we name -- not an
    empty result, because an empty result is a claim about the tool.
    """
    content_type = (r.headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type:
        last: dict = {}
        for chunk in _sse_payloads(text):
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(data, dict) and ("result" in data or "error" in data):
                last = data
        return last
    try:
        data = json.loads(text or "{}")
    except ValueError:
        raise McpError(
            f"unparseable answer from {r.request.url} "
            f"(status {r.status_code}, body {text[:200]!r})")
    if not isinstance(data, dict):
        raise McpError(
            f"unexpected answer shape from {r.request.url}: {text[:200]!r}")
    return data


def _file_facts(structured: dict, text: str) -> Optional[McpFile]:
    """The file the text IS, if the text is a complete file.

    The server names the file, its size in bytes and its type. We
    count our own bytes and compare: equal means we are holding the
    whole file, from its first byte to its last.

    Anything else is not a file. The facts line about a binary file is
    not the file; the metadata of a file we did not read is not the
    file; a piece of one is not the file. Those are tool outputs like
    any other, and they go through the ordinary budget -- with the cut
    marked, so nobody believes they saw the whole thing.

    Measured against the live side-car, 2026-11-01: a 511 949-byte
    file arrived as 462 099 characters, which encode to exactly
    511 949 bytes, and ``structuredContent`` said ``size: 511949``.
    """
    name = structured.get("name")
    size = structured.get("size")
    mime = structured.get("mime")
    if not isinstance(name, str) or not isinstance(mime, str):
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    if len(text.encode("utf-8")) != size:
        return None
    return McpFile(name=name, size=size, mime=mime)


class McpClient:
    """One MCP server, over HTTP."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._session_id: Optional[str] = None

    @property
    def session_id(self) -> Optional[str]:
        """The session the server gave us, or None for a stateless one."""
        return self._session_id

    # -- the three methods ---------------------------------------------

    async def initialize(self) -> Optional[str]:
        """Handshake. Returns the session id, or None if stateless."""
        data, headers = await self._post({
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        }, timeout=self.config.connect_timeout)
        if "error" in data:
            raise McpError(
                f"{self.config.name}: initialize failed: {data['error']}")
        # The session id lives in the HEADER, not in the body. Both
        # servers were checked: the sandbox sends none, donsetch sends
        # one, and in both cases it is in the response headers.
        self._session_id = headers.get("mcp-session-id") or None
        await self._notify_initialized()
        return self._session_id

    async def list_tools(self) -> list[dict]:
        """The tools the server offers, as the server spelled them."""
        data = await self._rpc("tools/list", {},
                               timeout=self.config.connect_timeout)
        tools = data.get("result", {}).get("tools") or []
        return [t for t in tools
                if isinstance(t, dict) and t.get("name")]

    async def call_tool(self, tool_name: str, arguments: dict) -> McpResult:
        """Call one tool and return its output.

        A session the server forgot is retried exactly once, after a
        fresh handshake -- that is the one failure the handshake
        fixes.
        """
        try:
            return await self._call_once(tool_name, arguments)
        except McpSessionExpired:
            log.info("[mcp] %s: session expired, re-initializing",
                     self.config.name)
            self._session_id = None
            await self.initialize()
            return await self._call_once(tool_name, arguments)

    # -- the wire ------------------------------------------------------

    async def _call_once(self, tool_name: str,
                         arguments: dict) -> McpResult:
        data = await self._rpc("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        })
        result = data.get("result") or {}
        text, file = self._render(result)
        return McpResult(text=text, file=file)

    def _render(self, result: dict) -> tuple[str, Optional[McpFile]]:
        """Flatten an MCP tool result into text, and say what it is.

        MCP results are a list of content blocks. Text is what the
        model can read; an image block becomes a line saying what it
        was, because the bot has no image channel into the transcript
        and a base64 blob would eat the context. A resource block --
        the shape a file comes back in when it is not text -- becomes
        the facts of the file, for the same reason and harder: a blob
        is megabytes of context for something no text model can read.
        A result the server marked as an error says so -- the caller
        must not pass an error off as an answer.

        The second half of the answer is the FILE the text is, when the
        server sent one: the side-car hands over a whole file and says
        how many bytes it has, and the client checks that the text it
        holds is that many bytes long.  What comes back is then known
        to be the whole file, and the layers above can give it the
        budget a file gets instead of the budget a tool result gets.
        """
        structured = result.get("structuredContent") or {}
        if not isinstance(structured, dict):
            structured = {}
        parts: list[str] = []
        for block in result.get("content") or []:
            if isinstance(block, dict):
                kind = block.get("type")
                if kind == "text":
                    parts.append(str(block.get("text") or ""))
                elif kind == "image":
                    data = block.get("data") or ""
                    parts.append(
                        f"[image: {block.get('mimeType', '?')}, "
                        f"{len(data)} bytes]")
                elif kind == "resource":
                    parts.append(self._render_resource(block, structured))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False)[:500])
            else:
                parts.append(str(block))
        text = "\n".join(part for part in parts if part)
        if not text:
            text = str(result.get("text") or "")
        if not text:
            text = "[mcp: empty result]"
        if result.get("isError"):
            return f"[mcp tool error: {text}]", None
        return text, _file_facts(structured, text)

    @staticmethod
    def _render_resource(block: dict, structured: dict) -> str:
        """One MCP resource block, as a line the model can use.

        A blob resource carries the file's bytes as base64. Those bytes
        are not for the model: they are a context window spent on
        something no text model can read, and the tool result is
        truncated on the way in, so a blob that is too big arrives as
        a fragment of base64 -- useless to the model and expensive to
        carry. What the model needs is the fact of the file: its name,
        its type, its size. The server usually says all three in
        ``structuredContent``; the block itself carries the mime type
        and the name as a URI.

        Measured on 2026-10-27 against the sandbox side-car v2.12.1:
        a binary file comes back as ``{"type": "resource", "resource":
        {"blob": ..., "mimeType": ..., "uri": "file://..."}}`` with
        ``structuredContent = {"name", "size", "mime"}``.
        """
        inner = block.get("resource")
        if not isinstance(inner, dict):
            inner = {}
        name = str(structured.get("name") or inner.get("uri") or "file")
        mime = str(structured.get("mime") or inner.get("mimeType")
                   or "application/octet-stream")
        blob = inner.get("blob")
        if isinstance(blob, str):
            size = structured.get("size")
            if not isinstance(size, int):
                size = (len(blob) * 3) // 4
            return (f"[binary file: {name} ({mime}), {size} bytes -- "
                    "the content is not inlined]")
        text = inner.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(block, ensure_ascii=False)[:500]

    async def _rpc(self, method: str, params: dict,
                   timeout: Optional[float] = None) -> dict:
        data, _ = await self._post({
            "jsonrpc": "2.0",
            "id": method,
            "method": method,
            "params": params,
        }, timeout=timeout)
        if "error" in data:
            raise McpError(
                f"{self.config.name}: {method} failed: {data['error']}")
        return data

    async def _notify_initialized(self) -> None:
        """Send the ``initialized`` notification the spec asks for.

        A server that does not want it answers with a status and an
        empty body; a server that never gets it may refuse the calls
        that follow. So the notification is sent, and a failure to
        send it is not fatal: the next method call will say so, in
        the open, if the server really needed it.
        """
        try:
            await self._post({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }, notification=True, timeout=self.config.connect_timeout)
        except McpError as e:
            log.info("[mcp] %s: initialized notification not accepted: %s",
                     self.config.name, e)

    async def _post(self, payload: dict, *, notification: bool = False,
                    timeout: Optional[float] = None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.config.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            budget = self.config.timeout if timeout is None else timeout
            async with httpx.AsyncClient(timeout=budget) as client:
                r = await client.post(self.config.url, headers=headers,
                                      json=payload)
        except Exception as e:
            raise McpError(
                f"{self.config.name}: cannot reach {self.config.url}: {e!r}"
            ) from e
        text = r.text or ""
        if r.status_code == 404:
            # The streamable transport's own answer for a session it
            # does not know. Measured against the live servers.
            raise McpSessionExpired(
                f"{self.config.name}: the server does not know our session")
        if r.status_code >= 400:
            if _names_a_forgotten_session(text):
                raise McpSessionExpired(
                    f"{self.config.name}: the server does not know our session")
            raise McpError(
                f"{self.config.name}: HTTP {r.status_code}: {text[:200]}")
        if notification:
            return {}, r.headers
        try:
            data = _parse_body(r, text)
        except McpError:
            # A body we cannot read is not a result. If it names the
            # session, it is the server asking for a new handshake.
            if _names_a_forgotten_session(text):
                raise McpSessionExpired(
                    f"{self.config.name}: the server does not know our session")
            raise
        if "error" in data and _names_a_forgotten_session(
                json.dumps(data["error"], ensure_ascii=False)):
            # A JSON-RPC error that names the session: the server wants
            # a new handshake, not a report of a failed call.
            raise McpSessionExpired(
                f"{self.config.name}: the server does not know our session")
        return data, r.headers
