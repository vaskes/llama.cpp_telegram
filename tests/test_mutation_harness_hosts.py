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


def _runs_the_suite_in_an_image(tree):
    """Does the harness run its suite inside the image?

    Two shapes count: it takes the image's name from the environment
    (`MUTATION_HARNESS_IMAGE`, the way the nightly job hands it over),
    or it names an image itself.
    """
    for value in _string_constants(tree):
        if "MUTATION_HARNESS_IMAGE" in value:
            return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "IMAGE":
                if isinstance(node.value, ast.Constant) and \
                        isinstance(node.value.value, str) and \
                        node.value.value.strip():
                    return True
    return False


def test_a_harness_the_nightly_job_picks_runs_its_suite_in_the_image():
    """A harness that runs on the runner's own Python measures nothing.

    The runner has no pytest: the suite does not start, the preflight
    reads "(no summary)", and the nightly job goes red on the night
    that harness was supposed to measure -- and the red says nothing
    about the harness, because the harness never ran.

    Measured on 2026-11-07: the document channel's night went red with
    exactly that, and on 2026-11-12 the file door's harness was one
    step away from the same thing (it ran on the runner's Python
    because it never asked for the image).

    So every harness the nightly job can pick -- every harness that
    does not declare REQUIRES_HOST -- has to run its suite in the
    image the job built.
    """
    dead = [
        p.name for p in HARNESSES
        if not _declares_host(ast.parse(p.read_text(encoding="utf-8")))
        and not _runs_the_suite_in_an_image(
            ast.parse(p.read_text(encoding="utf-8")))
    ]
    assert not dead, (
        "these harnesses run their suite on the runner's own Python, "
        f"where pytest is not installed: {dead}. The nightly job would "
        "fail on their nights with a red that says nothing about the "
        "harness, and the harness would measure nothing."
    )


def test_the_nightly_jobs_image_can_read_the_tree_through_git():
    """The pin tripwires read the tree with git, so the image needs git.

    The bot's own image is a runtime image: it carries no git. The
    harnesses run their suites inside THAT image, and two of the
    tripwires the exec timeout harness judges read the pinned
    side-car's commit with `git ls-tree` -- so in that image they
    cannot run at all, and the preflight is red for a reason that has
    nothing to do with the mutation.

    Measured on 2026-11-12: the mutation-nightly job went red with
    "2 failed, 73 passed", and the two were the pin tripwires --
    `test_the_contract_names_the_version_the_pinned_sandbox_declares`
    and `test_the_compatibility_table_names_the_pinned_commit`.

    Git also has to be allowed to read the tree it is mounted on: the
    container runs as root and the tree belongs to the runner, so git
    would refuse it as dubious ownership.
    """
    workflow = (ROOT / ".github" / "workflows" /
                "mutation-nightly.yml").read_text(encoding="utf-8")
    assert "FROM mutation-bot-base" in workflow, (
        "the nightly job's image block moved -- this tripwire reads the "
        "recipe by its first line")
    block = workflow.split("FROM mutation-bot-base", 1)[-1].split("EOF", 1)[0]
    assert "git" in block, (
        "the nightly job's test image does not install git -- the "
        "tripwires that read the pinned side-car's commit cannot run "
        "there, and the preflight will be red on a reason that has "
        "nothing to do with the mutation")
    assert "safe.directory" in block, (
        "the nightly job's image does not let git read the tree it is "
        "mounted on: the container runs as root and the tree belongs to "
        "the runner, so git refuses it as dubious ownership")


def test_the_nightly_rotation_reaches_every_runnable_harness():
    """The rotation has to come back to every harness, not to the first thirty.

    The slot used to be the day of the month, and the calendar gives
    thirty slots while there are thirty-six runnable harnesses: the
    slots 30..35 could never come up, so six harnesses had never run in
    this job at all. They looked accounted for -- they are in the list,
    and the tripwires above count them -- and they measured nothing,
    because the night that would have picked them did not exist.

    Measured on 2026-11-13, by running the whole list at once instead
    of waiting for the rotation to come around.

    The slot comes from the run counter now, so every harness comes back
    once per cycle of thirty-six nights. The weekly sweep runs all of
    them anyway; this tripwire says the rotation itself has no hole.
    """
    workflow = (ROOT / ".github" / "workflows" /
                "mutation-nightly.yml").read_text(encoding="utf-8")
    assert "date -u +%d" not in workflow, (
        "the nightly job's rotation is on the day of the month again: the "
        "calendar gives thirty slots a month and there are more runnable "
        "harnesses than that, so the tail of the list never comes up and "
        "those harnesses never run at all")
    assert "run_number" in workflow, (
        "the nightly job's rotation does not use the run counter -- a slot "
        "that comes from the calendar cannot reach every harness")
