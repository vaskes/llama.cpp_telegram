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


def _looks_like_session_expiry(status: int, text: str) -> bool:
    """True iff the answer says "I do not know your session".

    Measured shapes: HTTP 404 from the streamable transport, and a
    body that mentions the session and says "unknown". Both are the
    server asking for a new handshake, not a failure of the call.
    """
    if status == 404:
        return True
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

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call one tool and return its output as text.

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

    async def _call_once(self, tool_name: str, arguments: dict) -> str:
        data = await self._rpc("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        })
        result = data.get("result") or {}
        return self._render(result)

    def _render(self, result: dict) -> str:
        """Flatten an MCP tool result into text.

        MCP results are a list of content blocks. Text is what the
        model can read; an image block becomes a line saying what it
        was, because the bot has no image channel into the transcript
        and a base64 blob would eat the context. A result the server
        marked as an error says so -- the caller must not pass an
        error off as an answer.
        """
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
            return f"[mcp tool error: {text}]"
        return text

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
        if _looks_like_session_expiry(r.status_code, text):
            raise McpSessionExpired(
                f"{self.config.name}: the server does not know our session")
        if r.status_code >= 400:
            raise McpError(
                f"{self.config.name}: HTTP {r.status_code}: {text[:200]}")
        if notification:
            return {}, r.headers
        return _parse_body(r, text), r.headers
