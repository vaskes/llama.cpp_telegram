"""No docstring in the tree may carry a lone surrogate.

Python 3.14 encodes docstrings to UTF-8 while compiling them, so a
docstring whose source carries a surrogate escape (\\ud83d\\udc4d) makes
the whole module fail to compile:

    UnicodeEncodeError: 'utf-8' codec can't encode characters in
    position 64-65: surrogates not allowed

Measured on 3.14.10 against v0.17.0: the whole of
tests/test_c4_thinking.py was lost -- 28 tests, silently, because the
rest of the suite collected fine and the README's test badge was off
by exactly that many.

The rule is about docstrings only. A string *constant* with a lone
surrogate is legal at runtime, and the scrubber's own tests need one:
that is the input the production code is there to clean. The compiler
only encodes the docstring, so only the docstring is the problem.

The check is on the value, parsed with ast: a docstring that carries
the escape is the thing that breaks, whether the escape was written
directly or arrived through a raw string.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = ("telegram-bot", "tests")


def _python_files():
    for tree in TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            yield path


def _docstrings(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                yield node, doc


def test_no_docstring_carries_a_lone_surrogate():
    offenders = []
    seen = 0
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node, doc in _docstrings(tree):
            seen += 1
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in doc):
                name = getattr(node, "name", "<module>")
                offenders.append(f"{path.relative_to(ROOT)}:{name}")
    assert seen, "no docstrings were found -- the check would pass on anything"
    assert not offenders, (
        "these docstrings carry a lone surrogate, and Python 3.14 refuses "
        f"to compile the module: {offenders}")
