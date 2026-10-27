"""The seam between the MCP registry and the rest of the bot.

The registry's own tests cannot see whether the model is actually shown
the tools it found, or whether a call the model makes reaches the
server. Those are two different directions, and a suite that only
covers the registry would stay green while the bot showed the model
nothing and refused every call.

So this file drives the two places the registry touches the outside
world:

  * the tool list the model is shown (llm_tools.build_tool_defs), and
  * the dispatch of a call the model made (llm_dispatch.execute_tool).

The registry here is the REAL one -- only its HTTP client is faked --
because the point of these tests is the wiring: the names the registry
finds have to reach the model, the isolation layer and the dispatcher.

The engine's own tool list is in the picture too, because two facts
about it were measured on the live engine and both were traps: it
advertises tools the bot cannot execute, and it advertises the same
names the bot defines -- with a different schema.
"""
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT))

import llm_dispatch  # noqa: E402
import llm_tools  # noqa: E402
import mcp_registry  # noqa: E402
import tool_isolation  # noqa: E402
from llm_dispatch import DispatchDeps, execute_tool  # noqa: E402
from mcp_client import McpServerConfig  # noqa: E402
from mcp_registry import McpRegistry  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _personal_profile(monkeypatch):
    """The profile these tests run in.

    The default profile is `public`, and in the public profile the bot
    has no imported tools at all -- that is the gate working, not a
    bug in the registry. The gate has its own tests.
    """
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "personal")


def _def(name, description="d", parameters=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


class FakeClient:
    """The HTTP layer, faked. Everything above it is the real code."""

    def __init__(self, config):
        self.config = config
        self.calls = []

    async def initialize(self):
        return "sess"

    async def list_tools(self):
        return [{"name": "read_file", "description": "read a file"}]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return f"[MCP OUTPUT] {name}"


def _engine_tools():
    """What the live engine advertises, measured on 2026-10-27."""
    return [
        _def("exec_shell_command"),          # a built-in the bot cannot run
        _def("read_file"),                   # another one
        _def("donsetch_web_search"),         # the same name the bot defines
    ]


@pytest.fixture
def registry(monkeypatch):
    """A real registry over a faked server."""
    clients = {}

    def factory(config):
        client = FakeClient(config)
        clients[config.name] = client
        return client

    monkeypatch.setattr(mcp_registry, "McpClient", factory)
    real = McpRegistry([McpServerConfig(name="sandbox",
                                        url="http://sandbox.invalid/mcp")])
    monkeypatch.setattr(mcp_registry, "get_registry", lambda: real)
    return real, clients


@pytest.fixture
def engine(monkeypatch):
    async def fake_engine():
        return _engine_tools()

    monkeypatch.setattr(llm_tools, "fetch_tools_from_llama", fake_engine)
    monkeypatch.setattr(llm_tools, "_bash_command_tool_enabled", lambda: False)


async def test_the_model_is_shown_the_imported_tools(monkeypatch, registry, engine):
    defs = await llm_tools.build_tool_defs(
        bot=None, chat_id=42, chat_type="supergroup")

    names = [d["function"]["name"] for d in defs]
    assert "sandbox_read_file" in names
    # the native ones are still there
    assert "react_to_message" in names
    assert "search_messages" in names


async def test_the_tools_the_bot_cannot_execute_are_not_offered(monkeypatch, registry, engine):
    defs = await llm_tools.build_tool_defs(
        bot=None, chat_id=42, chat_type="supergroup")

    names = [d["function"]["name"] for d in defs]
    assert "exec_shell_command" not in names
    assert "read_file" not in names


async def test_a_name_the_bot_defines_appears_once(monkeypatch, registry, engine):
    """Two definitions of one name is how a model sends arguments that
    one of them does not have."""
    defs = await llm_tools.build_tool_defs(
        bot=None, chat_id=42, chat_type="supergroup")

    names = [d["function"]["name"] for d in defs]
    assert names.count("donsetch_web_search") == 1


async def test_the_isolation_layer_knows_the_imported_names(monkeypatch, registry, engine):
    await llm_tools.build_tool_defs(
        bot=None, chat_id=42, chat_type="supergroup")

    known = tool_isolation.known_tool_names()

    assert "sandbox_read_file" in known
    # and the native ones are still there
    assert "delete_messages" in known


async def test_a_call_the_model_made_reaches_the_server(monkeypatch, registry, engine):
    real, clients = registry
    await llm_tools.build_tool_defs(
        bot=None, chat_id=42, chat_type="supergroup")
    deps = DispatchDeps(
        bot=None, chat_id=42, current_message_id=7, chat_type="supergroup",
        iteration=0, custom_tools={}, donsetch_tools={})

    got = await execute_tool("sandbox_read_file", {"path": "/tmp/x"}, deps)

    assert clients["sandbox"].calls == [("read_file", {"path": "/tmp/x"})]
    assert "[MCP OUTPUT]" in got.text
    assert got.ledger["name"] == "sandbox_read_file"


async def test_a_name_no_server_offered_is_not_dispatched(monkeypatch, registry, engine):
    real, clients = registry
    await llm_tools.build_tool_defs(
        bot=None, chat_id=42, chat_type="supergroup")
    deps = DispatchDeps(
        bot=None, chat_id=42, current_message_id=7, chat_type="supergroup",
        iteration=0, custom_tools={}, donsetch_tools={})

    got = await execute_tool("sandbox_made_up", {}, deps)

    assert clients["sandbox"].calls == []
    assert got.unavailable is True
