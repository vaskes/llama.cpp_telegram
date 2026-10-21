"""Block D4 -- llm_dispatch: running one tool call.

Before this, the only way to test any branch of the dispatch was to make
a model emit that exact call on demand. The bash branch alone had a late
import, a profile gate, an empty-command case and a ledger entry with an
extra field, and none of it was reachable from a test.

The tests below drive the module directly with a fake registry and a
fake personal_tools, which is the point of the extraction: everything
that was untestable is now a function call.

`tech_debt(item=8)` is declared by the module-level pytestmark below.
"""

import ast
from pathlib import Path

import pytest

import llm_dispatch
from llm_dispatch import (
    MAX_TOOL_RESULT_CHARS,
    SESSION_ID_TEMPLATE,
    UNAVAILABLE_TOOL_MESSAGE,
    DispatchDeps,
    ToolExecution,
    execute_tool,
    result_is_empty,
)

pytestmark = pytest.mark.tech_debt(item=8)

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"


def deps(**over):
    base = dict(bot=object(), chat_id=4242, current_message_id=99,
                chat_type="private", iteration=3,
                custom_tools={}, donsetch_tools={})
    base.update(over)
    return DispatchDeps(**base)


class FakeBash:
    """Stands in for personal_tools.bash_command."""

    def __init__(self, enabled=True, result=None):
        self.enabled = enabled
        self.result = result if result is not None else {"ok": True, "text": "hi"}
        self.calls = []

    def is_enabled_for_active_profile(self):
        return self.enabled

    async def call_bash_command(self, command, *, session_id, chat_type, chat_id):
        self.calls.append({"command": command, "session_id": session_id,
                           "chat_type": chat_type, "chat_id": chat_id})
        return self.result

    @staticmethod
    def tool_result_to_text(result):
        return result.get("text", "")


@pytest.fixture
def fake_bash(monkeypatch):
    """Install a fake personal_tools module for the duration."""
    import types

    holder = FakeBash()
    module = types.ModuleType("personal_tools")
    module.bash_command = holder
    monkeypatch.setitem(__import__("sys").modules, "personal_tools", module)
    holder.module = module
    return holder


# --------------------------------------------------------------------------
# result_is_empty -- the heuristic
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Found 0 results for Москва",
    "0 RESULTS",
    "engines unavailable",
    "ENGINES UNAVAILABLE",
    "[donsetch error] something went wrong",
    "[fetch error] timeout",
    "[tool rm_rf unavailable in this mode. Bot supports: ...]",
])
def test_these_are_not_progress(text):
    assert result_is_empty(text) is True


@pytest.mark.parametrize("text", [
    "Here are 5 results about Moscow",
    "[bash_command unavailable: requires personal mode]",
    "[react_to_message unavailable in this chat]",
    "",
])
def test_these_count_as_progress(text):
    assert result_is_empty(text) is False


def test_the_heuristic_is_case_insensitive_where_it_says_so():
    """lower() for the substring rules, exact prefix for the others --
    '[Donsetch' is not a prefix the executors ever emit, and matching it
    would be a guess."""
    assert result_is_empty("0 Results") is True
    assert result_is_empty("[Donsetch error] x") is False


def test_the_matched_prefixes_are_case_sensitive_by_design():
    """Pinned so a future 'make it case insensitive' change is a
    deliberate one rather than an accident."""
    assert result_is_empty("[donsetch error] x") is True
    assert result_is_empty("[DONSETCH error] x") is False


# --------------------------------------------------------------------------
# the unknown-tool branch
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unknown_name_is_refused_and_flagged():
    got = await execute_tool("rm_rf", {"path": "/"}, deps())
    assert got.unavailable is True
    assert got.empty is True, "a refused tool is not progress"
    assert got.ledger is None, "a tool that never ran leaves no ledger entry"
    assert "rm_rf" in got.text


@pytest.mark.asyncio
async def test_the_refusal_lists_what_is_available():
    got = await execute_tool("rm_rf", {}, deps())
    for tool in ("donsetch_web_search", "react_to_message", "bash_command"):
        assert tool in got.text


@pytest.mark.asyncio
async def test_the_refusal_is_not_truncated_by_the_module():
    """The 12000-char cap belongs to the caller, which owns the message
    it appends. Applying it here would make the constant untestable."""
    got = await execute_tool("rm_rf", {}, deps())
    assert len(got.text) < MAX_TOOL_RESULT_CHARS
    assert got.text == UNAVAILABLE_TOOL_MESSAGE.format(name="rm_rf")


# --------------------------------------------------------------------------
# the registry branches
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_custom_tool_runs_and_is_recorded():
    async def handler(args):
        return "custom ran"

    got = await execute_tool("my_tool", {"a": 1},
                             deps(custom_tools={"my_tool": handler}))
    assert got.text == "custom ran"
    assert got.ledger == {"name": "my_tool", "args": {"a": 1}, "iter": 3}
    assert got.unavailable is False


@pytest.mark.asyncio
async def test_a_donsetch_tool_runs_and_is_recorded():
    async def handler(args):
        return "[donsetch_web_search] 3 results"

    got = await execute_tool("donsetch_web_search", {"q": "x"},
                             deps(donsetch_tools={"donsetch_web_search": handler}))
    assert got.text == "[donsetch_web_search] 3 results"
    assert got.empty is True, "the [donsetch prefix marks it as no progress"


@pytest.mark.asyncio
async def test_custom_tools_win_over_donsetch_on_a_name_collision():
    """Both are plain dicts and nothing stops the same name being in
    both. Pinned so the precedence is a decision and not an accident of
    the elif order."""
    async def custom(args):
        return "from custom"

    async def donsetch(args):
        return "from donsetch"

    got = await execute_tool("clash", {},
                             deps(custom_tools={"clash": custom},
                                  donsetch_tools={"clash": donsetch}))
    assert got.text == "from custom"


# --------------------------------------------------------------------------
# bash_command
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bash_is_gated_by_the_active_profile(fake_bash):
    fake_bash.enabled = False
    got = await execute_tool("bash_command", {"command": "ls"}, deps())
    assert "personal mode" in got.text
    assert fake_bash.calls == [], "the gate must run before the command"
    assert got.ledger is None


@pytest.mark.asyncio
async def test_bash_runs_the_command_when_enabled(fake_bash):
    got = await execute_tool("bash_command", {"command": "ls -la"}, deps())
    assert fake_bash.calls[0]["command"] == "ls -la"
    assert got.text == "hi"
    assert got.ledger["name"] == "bash_command"
    assert got.ledger["ok"] is True


@pytest.mark.asyncio
async def test_bash_records_the_failure_flag(fake_bash):
    fake_bash.result = {"ok": False, "text": "command not found"}
    got = await execute_tool("bash_command", {"command": "nope"}, deps())
    assert got.ledger["ok"] is False, "a failed command must be visible as failed"


@pytest.mark.asyncio
async def test_bash_rejects_an_empty_command_without_calling(fake_bash):
    for args in ({}, {"command": ""}, {"command": None}):
        got = await execute_tool("bash_command", args, deps())
        assert got.text == "[bash_command: empty command]"
        assert got.ledger is None, "nothing ran, so nothing is recorded"
    assert fake_bash.calls == []


@pytest.mark.asyncio
async def test_bash_uses_the_deterministic_session_id(fake_bash):
    """T51 C5 / T57 #S16. The sandbox binds cwd, env and history to the
    session, so a per-call id would silently break the persistent shell,
    and a model-supplied one would let an injection cross chats."""
    await execute_tool("bash_command", {"command": "pwd"}, deps())
    assert fake_bash.calls[0]["session_id"] == "chat-4242"
    assert SESSION_ID_TEMPLATE == "chat-{chat_id}"


@pytest.mark.asyncio
async def test_bash_never_reads_the_session_id_from_arguments(fake_bash):
    """The wave-5 regression, restated as a behavioural test rather than
    an AST one, because now the code is callable."""
    await execute_tool("bash_command",
                       {"command": "pwd", "session_id": "chat-666"}, deps())
    assert fake_bash.calls[0]["session_id"] == "chat-4242", \
        "the model's own session_id won -- a cross-chat injection"


@pytest.mark.asyncio
async def test_bash_survives_a_missing_personal_tools(monkeypatch):
    """Very old deploys have no personal_tools package. The gate must
    degrade to a message, not take the turn down."""
    import sys
    monkeypatch.setitem(sys.modules, "personal_tools", None)
    got = await execute_tool("bash_command", {"command": "ls"}, deps())
    assert "unavailable" in got.text


@pytest.mark.asyncio
async def test_bash_is_the_only_tool_with_ok_in_its_ledger(fake_bash):
    async def handler(args):
        return "x"

    plain = await execute_tool("my_tool", {}, deps(custom_tools={"my_tool": handler}))
    bash = await execute_tool("bash_command", {"command": "ls"}, deps())
    assert "ok" not in plain.ledger
    assert "ok" in bash.ledger


# --------------------------------------------------------------------------
# the outcome shape
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_non_string_result_is_stringified():
    async def handler(args):
        return {"unexpected": "dict"}

    got = await execute_tool("my_tool", {}, deps(custom_tools={"my_tool": handler}))
    assert isinstance(got.text, str)
    assert "unexpected" in got.text


@pytest.mark.asyncio
async def test_a_none_result_is_stringified_to_none():
    async def handler(args):
        return None

    got = await execute_tool("my_tool", {}, deps(custom_tools={"my_tool": handler}))
    assert got.text == "None"
    assert got.empty is False


@pytest.mark.asyncio
async def test_a_long_result_is_not_truncated_by_the_module():
    """The 12000-char cap is the caller's, because only the caller builds
    the protocol message.

    The cap moving into this module survived the first mutation round
    for a reason worth recording: the test that claims to cover it fed a
    nine-character refusal, which is under any cap. A test about
    truncation has to exceed the limit, or it is testing nothing.
    """
    long_text = "z" * (MAX_TOOL_RESULT_CHARS * 3)

    async def handler(args):
        return long_text

    got = await execute_tool("my_tool", {}, deps(custom_tools={"my_tool": handler}))
    assert len(got.text) == len(long_text), (
        "the module truncated; the cap is the caller's to apply, and the "
        "caller's test asserts it")
    assert got.text[:MAX_TOOL_RESULT_CHARS] == long_text[:MAX_TOOL_RESULT_CHARS]


def test_the_outcome_defaults_are_inert():
    got = ToolExecution(text="x")
    assert (got.empty, got.unavailable, got.ledger) == (False, False, None)


@pytest.mark.asyncio
async def test_deps_is_positional_and_readable():
    """A NamedTuple, not eight keyword arguments -- the call site is one
    line and stays that way."""
    d = deps(iteration=9)
    assert d.iteration == 9
    assert tuple(d)[:3] == (d.bot, d.chat_id, d.current_message_id)


# --------------------------------------------------------------------------
# the call site
# --------------------------------------------------------------------------

def _call_llama():
    tree = ast.parse((BOT / "call_llama.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")


def test_call_llama_calls_execute_tool_and_nothing_else():
    fn = _call_llama()
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "execute_tool" in called


def test_no_handler_body_is_left_inline():
    """If one of these comes back, the module is not the single owner and
    the tests above stop describing production."""
    src = (BOT / "call_llama.py").read_text(encoding="utf-8")
    for gone in ("_execute_react_to_message(", "is_enabled_for_active_profile()",
                 "call_bash_command(", "CUSTOM_TOOLS[fn_name]", "DONSETCH_TOOLS[fn_name]"):
        assert gone not in src, f"still inline: {gone}"


def test_the_result_cap_is_applied_by_the_caller():
    """The cap belongs where the message is built, because only there does
    the protocol shape exist."""
    src = (BOT / "call_llama.py").read_text(encoding="utf-8")
    assert "outcome.text[:MAX_TOOL_RESULT_CHARS]" in src


def test_the_ledger_is_appended_only_when_present():
    src = (BOT / "call_llama.py").read_text(encoding="utf-8")
    assert "if outcome.ledger is not None:" in src


def test_screening_stays_out_of_the_dispatch_module():
    """parse_tool_args is a separate decision with its own module. If it
    creeps in here, ArgsRejected has to be handled twice."""
    # Only the screening entry point is forbidden by name. An earlier
    # version also banned `isinstance`, which this module uses
    # legitimately for the args-dict invariant.
    #
    # AST, not a substring. This test's first version asserted the name
    # is absent from the file and failed on the module's own docstring,
    # which mentions it to explain that it does not belong here. That is
    # the third time in this refactor that a substring check was wrong.
    tree = ast.parse((BOT / "llm_dispatch.py").read_text(encoding="utf-8"))

    imported = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}

    assert "llm_toolargs" not in imported

    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "parse_tool_args" not in called
    assert not any(isinstance(n, ast.Name) and n.id == "ArgsRejected"
                   for n in ast.walk(tree))