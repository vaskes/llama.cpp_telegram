"""Nothing a `finally` reads may be unbound when that finally is entered.

Review wave 3 §5. `tmp_path = None` was hoisted above handle_document's
try so that the finally's `if tmp_path and os.path.exists(tmp_path)`
cannot raise UnboundLocalError. Removing the hoist leaves 36 tests green
-- the guard is working and nothing tests it.

That hoist is gone as of 2026-11-01, together with the temporary file
it guarded: a document is uploaded to the sandbox as the bytes it is
now, so there is no temporary copy to unlink and nothing to hoist. The
invariant below did not move with it -- the AST walk still covers every
name every finally reads -- and the shape of the old case is pinned by
`test_handle_document_has_no_temporary_file`, so a change that brings
the temporary file back has to bring the hoist with it.

The failure it prevents is worse than a raised error. UnboundLocalError
raised *inside* a finally skips the statements after it in that same
finally, which is where the per-user slot is released; it also flies
past `except Exception`, so the real error is replaced by noise, the
user sees nothing, and the log gets a task failure.

The names come from the AST -- every Name in Load inside each finally's
finalbody -- so the next hoist someone adds is covered without anyone
remembering this file.

Two earlier versions of this test were wrong and are worth recording,
because both looked like the property and were not:

  * "not assigned inside the try" flags `thinking` in all three
    handlers. Wrong: `thinking = await _reply(...)` inside the try is
    normal, and the `if thinking is not None` guard in the finally
    depends on that reassignment being visible.

  * counting module globals as unbound locals flags `_abort_events`, an
    import. A test that is red on the correct tree teaches its reader to
    ignore it.
"""
import ast
import builtins
import sys
from pathlib import Path

import pytest

# TECH-DEBT #13 -- the wrapper duplication in item 13 is what this
# pins while it stays open.
pytestmark = pytest.mark.tech_debt(item=13)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

SRC = ROOT / "telegram-bot" / "handlers.py"
TEXT = SRC.read_text(encoding="utf-8")
TREE = ast.parse(TEXT)

HANDLERS = ["handle_photo", "handle_voice", "handle_document", "handle_text"]


def _module_names(tree):
    """Everything bound at module level: imports, assignments, defs."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


GLOBALS = _module_names(TREE) | set(dir(builtins))


def _fn(name):
    for n in TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    pytest.skip(f"{name} is not a top-level function")


def _tries(fn):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]


def _read_names(t):
    out = set()
    for stmt in t.finalbody:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                out.add(sub.id)
    return out


def _bound_before(fn, line, name):
    for node in ast.walk(fn):
        hit = (
            (isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == name for t in node.targets))
            or (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name) and node.target.id == name)
            or (isinstance(node, (ast.For, ast.AsyncFor))
                and isinstance(node.target, ast.Name) and node.target.id == name)
            or (isinstance(node, ast.withitem)
                and isinstance(node.optional_vars, ast.Name)
                and node.optional_vars.id == name)
        )
        if hit and node.lineno < line:
            return True
    return False


@pytest.mark.parametrize("handler", HANDLERS)
def test_finally_reads_only_names_bound_before_its_try(handler):
    """The invariant, on all four handlers."""
    fn = _fn(handler)
    params = {a.arg for a in fn.args.args}
    problems = []
    for t in _tries(fn):
        for name in sorted(_read_names(t)):
            if name in params or name in GLOBALS:
                continue
            if not _bound_before(fn, t.lineno, name):
                problems.append(
                    f"{handler}: the finally at line {t.lineno} reads "
                    f"{name!r}, which is not bound before that try")
    assert not problems, (
        "\n".join(problems) + "\n\nIf the body returns or raises before that "
        "assignment, the finally raises UnboundLocalError, skips the rest of "
        "its own body -- including sem.release() -- and escapes past except "
        "Exception, so the real error is replaced by noise."
    )


@pytest.mark.parametrize("handler", HANDLERS)
def test_release_is_not_behind_anything_that_can_raise(handler):
    """Nothing ahead of sem.release() may raise.

    Inside a finally the statements after a raise never run, so anything
    placed between the start of the finally and the release is a way to
    lose the per-user slot while reading as harmless cleanup.
    """
    fn = _fn(handler)
    problems = []
    for t in _tries(fn):
        releases = [i for i, s in enumerate(t.finalbody)
                    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                    and getattr(s.value.func, "attr", "") == "release"]
        if not releases:
            continue
        # Everything ahead of the release must be incapable of raising.
        # A bare `try` node is the simple case; `if X: try: ... except: pass`
        # is equally safe, which is how handle_photo and handle_voice are
        # written -- a first version that accepted only a Try node flagged
        # both of them, and a test red on the correct tree is worse than
        # no test.
        def _safe(stmt):
            if isinstance(stmt, ast.Try):
                return True
            if isinstance(stmt, ast.If):
                return all(_safe(b) for b in stmt.body) and \
                    all(_safe(b) for b in stmt.orelse)
            return False

        for stmt in t.finalbody[:releases[0]]:
            if not _safe(stmt):
                problems.append(
                    f"{handler}: the finally at line {t.lineno} runs "
                    f"{type(stmt).__name__} before sem.release(), and it is "
                    f"not wrapped in try/except")
    assert not problems, "\n".join(problems)


def test_handle_document_has_no_temporary_file():
    """The temporary file is gone, and with it the hoist that guarded it.

    This test used to pin `tmp_path = None` in handle_document: the
    handler wrote the document to a temporary file so that it could read
    the first 16 000 characters back out of it, and the finally unlinked
    it. The document is not read that way any more -- it is uploaded to
    the sandbox as the bytes it is, and the model is given a pointer to
    that file -- so the temporary file, the hoist and the finally's
    unlink are gone together.

    The property the old test guarded -- nothing a finally reads may be
    unbound when that finally is entered -- is still guarded, by the
    AST walk in `test_finally_reads_only_names_bound_before_its_try`,
    for every name in every finally of all four handlers. What this
    test adds is the shape: there is no temporary copy of a document
    any more, and a change that brings one back has to bring the hoist
    with it.
    """
    fn = _fn("handle_document")
    tmp_names = {n.id for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and n.id == "tmp_path"}
    assert not tmp_names, (
        "handle_document carries a tmp_path again -- the document is not "
        "read from a temporary file any more, and a temporary file "
        "without its hoist would make the finally raise UnboundLocalError "
        "and skip sem.release()")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "NamedTemporaryFile"]
    assert not calls, (
        "handle_document writes a temporary file again -- the document "
        "goes to the sandbox as the bytes it is")