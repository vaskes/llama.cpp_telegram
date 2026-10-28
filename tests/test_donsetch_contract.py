"""The bot's claims about donsetch must match the donsetch we pin.

The bot has a hand-written MCP client for donsetch's four web tools
(`telegram-bot/donsetch.py`), and it names those tools in a dozen
places. A renamed tool on the server side is a tool call that fails in
production and nowhere else: the client's error is a message, the
model retries, and the log says nothing about which name went stale.

So the claims live in one place --
`telegram-bot/personal_tools/donsetch_contract.py` -- and this file is
the check:

  * the version the contract names is the version the pinned submodule's
    own source declares, and the submodule is the one the tree pins;
  * the pinned version is not below the floor below which the shape the
    bot's client expects (a human-readable text result) is not there;
  * **every tool the bot's client calls is a tool the pinned server
    dispatches** -- read out of the submodule's own source, so a rename
    on the server side is a red test here, not a failed call in
    production;
  * the documents that talk about donsetch do not carry a second copy of
    the version number.

The submodule is a third-party repository (AGPL-3.0), consumed over
MCP. It is pinned the same way the sandbox is pinned, because the same
class of rot applies: a claim about a sibling service that nobody
recomputes.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"
DONSETCH = ROOT / "donsetch"

if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from personal_tools.donsetch_contract import (  # noqa: E402
    DONSETCH_LICENSE,
    DONSETCH_MIN_VERSION,
    DONSETCH_NATIVE_TOOLS,
    DONSETCH_PINNED_VERSION,
    DONSETCH_REPO,
    DONSETCH_SPLIT_SHAPE_ENV,
    DONSETCH_SPLIT_SHAPE_VALUE,
)

#: The operator-facing places that talk about donsetch. The CHANGELOG is
#: history and may quote old versions on purpose.
DOCS = (ROOT / "README.md", ROOT / "docs" / "SETUP.md",
        ROOT / "docs" / "ARCHITECTURE.md", ROOT / "docs" / "TOOL-ISOLATION.md")

#: The server's own dispatch: the tool names it answers to. Read from the
#: pinned source, so this is the server's truth and not ours.
DISPATCH = re.compile(r'"(?P<tool>web_[a-z_]+)"\s*=>\s*Ok\(', re.MULTILINE)

#: A version number in a document. Used to catch a second copy of the
#: pin -- the version lives in the contract, and the documents point at
#: it.
VERSION = re.compile(r"\b4\.\d+\.\d+\b")


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=True).stdout.strip()


def _declared_version() -> str:
    """The version the pinned submodule's own source declares."""
    cargo = DONSETCH / "Cargo.toml"
    assert cargo.is_file(), (
        "the donsetch submodule is not checked out -- run "
        "`git submodule update --init --recursive`. A tripwire that "
        "cannot see donsetch cannot check the claims about it.")
    m = re.search(r"^version\s*=\s*\"(?P<v>[^\"]+)\"", cargo.read_text(
        encoding="utf-8"), re.MULTILINE)
    assert m, "the submodule's Cargo.toml declares no version"
    return m.group("v")


def _server_tools() -> set:
    """The tool names the pinned server dispatches."""
    mod = DONSETCH / "src" / "mcp" / "server" / "mod.rs"
    assert mod.is_file(), (
        "the pinned donsetch has no src/mcp/server/mod.rs -- the server's "
        "tool dispatch moved, and the check has to follow it")
    return {m.group("tool") for m in DISPATCH.finditer(
        mod.read_text(encoding="utf-8"))}


def _version_tuple(value: str) -> tuple:
    parts = []
    for chunk in value.strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def test_the_tree_pins_a_donsetch_submodule():
    """The pin is a gitlink in the tree, not a clone somebody made."""
    entry = _git("ls-tree", "HEAD", "donsetch")
    assert entry, (
        "the tree has no donsetch submodule -- there is nothing to pin, "
        "and the version in the contract is a claim about nothing")
    parts = entry.split()
    kind = parts[1]
    assert kind == "commit", f"donsetch is not a gitlink: {entry!r}"
    assert parts[3] == "donsetch", f"the gitlink is not at donsetch/: {entry!r}"


def test_the_contract_names_the_version_the_pinned_donsetch_declares():
    """The number in the contract is the number in the submodule."""
    declared = _declared_version()
    assert declared == DONSETCH_PINNED_VERSION, (
        f"the contract says {DONSETCH_PINNED_VERSION}, the pinned donsetch "
        f"declares {declared}. Bump the submodule, the contract and the "
        "documentation together, or the number in one of them is a lie.")


def test_the_checked_out_donsetch_is_the_pinned_one():
    """The submodule on disk is the one the tree pins.

    A submodule left at another commit is the same failure as a stale
    number, only louder: the test suite would be measuring a tree that
    is not the tree that ships.
    """
    entry = _git("ls-tree", "HEAD", "donsetch")
    pinned = entry.split()[2]
    checked_out = _git("rev-parse", "HEAD", cwd=DONSETCH)
    assert checked_out == pinned, (
        f"donsetch is checked out at {checked_out}, the tree pins {pinned}. "
        "Run `git submodule update --init --recursive`.")


def test_the_pinned_version_is_above_the_floor():
    """The shape the bot's client expects exists from the floor on."""
    assert _version_tuple(DONSETCH_PINNED_VERSION) >= _version_tuple(
        DONSETCH_MIN_VERSION), (
        f"the pinned donsetch is {DONSETCH_PINNED_VERSION}, below the floor "
        f"{DONSETCH_MIN_VERSION} below which the split shape the bot's "
        "client expects is not there")


def test_every_tool_the_bot_calls_is_a_tool_the_server_dispatches():
    """The names in the bot's client are the names the server answers to.

    This is the check that would have caught a renamed tool: the bot
    calls `web_search`, the server dispatches something else, and the
    failure is a message in a chat, not a red test.
    """
    server = _server_tools()
    assert server, (
        "no tool names read out of the pinned donsetch's dispatch -- the "
        "server's source moved, and this check would pass by finding "
        "nothing")
    missing = sorted(set(DONSETCH_NATIVE_TOOLS) - server)
    assert not missing, (
        f"the bot's client calls {missing}, and the pinned donsetch does "
        f"not dispatch them. It dispatches {sorted(server)}.")


def test_the_split_shape_knob_is_the_one_the_donsetch_documents():
    """The knob that gives the bot its text result is donsetch's own.

    Read out of the pinned submodule's README, so the constant in the
    contract cannot drift away from the setting the server knows.
    """
    readme = DONSETCH / "README.md"
    assert readme.is_file(), (
        "the pinned donsetch has no README.md -- the check has to follow "
        "the source")
    assert DONSETCH_SPLIT_SHAPE_ENV in readme.read_text(encoding="utf-8"), (
        f"{DONSETCH_SPLIT_SHAPE_ENV} is not in the pinned donsetch's "
        "README: the knob the bot's client depends on is not the knob the "
        "server documents")
    assert DONSETCH_SPLIT_SHAPE_VALUE in readme.read_text(encoding="utf-8"), (
        f"the value {DONSETCH_SPLIT_SHAPE_VALUE!r} is not what the pinned "
        "donsetch documents for the split shape")


def test_the_documents_do_not_carry_a_second_version_number():
    """The version lives in the contract; the documents point at it.

    A second copy of a version number is a second thing to update, and
    the copy that rots first is the one nobody recomputes.
    """
    stale = []
    for doc in DOCS:
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8")
        for m in VERSION.finditer(text):
            line = text[:m.start()].count("\n") + 1
            context = text.splitlines()[line - 1]
            if "donsetch" not in context.lower() and "donsetch" not in (
                    text.splitlines()[line - 2].lower()
                    if line >= 2 else ""):
                continue
            stale.append(f"{doc.relative_to(ROOT)}:{line} {m.group(0)}")
    assert not stale, (
        "these documents name a donsetch version of their own; the pin "
        "lives in personal_tools/donsetch_contract.py:\n  "
        + "\n  ".join(stale))


def test_the_submodule_is_the_repository_the_contract_names():
    """The submodule's URL is the repository the contract names."""
    modules = (ROOT / ".gitmodules").read_text(encoding="utf-8")
    assert DONSETCH_REPO in modules, (
        f"the contract says the submodule is {DONSETCH_REPO}, and "
        f".gitmodules does not say that")
    assert "donsetch" in modules, "there is no donsetch entry in .gitmodules"


def _web_tool(name: str) -> dict:
    """The tool definition the MODEL is shown, from the tree."""
    import llm_tools
    for tool in llm_tools.DONSETCH_WEB_TOOL_DEFS:
        if tool.get("function", {}).get("name") == name:
            return tool["function"]
    raise AssertionError(f"the bot does not offer a tool named {name}")


def test_the_search_tool_tells_the_model_about_the_handle():
    """The search returns a handle; the description has to say so.

    Measured on the production host, 2026-10-28: the search's split-shape
    result carries a compact handle (`S7wfjEZlQ`), a title, a domain and a
    snippet -- and no URL. The tool description used to claim "Returns
    titles + snippets + URLs" and told the model to follow up with
    `web_fetch` "to read a specific URL". The model did what it was
    told: it guessed URLs, got a 404 for every guess, and the turn ended
    at the iteration limit having read nothing.

    The pinned server's own README says the same thing the tool must say
    ("compact, unguessable link and search handles"), so this check has
    an external anchor: the bot's description and the server's
    documentation must not disagree.
    """
    description = _web_tool("donsetch_web_search")["description"]
    assert "handle" in description.lower(), (
        "the search tool's description does not mention the handle the "
        "search result actually carries -- the model will guess URLs")
    readme = (DONSETCH / "README.md").read_text(encoding="utf-8")
    assert "handle" in readme.lower(), (
        "the pinned donsetch's README no longer mentions handles -- the "
        "server's documented interface moved and the bot's description "
        "has to be re-checked against it")


def test_the_fetch_tool_accepts_a_handle():
    """The fetch takes the handle; the parameter has to say so."""
    function = _web_tool("donsetch_web_fetch")
    description = function.get("description", "")
    url_param = function.get("parameters", {}).get("properties", {}).get(
        "url", {})
    assert "handle" in description.lower(), (
        "the fetch tool's description does not say that it takes the "
        "handle from the search")
    assert "handle" in str(url_param.get("description", "")).lower(), (
        "the url parameter's description does not say that a handle "
        "is accepted there")
