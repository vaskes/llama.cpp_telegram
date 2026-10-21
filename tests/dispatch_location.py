"""Where the tool dispatch lives, for tests that care about the property.

Six tripwires broke in Block D4, all for the same reason: they read
call_llama.py to find a dispatch site that is now in llm_dispatch.py.
Nothing they protect had changed.

That is a tripwire pinned to a location rather than to a behaviour. It
fails on every extraction, and the usual response to a test that fails on
every refactor is to delete it -- which takes the real check with it.
So the location moves, the assertion does not.

Deliberately a list rather than a glob. When the next block moves the
dispatch again, adding a module here is a visible edit to one obvious
place, and the alternative -- a glob that silently starts matching more
files -- hides it.

`DISPATCH_SOURCE` is the concatenation, for the substring assertions
that several of these still make. `source_of` returns a file's own text
for the AST assertions, where concatenation would not parse.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
BOT = REPO / "telegram-bot"

#: Modules that may contain tool dispatch logic, newest first.
DISPATCH_MODULES = ("llm_dispatch.py", "call_llama.py")


def source_of(module: str) -> str:
    """One module's text, or an empty string if it does not exist."""
    path = BOT / module
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def dispatch_source() -> str:
    """Every dispatch module, concatenated. For substring assertions."""
    return "\n".join(source_of(m) for m in DISPATCH_MODULES)


def iter_modules():
    """(name, source) for the AST walkers."""
    for name in DISPATCH_MODULES:
        yield name, source_of(name)