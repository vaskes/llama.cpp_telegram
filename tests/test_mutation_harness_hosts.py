"""The marker that skips a harness from the nightly job must be true.

The nightly mutation job (`.github/workflows/mutation-nightly.yml`) runs
the harnesses that can run without the production host. It finds them by
grepping for `REQUIRES_HOST = True` -- a marker inside the harness
itself, so the skip list is not a second copy of the world that nobody
rechecks.

Both directions matter, and they fail in opposite ways:

  * a harness that reaches the host over SSH but does not say so runs in
    the nightly job, fails there every night, and takes the job red
    with it;
  * a harness that says it needs the host but does not is skipped
    silently -- and a harness nobody runs measures nothing while
    looking accounted for.

So: the marker and the behaviour must agree, in both directions.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

HARNESSES = sorted(SCRIPTS.glob("mutation_harness*.py"))


def _docstring_nodes(tree):
    """The string expressions that are docstrings, not code."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _string_constants(tree):
    """Every string in the module that is code, not a docstring."""
    docstrings = _docstring_nodes(tree)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.add(node.value)
    return out


def _declares_host(tree):
    """The marker must be True -- a name assigned False declares nothing."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "REQUIRES_HOST":
                if isinstance(node.value, ast.Constant) and \
                        node.value.value is True:
                    return True
    return False


def _reaches_the_host(tree):
    """Does the harness shell out to ssh or scp?"""
    for value in _string_constants(tree):
        head = value.strip().split(" ")[0] if value.strip() else ""
        if head in ("ssh", "scp"):
            return True
    return False


def test_the_harnesses_are_there_to_check():
    """A check over an empty collection proves nothing."""
    assert len(HARNESSES) >= 20, (
        f"only {len(HARNESSES)} harnesses found -- the collection moved, "
        "and this test would pass on a glob that matches nothing"
    )


def test_a_harness_that_reaches_the_host_says_so():
    undeclared = [
        p.name for p in HARNESSES
        if _reaches_the_host(ast.parse(p.read_text(encoding="utf-8")))
        and not _declares_host(ast.parse(p.read_text(encoding="utf-8")))
    ]
    assert not undeclared, (
        "these harnesses shell out to ssh/scp but do not declare "
        f"REQUIRES_HOST = True: {undeclared}. The nightly job would try to "
        "run them, and the job would be red every night for a reason "
        "nobody wrote down."
    )


def test_a_harness_that_says_it_needs_the_host_reaches_the_host():
    unjustified = [
        p.name for p in HARNESSES
        if _declares_host(ast.parse(p.read_text(encoding="utf-8")))
        and not _reaches_the_host(ast.parse(p.read_text(encoding="utf-8")))
    ]
    assert not unjustified, (
        "these harnesses declare REQUIRES_HOST = True but never shell out "
        f"to ssh/scp: {unjustified}. A marker without the behaviour is a "
        "silent skip: the harness stops running and nobody notices."
    )
