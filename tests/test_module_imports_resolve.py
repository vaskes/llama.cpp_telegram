"""Every name a module reads must be bound by that module or reachable.

TECH-DEBT #10. Moving `start` out of `dispatch.py` produced a module
that called `asyncio.to_thread` six times and never imported `asyncio`.
It compiled. `py_compile` passed. The full suite failed three tests,
none of which said "NameError"; they said the reply was never sent and
a handler was not registered.

The failure mode is worth being precise about. Nothing here is a
typo -- the spans were lifted verbatim, byte for byte, so the bodies
were exactly as they had been. What did not travel with them was the
module-level context they had been reading out of `dispatch` for
years: `asyncio`, `json`, `Update`, `_reply`, `get_budget`, and the
rest. A moved function carries its statements and none of its imports.

That is the whole class. A refactor that extracts code is only correct
if it also carries the bindings those statements assumed, and the
compiler will not tell you, because every one of the resulting modules
is individually valid.

This is deliberately a name-resolution walk rather than a pyflakes run:
no linter is installed in the test image, and more to the point this
needs to see the production modules only -- a test asserting its own
imports are fine is worth nothing.
"""
import ast
import builtins
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=10)

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"

# Modules that are not importable in isolation: the facade `bot.py`
# wires everything, and the selftest blocks execute their imports at
# call time by design.
SKIP = {"bot.py", "selftest.py"}
SKIP_PREFIX = "selftest_"


def _bound_names(tree: ast.Module) -> set:
    """Every name this module binds, at any scope.

    Deliberately not scope-aware. A scope-aware checker needs to model
    comprehensions, except-handlers and with-items correctly, and two
    earlier attempts at one were wrong in both directions -- reporting
    leaks that do not exist and missing the ones that do. Over-approximating
    the binding set is the safe direction: it can only make this test
    quieter, never make it lie about a module that is fine.
    """
    out: set = set()

    def names_in(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            out.add(n.name)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs:
                out.add(arg.arg)
            if a.vararg:
                out.add(a.vararg.arg)
            if a.kwarg:
                out.add(a.kwarg.arg)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                out |= names_in(t)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            out |= names_in(n.target)
        elif isinstance(n, ast.comprehension):
            # generator expressions and comprehensions bind their own
            # loop variable; without this every `for x in y` inside a
            # comprehension reads as a NameError
            out |= names_in(n.target)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                if alias.asname:
                    out.add(alias.asname)
                else:
                    # `import urllib.request` binds `urllib`, not
                    # `urllib.request`. Getting this backwards produced a
                    # false positive on dispatch.py the first time.
                    out.add(alias.name.split(".")[0])
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            out |= names_in(n.optional_vars)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Lambda):
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs:
                out.add(arg.arg)
        elif isinstance(n, ast.Global) or isinstance(n, ast.Nonlocal):
            out |= {x for x in n.names}
    return out


def _modules():
    for p in sorted(BOT.rglob("*.py")):
        if p.name.startswith("test_") or p.name in SKIP:
            continue
        if any(part.startswith(SKIP_PREFIX) for part in p.parts):
            continue
        if "__pycache__" in p.parts:
            continue
        yield p


def test_no_module_reads_a_name_it_does_not_bind():
    offenders = {}
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound = _bound_names(tree)
        known = set(dir(builtins)) | {"__name__", "__file__", "__doc__",
                                      "__package__", "__spec__", "__all__"}
        read = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(read - bound - known)
        if missing:
            offenders[str(path.relative_to(ROOT))] = missing
    assert not offenders, (
        "modules that read names they do not bind -- every one of these "
        "is a NameError waiting for the code path that reaches it:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in offenders.items()))


def test_the_commands_package_is_importable_as_a_whole():
    """It exists, and it exports what dispatch re-exports."""
    sys_path = BOT.as_posix()
    if sys_path not in __import__("sys").path:
        __import__("sys").path.insert(0, sys_path)
    import commands

    for name in commands.__all__:
        assert hasattr(commands, name), (
            f"commands.__all__ promises {name} and does not deliver it")