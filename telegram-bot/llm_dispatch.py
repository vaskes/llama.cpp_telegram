"""Executing one tool call.

TECH-DEBT #8, Block D4 -- the largest and last leaf off the tool loop.

Before this, four ways of running a tool were an if/elif chain in the
middle of a 654-line loop, ~130 lines, and the only way to test any of
them was to make the model emit that exact call. The bash branch alone
has a late import, a profile gate, an empty-command branch and a ledger
entry with an extra field.

What this module owns is one call: name plus parsed arguments in, one
result plus three facts about it out. The loop keeps what only the loop
knows -- the iteration counter, the stuck-call ledger, the messages it
appends.

Deliberately not in here:

- **argument screening.** `parse_tool_args` already has its own module.
  Screening and execution are separate decisions: one rejects, the other
  runs.
- **the ledger and the message append.** Both are loop state. What
  travels back is a plain entry the loop appends if it got one.
- **the unavailable-tool message text.** It lists the tools by name, and
  that list has to be updated when tools are added -- see the constant.

One asymmetry worth stating, because it looks like a bug and is not: the
ledger entry for `bash_command` carries an extra `ok` field that the
other three do not, and it is only appended when a command was actually
run. The other outcomes of the bash branch -- unavailable, empty command
-- leave no trace in the turn ledger, which is what makes a
"bash did not run" turn visible in the logs while looking clean in the
ledger.

`result_is_empty` is a heuristic on the text of a result, not a status.
Tool executors return strings, several of them prose, and there is no
uniform signal to key on. It is kept as its own function because it is
pure and because it is the kind of predicate that will be wrong one day
and should then be changed in one place with one test.
"""
import os
from typing import Any, NamedTuple, Optional

import mcp_registry
from llm_truncation import condense
from rating import _execute_react_to_message
from tool_isolation import known_tool_names

def unavailable_tool_message(name: str) -> str:
    """What the model is told about a tool the bot cannot run.

    The list of tools is read from the whitelist, not written by
    hand. The hand-written version had already gone stale: it did
    not mention `delete_messages` or `search_messages`, which the bot
    has been executing since October. A list that lies about what the
    bot can do is worse than no list -- the model reads it as the
    whole menu.

    Some of the tools are available only in personal mode; the
    message says so rather than pretending otherwise.
    """
    known = ", ".join(sorted(known_tool_names()))
    return (
        f"[tool {name} unavailable in this mode. Bot supports: {known}. "
        f"Some of them are available only in personal mode.]"
    )

# T57 #S16: never trust the LLM's session_id. Deterministic on purpose.
#
# T51 C5: the persistent-shell feature is the reason. cwd, env and
# history are tmux-bound in the sandbox, so they survive across calls
# only when the same session_id is reused. A random per-call id would
# give every command a fresh shell and silently drop the persistence the
# tool is named for.
#
# T57 #S16 then closed the other half: the model must not choose this
# either, or a prompt injection could forward a command into another
# chat's session. chat_id is the only input.
SESSION_ID_TEMPLATE = "chat-{chat_id}"

MAX_TOOL_RESULT_CHARS = 12000

#: The channel's own ceiling for a document.
#:
#: Telegram's public Bot API accepts 50 MB in `sendDocument` (2000 MB with
#: a local telegram-bot-api). The sandbox has its own limit -- the one
#: `AGENT_BINARY_LIMIT` sets -- and the smaller of the two is the one
#: that can actually be used. This is the channel's half of that
#: statement, and it is checked BEFORE the bytes are pulled, so a file
#: nobody could have delivered is never downloaded.
MAX_SEND_FILE_BYTES = 50 * 1024 * 1024


class DispatchDeps(NamedTuple):
    """Everything a handler might need that is not its arguments.

    A NamedTuple rather than eight keyword arguments, because the call
    site is one line and this keeps it that way.
    """

    bot: Any
    chat_id: Any
    current_message_id: Any
    chat_type: str
    iteration: int
    custom_tools: dict
    donsetch_tools: dict
    # The id of the message the user replied to, when they replied to
    # one. It is the only id of ANOTHER person's message the bot can
    # know, and the delete tool may only touch ids it can vouch for
    # (see llm_delete.authorized_ids).
    reply_to_message_id: Any = None


class ToolExecution(NamedTuple):
    """One executed tool call."""

    text: str                       # the whole result, uncapped
    empty: bool = False             # does not count as a real result
    unavailable: bool = False       # the name was not recognised
    ledger: Optional[dict] = None   # what to record for this turn


def result_is_empty(text: str) -> bool:
    """Heuristic: this result should not count as the turn making progress.

    Deliberately string-shaped. The executors return prose -- a search
    that found nothing says "0 results", a fetch that failed says
    "[fetch error ...]" -- and there is no status object behind any of
    it to key on instead.

    Note what it does not match: "[bash_command unavailable: ...]" and
    "[react_to_message unavailable...]" are *not* empty by this rule,
    so they do count as real results. That is pre-existing behaviour and
    it is pinned rather than corrected here, because "correct" would mean
    deciding whether a refused tool is progress, which is a product
    question.
    """
    lowered = text.lower()
    return (
        "0 results" in lowered
        or "engines unavailable" in lowered
        or text.startswith("[donsetch")
        or text.startswith("[fetch error")
        or text.startswith("[tool")
        or text.startswith("[mcp")
    )


def _entry(name: str, args, iteration: int, **extra) -> dict:
    return {"name": name,
            "args": args if isinstance(args, dict) else {},
            "iter": iteration,
            **extra}


async def execute_tool(name: str, args, deps: DispatchDeps) -> ToolExecution:
    """Run one already-screened tool call and describe what happened."""
    if name == "react_to_message":
        result = await _execute_react_to_message(
            args,
            bot=deps.bot,
            chat_id=deps.chat_id,
            default_message_id=deps.current_message_id,
        )
        return _finish(result, _entry("react_to_message", args, deps.iteration), deps)

    if name == "bash_command":
        return await _execute_bash(args, deps)

    if name == "send_file":
        return await _execute_send_file(args, deps)

    if name == "delete_messages":
        # The chat's own messages, removed by Telegram's own API. The
        # bot's history is not touched: the record of what happened
        # stays (the operator's rule, 2026-10-26).
        #
        # The ids the tool may act on are looked up HERE and handed to it
        # as a parameter. The tool does not read the database -- it cannot,
        # and that is checked -- and a deletion in the chat must not
        # reach the record.
        from llm_delete import execute_delete_messages, requested_ids
        from llm_history import known_ids
        vouched = await known_ids(deps.chat_id, requested_ids(args))
        text, extras = await execute_delete_messages(
            args, bot=deps.bot, chat_id=deps.chat_id,
            chat_type=deps.chat_type,
            reply_to_message_id=deps.reply_to_message_id,
            current_message_id=deps.current_message_id,
            known_ids=vouched)
        ledger = _entry("delete_messages", args, deps.iteration,
                         **(extras or {}))
        return _finish(text, ledger, deps)

    if name == "search_messages":
        # The bot's own record of this chat: the messages it was sent,
        # the ones it absorbed from the group, and its own answers. It is
        # a read; the record is not a scratch pad.
        from llm_history import run_search
        text, extras = await run_search(args, chat_id=deps.chat_id)
        ledger = _entry("search_messages", args, deps.iteration,
                        **(extras or {}))
        return _finish(text, ledger, deps)

    imported = mcp_registry.get_registry()
    if name in imported.known_names():
        return await _execute_mcp(name, args, deps)

    registry = deps.custom_tools or {}
    if name in registry:
        result = await registry[name](args)
        return _finish(result, _entry(name, args, deps.iteration), deps)

    registry = deps.donsetch_tools or {}
    if name in registry:
        result = await registry[name](args)
        return _finish(result, _entry(name, args, deps.iteration), deps)

    text = unavailable_tool_message(name)
    return ToolExecution(text=text, empty=True, unavailable=True, ledger=None)


async def _execute_bash(args, deps: DispatchDeps) -> ToolExecution:
    """The personal-profile-only shell tool.

    The gate is repeated from the tool advertisement on purpose: the
    advertisement decides what the model is *told* it can call, this
    decides what actually runs. A call can also arrive from a caller
    passing `tools=` directly, which never went through the gate.
    """
    try:
        from personal_tools import bash_command as _bc
    except Exception as e:
        return ToolExecution(text=f"[bash_command unavailable: {e!r}]")

    if not _bc.is_enabled_for_active_profile():
        return ToolExecution(text=(
            "[bash_command unavailable: requires "
            "LLAMABOT_DEPLOYMENT_PROFILE=personal "
            "and AGENT_STACK_TOKEN. This chat is "
            "not in personal mode.]"
        ))

    # args is always a dict here: parse_tool_args normalises a non-object
    # parse to {} rather than rejecting it. See llm_toolargs.py step 4.
    command = args.get("command", "")
    if not command:
        return ToolExecution(text="[bash_command: empty command]")

    response = await _bc.call_bash_command(
        command,
        session_id=SESSION_ID_TEMPLATE.format(chat_id=deps.chat_id),
        chat_type=deps.chat_type,
        chat_id=deps.chat_id,
    )
    return _finish(
        _bc.tool_result_to_text(response),
        _entry("bash_command", args, deps.iteration,
               ok=response.get("ok", False),
               # The stdout rides along in the ledger so the chat can
               # show what the command printed. The operator's rule
               # (2026-10-24): the console output belongs in the chat.
               # Before this, the output reached the user only through
               # the model's paraphrase -- or, worse, as a .txt
               # document, because the model pasted it into a code
               # block and the file feature took it out of the chat.
               stdout=_ledger_stdout(response.get("stdout", "") or "")),
        deps,
    )


#: How much of the sandbox's output the ledger keeps.
#:
#: The ledger is written to the database and to the log, once per tool
#: call, and the log is a bind-mount that outlives the container. A
#: ledger that carries a whole command's output turns every command
#: into kilobytes of database rows and log lines.
MAX_LEDGER_STDOUT_CHARS = 2000


def _ledger_stdout(stdout: str) -> str:
    """The command's output, bounded, with the cut marked.

    The sandbox's stdout is this command's output and only this
    command's (sandbox v2.7.0). It used to be a whole terminal pane --
    the echoed command, the prompt, and every command the session ran
    before -- which is why this used to keep the tail: the tail was the
    part that belonged to the command that just ran.

    Now the head is the part that belongs to the command. The sandbox
    prints its "the beginning of this output no longer fits in the
    session scrollback" marker on the FIRST line, and a ledger that
    keeps the tail throws that marker away: the chat is shown a fragment
    as if it were the whole thing, which is the defect the sandbox fixed
    on its side. `condense` keeps both ends and says what it dropped in
    between, so the marker survives and the end the model quotes back
    survives too.
    """
    return condense(stdout, MAX_LEDGER_STDOUT_CHARS)


def _as_int(value, default: int = 0) -> int:
    """An int from the sandbox's metadata, or the default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _execute_send_file(args, deps: DispatchDeps) -> ToolExecution:
    """Send a file from the sandbox workspace to the chat.

    The operator asked for a zip, then for a 10 KB binary, and both
    times the file stayed in the sandbox: the only way out of the
    sandbox was the model's own text, so a binary had no way to reach
    the chat at all. The model could paste base64, and did -- 180 lines
    of it, in a message.

    So the bot fetches the bytes and sends them. The fetch is the same
    sandbox the model writes into, through the same side-car, so the
    file travels no further than the model could have read it itself.

    The route is the sandbox's file door (`GET /files?path=`), not
    base64 through the terminal pane. The pane route was bounded by the
    scrollback -- it lost the *head* silently when it overflowed, so the
    payload decoded into garbage -- and it paid four thirds of the file
    in size for the privilege.

    The order is: ask what the file is, pull it, deliver it, and stop.
    The sandbox copy stays where it is.

    An earlier version deleted it after the delivery -- one copy, not
    two, the workspace is a scratchpad. The operator overruled that on
    2026-10-27: nobody had asked for the file to be deleted, and the
    workspace is his to look at. A delivery that removes what it was
    not told to remove is a delivery that surprises, and the surprise
    is what the user has to undo.
    """
    path = str(args.get("path", "") or "").strip()
    if not path:
        return ToolExecution(text="[send_file: no path given]")
    if not path.startswith("/"):
        # The old route ran the path through the sandbox's shell, so a
        # relative path was resolved against the session's cwd. The file
        # door has no session: the sandbox resolves paths inside its
        # workspace, and a relative path is not a path there.
        return ToolExecution(text=(
            f"[send_file: {path} is not an absolute path. Give the path "
            f"inside the sandbox workspace, e.g. /workspace/{path}]"))

    try:
        from personal_tools import bash_command as _bc
        from personal_tools import file_transport as _ft
    except Exception as e:
        return ToolExecution(text=f"[send_file unavailable: {e!r}]")

    if not _bc.is_enabled_for_active_profile():
        return ToolExecution(text=(
            "[send_file unavailable: requires the sandbox ("
            "LLAMABOT_DEPLOYMENT_PROFILE=personal or dev with an "
            "allowlist) and AGENT_STACK_TOKEN.]"
        ))

    # 1. What the file is, before the bytes. The sandbox knows its own
    #    limit, and a file that is over it is refused with its real
    #    size -- so the model can be told the numbers instead of
    #    watching a transfer die halfway.
    try:
        meta = await _ft.file_meta(path)
    except _ft.FileTransportError as e:
        return ToolExecution(text=f"[send_file: {path}: {e.message}]")

    name = str(meta.get("name") or os.path.basename(path) or "file.bin")
    size = _as_int(meta.get("size"))
    limit = _as_int(meta.get("limit"))
    if limit and size > limit:
        return ToolExecution(text=(
            f"[send_file: {name} is {size} bytes; the sandbox allows "
            f"{limit} bytes]"))
    if size > MAX_SEND_FILE_BYTES:
        return ToolExecution(text=(
            f"[send_file: {name} is {size} bytes; Telegram's sendDocument "
            f"accepts at most {MAX_SEND_FILE_BYTES} bytes]"))

    # 2. The bytes: streamed into a temp file, hashed on the way. A file
    #    that is too big for the channel never gets this far.
    try:
        fetched = await _ft.fetch_to_temp(path)
    except _ft.FileTransportError as e:
        return ToolExecution(text=f"[send_file: {path}: {e.message}]")

    # 3. Stage it.  The document goes out WITH THE ANSWER, not in the
    #    middle of the turn: the operator's rule of 2026-10-28.  Before
    #    this, the tool sent the file the moment the model called it --
    #    and the model was still working at that point, its reasoning
    #    still streaming into the chat, so the file landed in the middle
    #    of a thought.  The bytes are pulled once, here, and hashed on
    #    the way; the upload reads the temp file later.
    from llm_staged import StagedFile, stage

    entry = _entry("send_file", args, deps.iteration,
                   size=fetched.size, sha256=fetched.sha256, staged=True)
    stage((deps.chat_id, deps.current_message_id),
          StagedFile(path=fetched.path, name=fetched.name,
                     size=fetched.size, sha256=fetched.sha256,
                     ledger=entry))

    # 4. The sandbox copy stays. The delivery removes nothing on its
    #    own -- the operator's ruling of 2026-10-27. If the file is
    #    supposed to go, that is a request the model carries out through
    #    the shell, where the intent is explicit.

    return ToolExecution(
        text=(f"[send_file: staged {name} ({fetched.size} bytes, "
              f"sha256 {fetched.sha256}) for delivery with the answer; "
              f"the sandbox copy is still at {path}]"),
        ledger=entry,
    )


async def _execute_mcp(name: str, args, deps: DispatchDeps) -> ToolExecution:
    """Call a tool the bot imported from an MCP server.

    The registry owns the route: it knows which server offered the
    tool, and it checks the gate before the call leaves the process.
    The bot has no branch per server -- that is the whole point of
    the registry. Adding a server is a config line, not a dispatch
    branch, and a tool the model names that no server offered never
    reaches the wire.
    """
    registry = mcp_registry.get_registry()
    text = await registry.call(name, args, chat_id=deps.chat_id)
    return _finish(text, _entry(name, args, deps.iteration), deps)


def _finish(result, ledger: dict, deps: DispatchDeps) -> ToolExecution:
    """Wrap an executor's return value into the outcome the loop reads.

    `str(result)` is applied here so every executor may return whatever
    it likes -- dict, list, None -- and the emptiness heuristic always
    sees the same thing the model will see.

    The ledger also carries what the MODEL saw: the tool's own result
    text, bounded. That is not bookkeeping -- it is the base the
    file-extractor compares a code block against to tell a deliverable
    from a quote of the console output. Measured on production,
    2026-10-30: the model quoted the middle of a command's output, the
    ledger kept a narrower piece of that output than the model had
    seen, the quote looked like a file, and the user got `answer.txt`
    with the execution artifacts in it -- a document nobody asked for,
    made of output the model had already quoted. The comparison has to
    run against what the model saw, or a quote from the middle of a
    long output is invisible to it.
    """
    text = str(result)
    ledger["output"] = _ledger_output(text)
    return ToolExecution(text=text, empty=result_is_empty(text), ledger=ledger)


#: How much of a tool's result the ledger keeps for the comparison.
#:
#: The same bound the model's own view of a command's output uses
#: (8000 characters, `personal_tools.bash_command`), so what the ledger
#: keeps is what the model could quote back. The piece that reaches the
#: database is bounded again by the transcript.
MAX_LEDGER_OUTPUT_CHARS = 8000


def _ledger_output(text: str) -> str:
    """The tool's result, bounded, with the middle marked.

    The marker is part of the budget: a cut that spends the whole limit
    on the text and then adds a marker on top is a cut that did not
    fit.
    """
    if not text:
        return ""
    return condense(text, MAX_LEDGER_OUTPUT_CHARS)
