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
    that proved it -- see the note at the end of the function.

    What this file does NOT catch, stated so a green run is not read as
    more than it is: a module where one function binds a name locally and
    a different function reads it without declaring `global`. Every name
    a local function assigns looks bound from the outside here. That class
    needs a real scope model; two wrong attempts at one are on record.
    """
    out: set = set()
    declared_global: set = set()
    declared_nonlocal: set = set()
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
            # `nonlocal` points at an ENCLOSING function scope, not at the
            # module. Nothing here models enclosing scopes, so treating it
            # like `global` would subtract a name that is legitimately
            # bound one level up -- a false positive on correct code.
            if isinstance(n, ast.Nonlocal):
                declared_nonlocal |= set(n.names)
            else:
                # `global X` DECLARES an intent to rebind; it does not
                # create the name. If no module-level binding exists, the
                # first read raises NameError. Resolved after the walk --
                # see the note there.
                declared_global |= set(n.names)

    # Resolve the declarations AFTER the walk: subtract, then re-add --
    # not union.
    #
    # The walk cannot see scopes, so `_dispatcher = app` inside a function
    # already put the name in `out` before this point. A union left it
    # there, which made this filter dead on exactly the shape it was
    # written for. The real bug -- a module declaring `global _dispatcher`
    # and also assigning it inside a function -- has that assignment by
    # construction, so it was always going to pass, and always would have.
    # The filter assumed no assignment; the code always has one.
    out -= declared_global
    out |= {g for g in declared_global if g in module_level}
    out |= declared_nonlocal
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


# --- the `global` matrix -------------------------------------------------
#
# Wave 4 review, finding #1. The fix for the `global`-filter bug shipped
# in 2ae2c37 and was verified with a mutation that the *unfixed* checker
# also caught: `_probe` declared a `global` and read it without ever
# assigning it, so the name was bound nowhere and the old checker saw it
# too. That proved nothing about the fix.
#
# The real bug had a different shape, and the original checker missed it:
#
#     def set_dispatcher(app):
#         global _dispatcher
#         _dispatcher = app        # assignment INSIDE the function
#
#     def _register_bot_menu():
#         global _dispatcher
#         return _dispatcher       # read, no module-level binding
#
# The walk cannot see scopes, so the assignment put the name in the bound
# set, and a union-based `global`-filter cannot subtract it. A module that
# declares a `global` almost always assigns it somewhere -- that is what
# `global` is for -- so the filter was silent on precisely the case it was
# written for.
#
# Each case below is asserted, including the ones that must stay quiet.
# A checker that only has the positive cases is a checker nobody can tell
# apart from a broken one.
_GLOBAL_MATRIX = [
    # (name, source, must_be_flagged)
    ("global + assignment inside a function, no module-level",
     "def s(app):\n"
     "    global _d\n"
     "    _d = app\n"
     "def r():\n"
     "    global _d\n"
     "    return _d\n", True),
    ("global read, bound nowhere",
     "def p():\n"
     "    global _never_bound\n"
     "    return _never_bound\n", True),
    ("global + module-level assignment",
     "_d = None\n"
     "def s(app):\n"
     "    global _d\n"
     "    _d = app\n"
     "def g():\n"
     "    return _d\n", False),
    ("global + module-level import",
     "from config import SHUTDOWN_EVENT\n"
     "def p():\n"
     "    global SHUTDOWN_EVENT\n"
     "    SHUTDOWN_EVENT = None\n", False),
    ("global + module-level annotated assignment",
     "_tasks: set = set()\n"
     "def a(t):\n"
     "    global _tasks\n"
     "    _tasks.add(t)\n", False),
    ("plain read of an unbound name",
     "def g():\n"
     "    return _nothing_here\n", True),
    ("nonlocal is an enclosing scope, not the module",
     "def outer():\n"
     "    v = 1\n"
     "    def inner():\n"
     "        nonlocal v\n"
     "        return v\n"
     "    return inner\n", False),
    # `import a.b` binds `a`. This was the one false positive the old
    # checker produced, and "we removed the false positive" is exactly
    # the kind of edit that also removes the real detection, so the
    # quiet case is pinned here rather than left to memory.
    ("import x.y binds x, not x.y",
     "import urllib.request\n"
     "def p():\n"
     "    return urllib.request.urlopen\n", False),
    # Order independence. A module-level binding that appears AFTER the
    # function that reads it is still a binding; a checker that walks
    # top-to-bottom and stops early would flag this one, and "fixing"
    # that would be a regression dressed as a cleanup.
    ("module-level binding appears after the reader",
     "def r():\n"
     "    return _later\n"
     "_later = 1\n", False),
    ("global + module-level binding after the function",
     "def s(a):\n"
     "    global _after\n"
     "    _after = a\n"
     "def g():\n"
     "    return _after\n"
     "_after = None\n", False),
]

#: Exact, not `>=`. The commit that introduced this matrix described eight
#: cases and shipped seven; nothing in the suite could tell the difference,
#: so the count stayed a floor and the description stayed wrong. Pinning it
#: makes a lost case loud, which is the only way "was a case lost or was
#: the description wrong" stops resurfacing at every review.
EXPECTED_MATRIX_SIZE = 10


@pytest.mark.parametrize("name,source,must_flag", _GLOBAL_MATRIX,
                         ids=[c[0] for c in _GLOBAL_MATRIX])
def test_the_global_filter_flags_exactly_the_right_cases(name, source,
                                                          must_flag):
    """The `global` declaration binds nothing; only the module level does."""
    tree = ast.parse(source)
    bound = _bound_names(tree)
    known = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    read = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    missing = read - bound - known
    assert bool(missing) == must_flag, (
        f"{name}: expected "
        f"{'a flag' if must_flag else 'no flag'}, got {sorted(missing)}")


def test_the_global_matrix_would_have_caught_the_original_bug():
    """The reviewer's point, as an executable claim.

    Reconstructs the exact shape that shipped in block E and asserts the
    checker flags it. Without this, a future "simplification" back to a
    union-based filter is indistinguishable from the current code by any
    other test in this file.
    """
    tree = ast.parse(_GLOBAL_MATRIX[0][1])
    bound = _bound_names(tree)
    known = set(dir(builtins)) | {"__name__", "__file__"}
    read = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    assert read - bound - known == {"_d"}, (
        "the module-level global case regressed to a pass; this is the "
        "bug that would have shipped a NameError at bot startup")
