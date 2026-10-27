"""T38.5: Tool isolation between bot and llama-server.

Threat model
------------
llama-server may be launched with --agent and / or MCP-servers-config,
which gives the model access to native tools (shell_tool, exec, etc.)
defined inside llama.cpp itself. If we blindly execute any
tool_calls[i].function.name the model emits, the LLM can:

  - call a native "shell" tool and have llama-server run shell
    commands directly in its own process context, completely
    bypassing our vaskes/OpenWebUI-bash-sandbox side-car,
  - call a native MCP-loaded tool that we never whitelisted,
  - call any tool name the model hallucinated, and we would
    dispatch it as if it were ours.

Defence in depth
----------------
The isolation property lives on the BOT SIDE, not on the llama side.

  1. The bot advertises ONLY the tool names it knows how to dispatch
     (known_tool_names()). llama-server is free to have its own tools;
     we just don't tell the model about them.
  2. The bot REJECTS every tool_calls response that contains a
     function name the bot does not know. The rejection happens
     BEFORE we look up how to execute the tool, so we never run
     something we did not define.
  3. The bot logs a clear error when an unknown tool name appears
     (e.g. "llama-server returned a tool_call for native 'shell';
     this means --agent or MCP-servers-config is in use and the
     model is reaching for a non-bot tool").

Maintenance
-----------
When a new tool is added to the bot, its name MUST be added to
NATIVE_TOOL_NAMES in this file. The unit tests in
tests/test_tool_isolation.py enumerate this set and will fail
loudly if a new tool ships without being registered. See
docs/TOOL-ISOLATION.md for the full threat model and the
maintenance checklist.

Two classes of tools, and what the whitelist means
--------------------------------------------------
NATIVE tools are the bot's own -- Telegram, and the shell. Their
names live in NATIVE_TOOL_NAMES, which is a constant.

IMPORTED tools are somebody else's: they come from MCP servers, and
they are discovered rather than declared (see mcp_registry.py). Their
names are not in the constant -- they cannot be, they do not exist
until a server answers -- so they are added by known_tool_names(),
which is the union the validators actually use.

The split matters because the two classes have different owners. A
native tool is dispatched by a branch in llm_dispatch.py, and that
branch is what the tripwires check. An imported tool is dispatched by
the registry, which only ever calls the server it discovered the tool
on -- so the model cannot make the bot call a server the operator did
not configure, and cannot name a tool that server does not have.
"""

from __future__ import annotations

from typing import Iterable

from logconf import get_logger

log = get_logger(__name__)


#: Every NATIVE function name the bot knows how to dispatch. Adding
#: a new native tool = adding a name here AND shipping a handler.
#: Imported (MCP) tools are not here: they are found at runtime, and
#: known_tool_names() is what adds them.
NATIVE_TOOL_NAMES: frozenset[str] = frozenset({
    # donsetch_* tools: implemented in bot.py, dispatched over
    # HTTP to donsetch-http at 127.0.0.1:8765/mcp. They do
    # NOT touch the host shell; they only call out to a
    # separate MCP server. See bot.py:dispatch_one_tool.
    "donsetch_web_search",
    "donsetch_web_fetch",
    "donsetch_web_crawl",
    "donsetch_web_screenshot",
    # react_to_message: implemented in handlers.py, just sets a
    # Telegram reaction emoji. Read-only with respect to shell.
    "react_to_message",
    # bash_command: implemented in personal_tools/bash_command.py,
    # forwards to the vaskes/OpenWebUI-bash-sandbox side-car over
    # HTTP. Available only in personal profile.
    "bash_command",
    # send_file: implemented in llm_dispatch.py, reads a file out of
    # the same sandbox side-car and sends it to the chat as a Telegram
    # document. It reaches no further than bash_command does -- the
    # sandbox user can already read whatever the model can read -- but
    # it hands the model a way to move BYTES, which the answer-files
    # feature cannot do: a code block becomes a document, and a binary
    # has no text form. Without this name in the list the isolation
    # layer soft-rejects the call and the model loops on it.
    "send_file",
    # delete_messages: implemented in llm_delete.py, removes messages
    # from the chat the model is talking in. It reaches no further than
    # that chat -- there is no chat_id parameter, so the model's reach
    # is the conversation it is in. The bot's own database is NOT
    # touched: the history is the record of what happened.
    "delete_messages",
    # search_messages: implemented in llm_history.py, reads the bot's own
    # record of the chat it is in. It is a READ: the tool looks in the
    # messages table and returns what it finds, and it reaches no chat
    # other than the one it is in -- there is no chat_id parameter. The
    # ids it returns are the ones the delete tool may act on, because
    # the record is where a message said hours ago survives with its id
    # attached.
    "search_messages",
})


class UnknownToolNameError(Exception):
    """Raised when llama-server returns a tool_call whose function
    name is not known to the bot. The bot must NEVER execute such a
    call; the whole response (or at least that tool_call) is rejected
    and a clear error is sent back to the user.
    """

    def __init__(self, unknown_names: list[str], known: Iterable[str]):
        self.unknown_names = sorted(set(unknown_names))
        self.known = sorted(set(known))
        super().__init__(
            f"llama-server returned tool_calls for unknown name(s) "
            f"{self.unknown_names!r}; known = {self.known!r}. "
            f"This is a tool-isolation violation: the bot will not "
            f"execute it. If you are seeing this in production, "
            f"llama-server is likely running with --agent and / or "
            f"MCP-servers-config, and the model is reaching for a "
            f"native tool the bot does not manage."
        )


#: The names the MCP registry found at its last refresh. The registry
#: writes them here (register_imported_tool_names) instead of the
#: isolation layer reaching for the registry: one direction, and no
#: import cycle between the two.
_IMPORTED_TOOL_NAMES: frozenset[str] = frozenset()


def register_imported_tool_names(names: Iterable[str]) -> None:
    """The registry tells the isolation layer what the servers offer.

    Called by the registry after every refresh, including a refresh
    that found nothing: a server that went away takes its tools out
    of the whitelist, and the model that still remembers one gets a
    refusal instead of a call to a server that is not there.
    """
    global _IMPORTED_TOOL_NAMES
    _IMPORTED_TOOL_NAMES = frozenset(names)


def known_tool_names() -> frozenset[str]:
    """Every name the bot may execute: the native ones, plus the
    tools imported from MCP servers.

    The native half is a constant. The imported half is whatever the
    registry found at its last refresh -- a server that is down
    contributes nothing, and the native tools are unaffected, which
    is the whole point of keeping the two classes apart.

    Callers MUST go through this rather than reading
    NATIVE_TOOL_NAMES directly, so that the validators and the
    dispatcher see the same set.
    """
    return NATIVE_TOOL_NAMES | _IMPORTED_TOOL_NAMES


def is_known_tool_name(name: str) -> bool:
    """True iff `name` is in the bot's whitelist."""
    return name in known_tool_names()


def extract_function_names(tool_calls: list) -> list[str]:
    """Return the function name for every tool_call in the list,
    in order. Robust to malformed entries (missing 'function' or
    missing 'name'): an empty string is returned for the bad
    entry, which the caller will then see as 'not in the
    whitelist' and reject.
    """
    names: list[str] = []
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            names.append("")
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            names.append("")
            continue
        names.append(fn.get("name") or "")
    return names


def validate_tool_calls(tool_calls: list) -> list[str]:
    """Return the list of function names from `tool_calls`,
    raising UnknownToolNameError if any name is not known to the
    bot.

    This is the single guard the bot must call before executing
    any tool. It is intentionally strict: even ONE unknown name
    rejects the whole batch, because we cannot know whether the
    LLM has already executed that tool inside llama-server (and
    is now handing us a forged "result" to swallow), or whether
    the LLM expects us to execute it client-side (which we
    won't). In both cases the right move is to reject and tell
    the user.
    """
    names = extract_function_names(tool_calls)
    known = known_tool_names()
    unknown = [n for n in names if n not in known]
    if unknown:
        raise UnknownToolNameError(unknown, known)
    return names
