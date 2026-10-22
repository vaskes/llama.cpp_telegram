from loop_owner import (call_source, loop_source,  # noqa: F401
                       loop_tree)
"""Every name call_llama reads must actually resolve.

TECH-DEBT #8. `Nudge` was used at call_llama.py:1036 and never imported,
between two commits of Block C3. py_compile cannot see it -- names are
resolved at runtime, not at parse time -- and 925 tests were green,
because every one of them either imports the module or never drives the
loop far enough to reach the line.

The failure was not cosmetic. The line is the branch after
`isinstance(outcome, Abort)`, so a Nudge -- which the guard returns
whenever the empty-call budget reaches 2, i.e. whenever a tool returns
"[unavailable]" twice -- raised NameError and killed the turn. It would
have fired in production within a day.

This is the generalisation of `tests/test_drift_t58_revert.py`, which
walks dispatch.py for the same property. call_llama is the module where
it matters most: it has 979 lines, roughly a hundred module-level
imports, and an extraction history that moved symbols between modules
for four blocks straight.

Deliberately permissive about what counts as a local. A false positive
teaches you to ignore the checker; a false negative ships. Anything
bound anywhere inside the function -- including comprehension targets
and nested-function parameters, which are not really function-locals --
is treated as available. That can only make the check quieter than
reality, never louder.
"""
import ast
import builtins
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT_DIR))

pytestmark = pytest.mark.tech_debt(
    item=8,
    reason="Call_llama reads a name it does not import: a NameError that "
           "py_compile cannot see and no test reached.")


def _module_bindings(tree):
    """Everything bound at module level, in any form."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            names.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _bound_inside(node):
    """Every name bound anywhere under `node`, however it is bound."""
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
    return names


def _referenced_names(src):
    """Names the code actually touches, docstrings excluded.

    `LoopGuard` appears in llm_iteration's prose -- it says what the
    guard is -- and a substring search reports that as a use. Only a
    real `Name`/`Attribute` reference is a use, so strip the
    docstrings and read the tree.
    """
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                              ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
    got = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name):
            got.add(node.id)
        elif isinstance(node, ast.Attribute):
            got.add(node.attr)
        elif isinstance(node, (ast.ImportFrom, ast.Import)):
            continue
    return got


def _imports_from(src, module):
    """Names bound by `from <module> import ...` in `src`."""
    got = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ImportFrom) and n.module == module:
            got.update(a.asname or a.name for a in n.names)
    return got


def test_call_llama_reads_only_names_that_resolve():
    path = BOT_DIR / "call_llama.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    available = _module_bindings(tree) | set(dir(builtins))

    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")

    local = _bound_inside(fn)

    unresolved = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in available and n.id not in local:
                unresolved.add((n.id, n.lineno))

    assert not unresolved, (
        "call_llama reads names it never binds:\n"
        + "\n".join(f"  line {ln}: {name}" for name, ln in sorted(unresolved))
        + "\n\npy_compile cannot see this and a green suite will not either: "
          "the NameError fires only when control reaches the line."
    )


def test_the_known_regression_is_pinned_by_name():
    """Nudge specifically.

    A general name-resolution check says "some name is unresolved"; this
    says the one that actually shipped, so a reader who hits it in
    `git log` can see why the check exists.
    """
    src = loop_source()
    assert "isinstance(outcome, Nudge)" in src, (
        "the Nudge branch is gone -- if it was renamed or inlined, confirm "
        "the empty-call budget still nudges before it aborts"
    )
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "llm_loop_guard":
            imported.update(a.asname or a.name for a in n.names)
    assert "Nudge" in imported, (
        "Nudge is used in call_llama and not imported: a nudge from the "
        "loop guard raises NameError and kills the turn"
    )


def test_every_name_in_the_guard_outcome_family_is_imported():
    """Abort, LoopGuard and Nudge travel together.

    They were imported together once, with Nudge missing, and no test
    noticed. Importing the module and checking all three is the form
    that cannot drift.
    """
    import llm_loop_guard

    exported = {n for n in dir(llm_loop_guard) if not n.startswith("_")}
    family = {"Abort", "Nudge", "LoopGuard"}

    # T8.1 split the guard family across two modules: call_llama builds
    # the LoopGuard and hands it in, the loop consumes the Abort and the
    # Nudge it gets back. Each module must declare what it uses, so the
    # check runs per module and names the missing side in the message.
    call_src = call_source()
    loop_src = loop_source()
    call_imports = _imports_from(call_src, "llm_loop_guard")
    loop_imports = _imports_from(loop_src, "llm_loop_guard")
    call_used = _referenced_names(call_src) & family
    loop_used = _referenced_names(loop_src) & family

    assert call_used or loop_used, \
        "neither call_llama nor the loop uses anything from llm_loop_guard"
    missing_call = call_used - call_imports
    missing_loop = loop_used - loop_imports
    assert not missing_call and not missing_loop, (
        f"used but not imported -- call_llama: {sorted(missing_call)}; "
        f"loop: {sorted(missing_loop)}; llm_loop_guard exports "
        f"{sorted(exported & family)}"
    )

# ---------------------------------------------------------------------------
# The same check, across every module this refactor created.
# ---------------------------------------------------------------------------

EXTRACTED_MODULES = (
    "llm_nonstream.py", "llm_sse.py", "llm_loop_guard.py",
    "llm_thinking.py", "llm_json.py", "llm_toolargs.py",
    "llm_tools.py", "llm_messages.py", "http_clients.py", "donsetch.py",
)


@pytest.mark.parametrize("module", EXTRACTED_MODULES)
def test_extracted_module_reads_only_names_that_resolve(module):
    """Two NameErrors shipped out of this refactor, both because a name
    was used after its binding moved to another module.

    One was found in call_llama and it is easy to assume call_llama was
    the only place -- the fix was to check one module. Every module this
    work created gets the same check, because the failure mode is not
    specific to the file it was found in.
    """
    path = BOT_DIR / module
    if not path.exists():
        pytest.skip(f"{module} does not exist")

    tree = ast.parse(path.read_text(encoding="utf-8"))
    available = _module_bindings(tree) | set(dir(builtins))

    local = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(n.name)

    unresolved = {}
    # Every function at ANY depth. The first version iterated
    # tree.body only, so in a module made of classes -- llm_sse.py is one
    # class and one dataclass -- it checked nothing at all and stayed
    # green with an orphaned name injected into a method body.
    # A tripwire that cannot be made to fail is worse than no tripwire.
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        bound = _bound_inside(fn)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id not in available and n.id not in bound and n.id not in local:
                    unresolved.setdefault(n.id, n.lineno)

    assert not unresolved, (
        f"{module} reads names it never binds:\n"
        + "\n".join(f"  line {ln}: {name}" for name, ln in sorted(unresolved.items()))
        + "\n\nThese are runtime NameErrors. py_compile resolves names when the "
          "line executes, not when the file parses."
    )
