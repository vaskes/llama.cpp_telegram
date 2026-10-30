"""The lint job gates; the stylistic tail is only reported.

The job used to be `continue-on-error: true` with `--exit-zero` on one
file: a job that could not fail. Then it checked the whole package but
kept continue-on-error, so it reported and did not gate -- and an
external review read that correctly ("ruff -- advisory only ... this is
not a lint gate").

The split it has now:

  * the error-class rules (E9 syntax, F63 comparison, F7 syntax, F82
    undefined name) decide whether a push lands -- measured on
    2026-10-28, that rule set is clean on ruff 0.17.2 ("All checks
    passed!");
  * the stylistic tail (566 findings on the same version) is reported
    in the log and does not gate.

The tripwire is on the shape of the job, not on the wording of a
comment: a job-level continue-on-error, a missing gate step, a missing
advisory step, a matrix without 3.11, or an install that reads
requirements.txt instead of the lock -- each of those is a silent
return to the state this job just escaped.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "lint.yml"
TODO = ROOT / "docs" / "TODO.md"

GATE_RULES = "E9,F63,F7,F82"


def _text() -> str:
    assert WORKFLOW.is_file(), f"{WORKFLOW} does not exist"
    return WORKFLOW.read_text(encoding="utf-8")


def _todo_row(label: str) -> str:
    """One row of the TODO's table, as text."""
    text = TODO.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(f"| {label} |"):
            return line
    raise AssertionError(f"the TODO has no '{label}' row")


def _job(name: str) -> str:
    """The body of one job, up to the next job."""
    text = _text()
    marker = f"  {name}:"
    assert marker in text, f"the workflow has no {name} job"
    rest = text[text.index(marker) + len(marker):]
    nxt = re.search(r"\n  [A-Za-z_-]+:\n", rest)
    return rest[: nxt.start()] if nxt else rest


def _matrix_versions() -> set[str]:
    """The Python versions the pytest job runs."""
    body = _job("pytest")
    matrix = re.search(r"python:\s*\[(.*?)\]", body, re.S)
    assert matrix, "the pytest job has no python matrix"
    return set(re.findall(r"3\.\d+", matrix.group(1)))


def _steps(job_body: str) -> list:
    """Each step of a job, as text."""
    parts = re.split(r"\n      - ", job_body)
    return parts[1:] if len(parts) > 1 else []


def _without_comments(text: str) -> str:
    """The prose is not the setting.

    The job's header explains, in comments, that it used to carry
    continue-on-error. A check that reads the raw text would find that
    sentence and call it a setting -- the same mistake as a tripwire
    that greps for a knob name and finds it in a comment.
    """
    return "\n".join(line for line in text.splitlines()
                      if not line.strip().startswith("#"))


def test_the_ruff_job_is_not_continue_on_error():
    """The gate has to be able to fail the job."""
    body = _job("ruff")
    header = _without_comments(body.split("    steps:")[0])
    assert "continue-on-error" not in header, (
        "the ruff job carries continue-on-error, so a finding cannot "
        "stop a push -- the job reports and does not gate")


def test_the_gate_checks_the_rules_that_catch_code_that_would_not_run():
    steps = _steps(_job("ruff"))
    gating = [s for s in steps if f"--select {GATE_RULES}" in s]
    assert gating, (
        f"no step checks --select {GATE_RULES}; the job has to gate on the "
        f"error-class rules")
    for step in gating:
        assert "continue-on-error" not in _without_comments(step), (
            "the gating step is marked continue-on-error, so it cannot "
            "fail the job")


def test_the_stylistic_tail_is_reported_and_does_not_gate():
    steps = _steps(_job("ruff"))
    advisory = [s for s in steps
                if re.search(r"args: check telegram-bot/\s*$", s, re.M)]
    assert advisory, "the full rule set is not run at all, so its findings are never reported"
    for step in advisory:
        assert "continue-on-error: true" in _without_comments(step), (
            "the stylistic tail gates the push; 566 pre-existing "
            "findings would block every change until they are dealt with")


def test_the_suite_runs_on_the_versions_that_matter():
    """3.11 is what the image runs; 3.14 is what broke silently."""
    versions = _matrix_versions()
    assert "3.11" in versions, f"the matrix does not run the version the image runs: {versions}"
    assert "3.14" in versions, (
        f"the matrix does not run 3.14: {versions}. The tree broke there "
        f"and nothing said so -- a docstring with a surrogate escape cost "
        f"a whole test file, silently")


def _todo_ci_versions() -> set[str]:
    """The versions the TODO's CI row says the pytest job runs.

    Only the clause that says so: the row also names versions
    elsewhere (a red job on one of them), and a claim about what CI
    runs has to be read where it is made, not anywhere the number
    happens to appear.
    """
    row = _todo_row("CI")
    marker = "pytest on Python"
    assert marker in row, (
        "the TODO's CI row no longer says which versions the pytest job "
        "runs")
    tail = row[row.index(marker):]
    cut = tail.find("--")
    segment = tail[:cut] if cut != -1 else tail
    return set(re.findall(r"3\.\d+", segment))


def test_the_todo_names_every_python_the_matrix_runs():
    """The row that says what CI runs has to name every version.

    It said "3.11 and 3.12" for a week after the matrix grew a third
    version (3.14, on 2026-10-28). The row was wrong the way this
    project keeps catching itself being wrong: a claim about a
    workflow that nobody recomputes -- the same failure the badge
    count had, one file over.
    """
    versions = _matrix_versions()
    named = _todo_ci_versions()
    missing = sorted(versions - named)
    assert not missing, (
        f"the TODO's CI row does not name {missing} for the pytest job; "
        f"the matrix runs {sorted(versions)} (see .github/workflows/lint.yml)")
    extra = sorted(named - versions)
    assert not extra, (
        f"the TODO's CI row names {extra} for the pytest job; the matrix "
        f"runs {sorted(versions)} (see .github/workflows/lint.yml)")


def test_ci_installs_the_lock():
    """The suite has to run against the versions the image ships."""
    body = _job("pytest")
    installs = [line for line in body.splitlines() if "pip install -r" in line]
    assert installs, "the pytest job installs nothing"
    assert any("requirements.lock" in line for line in installs), (
        f"ci installs {installs}, not the lock -- the suite would test "
        f"transitive versions the image does not ship")
