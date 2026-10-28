"""Every mutation harness must fail its run on a refused mutation.

The harnesses all claim the same thing about themselves: an anchor
that has drifted is REFUSED, never reported as SURVIVED. That claim
was true of the wording and false of the exit code, in twenty-six
harnesses. A refused mutation was counted, printed on its own line,
and then ignored by the verdict, so a harness whose anchors had
drifted printed a green summary and returned 0.

The evidence was empty and it looked filled. This is the tripwire for
that: the verdict of every harness must name the refused and the
invalid counters, and those counters must be assigned where the
verdict sits.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

HARNESSES = sorted(SCRIPTS.glob("mutation_harness*.py"))


def _main_function(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    return None


def _assigned_names(func):
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _verdict_returns(func, source):
    """The return statements of main() that carry the verdict -- the
    ones whose expression mentions at least one of the counters."""
    out = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        segment = ast.get_source_segment(source, node) or ""
        if "refused" in segment or "invalid" in segment:
            out.append(segment)
    return out


def test_the_harnesses_are_there():
    """The property below is only worth checking if there are
    harnesses to check."""
    assert len(HARNESSES) >= 30, f"only {len(HARNESSES)} harnesses found"


def test_every_harness_fails_its_run_on_a_refused_mutation():
    """The verdict must name the refused and the invalid counters."""
    quiet = []
    for path in HARNESSES:
        source = path.read_text(encoding="utf-8")
        func = _main_function(ast.parse(source))
        assert func is not None, f"{path.name}: no main()"
        verdicts = _verdict_returns(func, source)
        assert verdicts, f"{path.name}: no verdict names refused or invalid"
        named = [
            v for v in verdicts
            if "refused" in v and "invalid" in v
        ]
        if not named:
            quiet.append(path.name)
    assert not quiet, (
        "these harnesses do not name both counters in a verdict: "
        + ", ".join(quiet)
    )


def test_the_counters_are_assigned_where_the_verdict_sits():
    """A verdict that names a counter from another scope would raise
    NameError on the first run and prove nothing."""
    broken = []
    for path in HARNESSES:
        source = path.read_text(encoding="utf-8")
        func = _main_function(ast.parse(source))
        assigned = _assigned_names(func)
        for name in ("refused", "invalid"):
            if name not in assigned:
                broken.append(f"{path.name}: {name} is never assigned")
    assert not broken, "; ".join(broken)


def test_no_harness_reports_a_refused_mutation_as_a_survivor():
    """The wording must match the counting: a refused row is not a
    survivor and not a catch."""
    broken = []
    for path in HARNESSES:
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.If):
                continue
            test = ast.get_source_segment(source, node.test) or ""
            if "REFUSED" in test or "'refused'" in test:
                body = ast.get_source_segment(source, node) or ""
                if "survivors" in body or "survived" in body:
                    broken.append(path.name)
    assert not broken, (
        "these harnesses count a refused row as a survivor: "
        + ", ".join(sorted(set(broken)))
    )
