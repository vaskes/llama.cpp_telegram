"""Common AST helpers for the T60 drift tripwires.

Each tripwire walks the project source AST and asserts
structural invariants that, if violated, would re-introduce
one of the 4 historical wave-trap regressions (T52 #M5,
T53 #R3, T57 #S16, acbd062 / S40, and 327b7b0).

Why a shared module:
  * Keep the AST walking code DRY.
  * Make the tripwires easy to read; each test is ~30 lines
    of "what to assert" rather than 100 lines of "how to
    walk the AST".
  * Future tripwires (when a new wave-trap class shows up)
    can reuse the helpers and stay focused.

Reference: the T60 report explains each tripwire in plain
English; this module is the implementation.
"""
from __future__ import annotations

import ast
import pathlib
from typing import Iterable


# Source paths the tripwires read. The bot team agreed to
# keep tests conftest-driven: these paths assume the
# project layout of v0.11.0 (telegram-bot/ as the package
# root, tests/ as a sibling).
TELEGRAM_BOT_DIR = (
    pathlib.Path(__file__).parent.parent / "telegram-bot"
)
HANDLERS_PY = TELEGRAM_BOT_DIR / "handlers.py"
DISPATCH_PY = TELEGRAM_BOT_DIR / "dispatch.py"
CALL_LLAMA_PY = TELEGRAM_BOT_DIR / "call_llama.py"
PERSONAL_TOOLS_DIR = TELEGRAM_BOT_DIR / "personal_tools"
BASH_COMMAND_PY = PERSONAL_TOOLS_DIR / "bash_command.py"


# Standard-library / known-builtin names that AST may pick up
# as Name() references. We subtract these from "undefined"
# when walking function bodies.
KNOWN_BUILTINS = frozenset({
    "True", "False", "None", "self", "cls",
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray",
    "bytes", "callable", "chr", "classmethod", "compile",
    "complex", "delattr", "dict", "dir", "divmod", "enumerate",
    "eval", "filter", "float", "format", "frozenset", "getattr",
    "globals", "hasattr", "hash", "hex", "id", "input", "int",
    "isinstance", "issubclass", "iter", "len", "list", "locals",
    "map", "max", "memoryview", "min", "next", "object", "oct",
    "open", "ord", "pow", "print", "property", "range", "repr",
    "reversed", "round", "set", "setattr", "slice", "sorted",
    "staticmethod", "str", "sum", "super", "tuple", "type",
    "vars", "zip", "__import__", "__name__", "__file__",
    "__doc__", "__all__", "__cached__", "__loader__",
    "__spec__", "__package__", "__builtins__", "Exception",
    "KeyboardInterrupt", "NotImplementedError", "RuntimeError",
    "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "StopAsyncIteration", "StopIteration",
    "ImportError", "OSError", "IOError", "FileNotFoundError",
    "PermissionError", "TimeoutError", "ConnectionError",
    "ConnectionRefusedError", "ConnectionResetError",
    "json", "asyncio", "os", "sys", "re", "subprocess",
    "logging", "time", "datetime", "pathlib",
})


def parse_source(path: pathlib.Path) -> ast.Module:
    """Read + parse a Python source file. Caches by file mtime
    via a simple sentinel: just re-parse every call, the
    tests run in ~50ms total so the cost is negligible."""
    return ast.parse(path.read_text())


def module_level_names(tree: ast.Module) -> set[str]:
    """Names defined at module level: function defs, class defs,
    simple assignments, annotated assignments. Excludes
    imports (those go in imported_names)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def imported_names(tree: ast.Module) -> set[str]:
    """Names introduced by `import x` or `from y import x`.
    Includes aliases."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def function_locals(func: ast.AST) -> set[str]:
    """Names defined locally inside func: function parameters,
    assignment targets, for-loop variables, with-as targets,
    except handler variables, comprehension targets.

    This is the set of "free" names a function body can use
    without raising NameError, beyond module-level names +
    imported names + builtins.
    """
    locals_: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.arg):
            locals_.add(node.arg)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                _add_name_targets(tgt, locals_)
        elif isinstance(node, ast.AnnAssign):
            _add_name_targets(node.target, locals_)
        elif isinstance(node, ast.AugAssign):
            _add_name_targets(node.target, locals_)
        elif isinstance(node, ast.For):
            _add_name_targets(node.target, locals_)
        elif isinstance(node, ast.AsyncFor):
            _add_name_targets(node.target, locals_)
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None:
                    _add_name_targets(item.optional_vars, locals_)
        elif isinstance(node, ast.AsyncWith):
            for item in node.items:
                if item.optional_vars is not None:
                    _add_name_targets(item.optional_vars, locals_)
        elif isinstance(node, ast.ExceptHandler):
            if node.name is not None:
                locals_.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Nested function definitions are local to their parent
            locals_.add(node.name)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            for gen in node.generators:
                _add_name_targets(gen.target, locals_)
        elif isinstance(node, ast.DictComp):
            for gen in node.generators:
                _add_name_targets(gen.target, locals_)
    return locals_


def _add_name_targets(node: ast.AST, out: set[str]) -> None:
    """Recursively add Name() ids from a target. Handles
    unpacking like `a, b = ...` and `a, *b = ...`."""
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _add_name_targets(elt, out)
    elif isinstance(node, ast.Starred):
        _add_name_targets(node.value, out)


def all_top_level_functions(tree: ast.Module) -> Iterable[ast.AST]:
    """Yield every function / async-function defined at
    module level. Inner functions are NOT included: they
    are visited via the parent's ast.walk and are caught
    by the per-function locals walk."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def find_calls_to(tree: ast.Module, function_name: str) -> list[ast.Call]:
    """Return every ast.Call node where the function being
    called is a bare `function_name` (not `module.function_name`
    and not `obj.method_name`). The matched calls are the
    ones that the AST would resolve as a top-level name lookup
    and therefore as a candidate for a missing-import bug."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == function_name:
            out.append(node)
        # Also match `from x import call_llama`; attribute
        # access like `x.call_llama` is a different pattern
        # and is handled by the specific test if needed.
    return out
