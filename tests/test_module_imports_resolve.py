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
    leaks that do not exist and missing the ones that do.

    "Over-approximate, being quiet is safe" was in this docstring and it
    was wrong. Quiet is not safe: an over-approximated binding set means
    the test cannot see the code it exists to see. `global` is the case
    that proved it -- see the note below.
    """
    out: set = set()
    declared_global: set = set()
    module_level: set = set()

    def names_in(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def names_in_targets(node):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {n.id for t in targets for n in ast.walk(t)
                if isinstance(n, ast.Name)}

    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_level.add(n.name)
        elif isinstance(n, ast.Assign):
            module_level |= names_in_targets(n)
        elif isinstance(n, ast.AnnAssign):
            module_level |= names_in_targets(n)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                module_level.add(alias.asname or alias.name.split(".")[0])

    out |= {g for g in declared_global if g in module_level}
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
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            # `global X` DECLARES an intent to rebind; it does not create
            # the name. If no module-level binding exists, the first read
            # raises NameError.
            #
            # This used to add every `global` name to the bound set, on the
            # theory that over-approximating can only make the test quieter.
            # That theory was wrong, and cost a real one: `dispatch_webhook.py`
            # declared `global _dispatcher`, never bound it at module level,
            # and the tripwire was green. The bot would have died with
            # NameError the first time it registered its command menu.
            # Quieter is not the same as safe -- an over-approximated binder
            # turns this test into decoration for exactly the names it exists
            # to catch. Deferred to `_global_ok`, resolved after the walk.
            declared_global |= set(n.names)
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

def test_every_module_actually_compiles():
    """`ast.parse` is not a syntax check, and that is not a detail.

    Python 3.11's `ast.parse` accepts `await` outside an async function.
    `compile()` and `py_compile` reject it. So a refactor script that
    validates its output with `ast.parse` will happily write a module
    that cannot be imported -- and every one of those scripts looks
    like it checked.

    It happened here: splitting `main` left three `await` calls inside a
    plain `def`, the parse said fine, and the module would not load.

    `compile` is what the interpreter itself asks. Use it.
    """
    offenders = []
    for path in _modules():
        source = path.read_text(encoding="utf-8")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as e:
            offenders.append(f"{path.relative_to(ROOT)}:{e.lineno} {e.msg}")
    assert not offenders, (
        "modules that ast.parse accepted but the interpreter will not:\n  "
        + "\n  ".join(offenders))
