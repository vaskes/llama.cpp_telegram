"""Every mutation anchor must still point at the tree it claims.

A mutation harness finds its site by a pattern in the tree. When the
tree moves, the pattern stops matching, the harness calls the mutation
REFUSED, and -- until this was fixed -- it reported a clean run while
measuring nothing. The measurement that found it is a moment in time
(2026-10-28): twenty harnesses would have been refused, and the count
of the dead anchors was 39.

This is the freshness tripwire the TODO asked for: it runs on every
pytest, it costs a second, and it goes red when a harness's pattern
stops matching its tree.

It measures the same thing the harness measures. The count is taken
the way the harness takes it -- a regular expression for the family
that uses `re.finditer`, a literal count for the one that uses
`str.count` -- and it is taken against the progressively mutated text,
because that is what the harness counts against: the second op of a
mutation is counted on the text the first op produced.

The shapes are read from the modules, not guessed. There are four:
the umbrella form, where the path is in the mutation and the ops are
(pattern, replacement, expected); the per-feature form, where the path
is in the op; the dispatch-split form, which names its target relative
to `telegram-bot/` and counts its pattern as a literal; and the
readme-claims harness, which has no list of mutations at all because it
builds them out of the tree it is about to measure.
"""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

HARNESSES = sorted(SCRIPTS.glob("mutation_harness*.py"))

#: The harness that builds its mutations out of the tree it measures.
#: It has no static anchors, so there is nothing to check here -- but
#: that is a claim to verify, not a shape to skip.
DYNAMIC = "mutation_harness_readme_claims"


def _load(path):
    """Load a harness the way it loads itself: with the repository
    root as its argument, so the paths it computes point at this
    tree."""
    saved = sys.argv
    sys.argv = ["probe", str(ROOT)]
    try:
        spec = importlib.util.spec_from_file_location(
            "freshness_" + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.argv = saved


def _ops_of(mutation):
    """The ops of one mutation, as (target, pattern, replacement,
    expected) tuples, with the target resolved to a path.

    The shape is decided by the types, not by the name of the harness:
    an op of four elements carries its own path, an op of three does
    not, and the dispatch-split form names its target in the mutation
    itself.
    """
    if len(mutation) == 5 and isinstance(mutation[4], int):
        # dispatch-split: (name, path, pattern, replacement, expected),
        # the path relative to telegram-bot/, the pattern a literal.
        target = ROOT / "telegram-bot" / mutation[1]
        return [(target, mutation[2], mutation[3], mutation[4])]

    head, ops = mutation[1], mutation[2]
    if isinstance(head, Path):
        # umbrella: (name, path, ops[(pattern, replacement, expected)])
        return [(head, o[0], o[1], o[2]) for o in ops]
    # per-feature: (name, expectation, ops[(path, pattern, repl, expected)])
    if len(ops[0]) == 4:
        return [(o[0], o[1], o[2], o[3]) for o in ops]
    # per-feature without an expected count
    return [(o[0], o[1], o[2], None) for o in ops]


def _is_literal(path):
    """Does this harness count its patterns as literals? The dispatch
    split harness does, with `base.count(old)`; the rest use
    `re.finditer`."""
    return ".count(" in path.read_text(encoding="utf-8")


def _sites(pattern, text, literal):
    if literal:
        return text.count(pattern)
    return len(list(re.finditer(pattern, text, re.M)))


def test_the_harnesses_are_there():
    """The checks below are only worth running if there are harnesses
    to check."""
    assert len(HARNESSES) >= 30, f"only {len(HARNESSES)} harnesses found"


def _measure_anchors():
    """(checked, dead) over every harness, measured the way the
    harnesses themselves measure."""
    dead = []
    checked = 0
    for path in HARNESSES:
        if path.stem == DYNAMIC:
            continue
        module = _load(path)
        mutations = getattr(module, "MUTATIONS", None)
        assert mutations, f"{path.name}: no MUTATIONS to check"
        literal = _is_literal(path)
        for mutation in mutations:
            # The harness keeps the mutated text of every file apart:
            # two ops on one file see each other's work, two ops on
            # different files do not. Counting them against one
            # running text would report anchors as dead that are alive.
            mutated = {}
            for target, pattern, repl, expect in _ops_of(mutation):
                text = mutated.get(target)
                if text is None:
                    try:
                        text = target.read_text(encoding="utf-8")
                    except OSError as e:
                        dead.append(f"{path.stem}: {mutation[0]}: "
                                    f"cannot read {target}: {e}")
                        break
                found = _sites(pattern, text, literal)
                if expect is not None and found != expect:
                    dead.append(
                        f"{path.stem}: {mutation[0]}: {target} has "
                        f"{found} site(s) of {pattern[:48]!r}, "
                        f"the harness expects {expect}")
                elif expect is None and found == 0:
                    dead.append(
                        f"{path.stem}: {mutation[0]}: {target} has no "
                        f"site of {pattern[:48]!r} at all")
                checked += 1
                if literal:
                    mutated[target] = text.replace(pattern, repl, 1)
                else:
                    mutated[target] = re.sub(pattern, repl, text, flags=re.M)
    return checked, dead


def test_every_anchor_still_points_at_the_tree():
    """The freshness tripwire: a pattern that no longer finds what it
    claims would make the harness refuse the mutation and report a
    clean run."""
    checked, dead = _measure_anchors()
    assert not dead, (
        "these anchors no longer point at the tree:\n" + "\n".join(dead))
    assert checked >= 400, f"only {checked} anchors were checked"


def test_the_census_in_the_todo_is_the_measured_one():
    """The number in the doc is recomputed, not remembered.

    A census that lives only in prose rots the moment a harness joins
    the collection: the doc keeps the old number, the number is still
    true of the day it was written, and nothing goes red. That is the
    same rule the README's badge follows -- the number belongs to the
    thing that measures it.

    The claims this checks are the present-tense ones. The historical
    ones ("it found 39 dead anchors before") are history and stay.
    """
    todo = (ROOT / "docs" / "TODO.md").read_text(encoding="utf-8")
    checked, _dead = _measure_anchors()

    claimed = [int(m) for m in re.findall(r"(\d+) anchors, 0 dead", todo)]
    claimed += [int(m) for m in re.findall(r"covers (\d+) anchors", todo)]
    assert claimed, (
        "the TODO states no anchor count at all -- the census is a "
        "number, and a number that can vanish is not one")
    wrong = [c for c in claimed if c != checked]
    assert not wrong, (
        f"the TODO claims {wrong} anchors, the tree measures {checked}. "
        "The count is recomputed when a harness joins the collection.")

    harnesses = [int(m) for m in
                 re.findall(r"in all (\d+) harnesses", todo)]
    for count in harnesses:
        assert count == len(HARNESSES), (
            f"the TODO says a refused mutation fails the run in all {count} "
            f"harnesses; the collection holds {len(HARNESSES)}")


def test_the_dynamic_harness_builds_its_mutations_from_the_tree():
    """The readme-claims harness has no static anchors, so nothing above
    checks it. That is a claim about it, and the claim is worth
    checking: it reads the claims it is about to measure out of the
    tree, instead of writing them down."""
    path = SCRIPTS / f"{DYNAMIC}.py"
    assert path.is_file(), f"{path.name} is gone"
    source = path.read_text(encoding="utf-8")
    assert "def _mutations(" in source, (
        f"{path.name} has no _mutations(): the harness no longer builds "
        "its mutations from the tree, and the static check cannot see "
        "it")
    assert "MUTATIONS = _mutations(claims_read)" in source, (
        f"{path.name} does not build its mutations from the claims it "
        "read out of the tree")
