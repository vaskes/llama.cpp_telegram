"""No message that reaches the user may carry a Python exception.

The rule: the chat gets a sentence, the log gets the detail. On
2026-10-25 the bot answered a dead engine with

    [both streaming and non-streaming failed: stream_err=ConnectError(
    'All connection attempts failed'), fallback_err=ConnectError(...)]

and five more paths did the same thing in their own words:

    ❌ Error: ConnectError('All connection attempts failed')
    ❌ Failed to download photo: ...
    ❌ createForumTopic failed: ...

All of them logged the exception properly -- `log.exception`, with the
traceback -- and then ALSO pushed the raw text into the chat. The log
was never the problem. The chat was.

This is an AST tripwire, not a grep. A grep for "{e}" would miss
`f"...{exc}"`, `f"...{err}"` and `f"...{type(e).__name__}"`, and it
would also match the log lines, which are exactly where the detail
belongs. What it looks for instead is the shape of the defect:

  * a name bound by `except ... as NAME`, or a name derived from one
    inside that handler;
  * interpolated into an f-string;
  * passed to a call whose text reaches the user.

Three together are the defect. Any one alone is fine -- the log
lines interpolate the exception on purpose, and a handler may name the
exception without sending it.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"

#: Calls whose arguments end up in the user's chat.
#
# `send_files` is not here: it ships a document, not a string, and a
# file's name is not an exception. `push_thinking` is not here either:
# it streams a progress note, and it already carries its own wording.
USER_SENDING = {
    "_reply",
    "_reply_active",
    "_send_answer_text",
    "_send_as_text",
    "reply_text",
    "send_message",
    "edit_message_text",
    "edit_text",
    "answer",
}


def _interpolated_names(node: ast.AST) -> set:
    """The names an f-string interpolates."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.FormattedValue):
            for inner in ast.walk(sub.value):
                if isinstance(inner, ast.Name):
                    names.add(inner.id)
    return names


class _Finder(ast.NodeVisitor):
    """Walks one module, remembering the exception names in scope."""

    def __init__(self, path: Path):
        self.path = path
        self.offenders = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        names = set()
        if node.name:
            names.add(node.name)
            # A name derived from the exception inside the handler is
            # the same thing one step removed: `msg = str(e)`.
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    if _interpolated_names(sub.value) & names or (
                            isinstance(sub.value, ast.Name)
                            and sub.value.id in names):
                        for target in sub.targets:
                            if isinstance(target, ast.Name):
                                names.add(target.id)
        self._scope.append(names)
        self.generic_visit(node)
        self._scope.pop()

    _scope = None

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name in USER_SENDING:
            tainted = set()
            for level in self._scope:
                tainted |= level
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                hit = _interpolated_names(arg) & tainted
                if hit:
                    self.offenders.append((node.lineno, name, sorted(hit)))
        self.generic_visit(node)

    def visit_Module(self, node: ast.Module) -> None:
        self._scope = []
        self.generic_visit(node)


def _sources():
    for path in sorted(BOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_no_user_facing_message_carries_an_exception():
    offenders = []
    for path in _sources():
        finder = _Finder(path)
        finder.visit(ast.parse(path.read_text(encoding="utf-8"), str(path)))
        for lineno, func, hit in finder.offenders:
            offenders.append(
                f"{path.relative_to(ROOT)}:{lineno}: {func}() carries {hit}")

    assert not offenders, (
        "an exception reached the user's chat here:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe detail belongs in the log (log.exception), and the chat "
          "belongs to user_notice."
    )


def test_the_tripwire_would_notice_a_smuggled_exception():
    """The tripwire must be able to fail.

    A check that cannot fail is a comment. This runs the same finder
    over a piece of code that does exactly what the fix removed, and
    expects it to be caught.
    """
    source = (
        "async def h(update, e):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        await _reply(update, f'❌ Error: {e}')\n"
    )
    finder = _Finder(Path("smuggled.py"))
    finder.visit(ast.parse(source, "smuggled.py"))
    assert finder.offenders, "the tripwire did not notice a smuggled exception"
    assert finder.offenders[0][2] == ["e"]


def test_the_tripwire_leaves_the_log_lines_alone():
    """The detail belongs in the log. That must stay possible.

    `log.exception(f"... {e}")` is the correct place for the exception's
    own text, and this tripwire must not forbid it.
    """
    source = (
        "async def h(update, e):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        log.exception(f'[ERR text] {type(e).__name__}: {e}')\n"
    )
    finder = _Finder(Path("logged.py"))
    finder.visit(ast.parse(source, "logged.py"))
    assert not finder.offenders


def test_the_tripwire_leaves_a_derived_name_alone_when_it_is_not_sent():
    """A derived name is only a problem when it reaches the user."""
    source = (
        "async def h(update, e):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        msg = str(e)\n"
        "        log.info(msg)\n"
    )
    finder = _Finder(Path("derived.py"))
    finder.visit(ast.parse(source, "derived.py"))
    assert not finder.offenders


@pytest.mark.parametrize("name", sorted(USER_SENDING))
def test_every_user_sending_name_is_in_the_tripwire(name):
    """The set of senders is a fact about the tree, not a guess.

    If a new way of sending text to the user appears, it has to be in
    the list -- otherwise the tripwire has a hole the size of that new
    way. This test fails when the list holds a name the tree no longer
    uses, which is the other half: a stale name would let a leak
    through a rename.
    """
    used = set()
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = (getattr(func, "attr", None)
                          or getattr(func, "id", None))
                if called:
                    used.add(called)

    assert name in used, (
        f"{name} is in the tripwire's sender list but nothing calls it "
        f"any more -- remove it or the tripwire is checking a ghost")
