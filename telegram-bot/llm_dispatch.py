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
from typing import Any, NamedTuple, Optional

from rating import _execute_react_to_message

UNAVAILABLE_TOOL_MESSAGE = (
    '[tool {name} unavailable in this mode. Bot supports: '
    'donsetch_web_search, donsetch_web_fetch, donsetch_web_crawl, '
    'donsetch_web_screenshot, react_to_message, bash_command '
    '(personal mode only).]'
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

    registry = deps.custom_tools or {}
    if name in registry:
        result = await registry[name](args)
        return _finish(result, _entry(name, args, deps.iteration), deps)

    registry = deps.donsetch_tools or {}
    if name in registry:
        result = await registry[name](args)
        return _finish(result, _entry(name, args, deps.iteration), deps)

    text = UNAVAILABLE_TOOL_MESSAGE.format(name=name)
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
               ok=response.get("ok", False)),
        deps,
    )


def _finish(result, ledger: dict, deps: DispatchDeps) -> ToolExecution:
    """Wrap an executor's return value into the outcome the loop reads.

    `str(result)` is applied here so every executor may return whatever
    it likes -- dict, list, None -- and the emptiness heuristic always
    sees the same thing the model will see.
    """
    text = str(result)
    return ToolExecution(text=text, empty=result_is_empty(text), ledger=ledger)