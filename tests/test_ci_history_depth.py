"""The gate has to be able to see the history it judges.

**Measured 2026-10-31 on this repository.** A pull-request checkout by
`actions/checkout` is, by default, a **depth-1** checkout of the MERGE
ref. `git log --all` in that checkout sees exactly one commit -- the
merge commit -- and the commits of the branch itself are not in it.

The tripwire that keeps a model out of the contributors list
(`tests/test_no_model_attribution.py`) reads `git log --all`. On a
depth-1 merge-ref checkout it therefore reads the merge commit's message
and nothing else, and it reports GREEN on a branch that carries the
trailer. The check was not wrong about what it saw; it was blind.

This file has two halves:

* the **config half** -- the workflows that run the suite must check out
  the whole history, and this test says so in the same commit that
  changes it, so the blindness cannot come back unnoticed;
* the **self-check half** -- the tripwire that reads the history must
  notice that it cannot see it, rather than reporting a clean bill of
  health on a tree it never looked at.

The second half is the one that matters. The first half is a fact about
a YAML file, and it can be edited away by anyone; the second half is a
property of the check itself, and it is what turns "the gate was blind"
from a mystery into a red job.

**What counts as "blind".** `git rev-parse --is-shallow-repository`
answers it exactly: `true` in a shallow checkout, `false` in a full
one. It is the same answer for a `--depth 1` clone and for a merge-ref
checkout, and it needs no network and no assumptions about the server.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

#: The workflows that run the test suite. A checkout that feeds one of
#: them has to carry the history the tripwires read.
#:
#: * `lint.yml` -- the pytest job, the gate on every push and PR;
#: * `mutation-nightly.yml` -- the harnesses run the suite inside the
#:   image, against this checkout, so the same tripwire runs there.
SUITE_WORKFLOWS = ("lint.yml", "mutation-nightly.yml")

#: `actions/checkout` with this input fetches the whole history.
FULL_HISTORY = "fetch-depth: 0"


def _text(name: str) -> str:
    path = WORKFLOWS / name
    assert path.is_file(), f"{path} does not exist"
    return path.read_text(encoding="utf-8")


def _checkout_blocks(text: str) -> list:
    """Each `actions/checkout` step, as text.

    The step and its `with:` block are taken together, because the
    setting lives in the block: a checkout that fetches the whole
    history says so in the same five lines that name the action.

    A comment inside the block does not end it. That is not a detail:
    the first version of this parser stopped at the first line it did
    not recognise, so a `with:` block whose `fetch-depth:` sat under an
    explanatory comment was read as a block without it -- and this
    tripwire reported the workflow as shallow while the workflow was
    exactly right. A check that cannot read the thing it is checking
    fails in the direction that costs the reader a false alarm, so the
    comment lines are skipped rather than treated as the end.
    """
    blocks = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "actions/checkout" not in line:
            continue
        block = [line]
        for follow in lines[i + 1:]:
            stripped = follow.strip()
            if stripped.startswith("#"):
                block.append(follow)
                continue
            if not stripped.startswith(("with:", "fetch-depth", "submodules",
                                        "ref:", "path:", "token:", "lfs",
                                        "persist-credentials", "ssh-key",
                                        "show-progress")):
                break
            block.append(follow)
        blocks.append("\n".join(block))
    return blocks


def _job(text: str, name: str) -> str:
    """The body of one job, up to the next job."""
    marker = f"  {name}:"
    assert marker in text, f"the workflow has no {name} job"
    rest = text[text.index(marker) + len(marker):]
    nxt = re.search(r"\n  [A-Za-z_-]+:\n", rest)
    return rest[:nxt.start()] if nxt else rest


def test_the_suite_workflows_are_still_there():
    """A guard on the guard: if the files were renamed or deleted, the
    checks below would pass by finding nothing."""
    for name in SUITE_WORKFLOWS:
        assert (WORKFLOWS / name).is_file(), (
            f".github/workflows/{name} is gone -- the suite runs from a "
            "workflow, and a tripwire that cannot find the workflow it "
            "governs is not a tripwire")


def test_the_pytest_job_checks_out_the_whole_history():
    """The gate that runs on every push and pull request.

    The comment in the workflow says why; this test says it in a place
    that fails when the comment and the setting disagree.
    """
    body = _job(_text("lint.yml"), "pytest")
    blocks = _checkout_blocks(body)
    assert blocks, "the pytest job has no checkout step at all"
    shallow = [b for b in blocks if FULL_HISTORY not in b]
    assert not shallow, (
        "the pytest job checks out a limited history:\n"
        + "\n---\n".join(shallow)
        + "\n\nA depth-1 pull-request checkout is the merge ref alone, so "
        "`git log --all` sees one commit and the tripwire that reads the "
        "history cannot see the commits it is supposed to judge. Set "
        f"`{FULL_HISTORY}` on the checkout.")


def test_the_nightly_harness_checks_out_the_whole_history():
    """The harnesses run the suite too, against the same checkout."""
    blocks = _checkout_blocks(_text("mutation-nightly.yml"))
    assert blocks, "the nightly workflow has no checkout step at all"
    shallow = [b for b in blocks if FULL_HISTORY not in b]
    assert not shallow, (
        "the nightly workflow checks out a limited history:\n"
        + "\n---\n".join(shallow)
        + "\n\nThe harnesses run the whole suite inside the image, so the "
        "tripwire that reads the history runs there too -- against a "
        f"checkout it cannot see. Set `{FULL_HISTORY}` on the checkout.")


def test_this_checkout_can_see_the_history_it_judges():
    """The self-check.

    The tripwire in `test_no_model_attribution.py` reads `git log --all`.
    If the checkout it runs in is shallow, that read is partial and every
    green it produces is a green about the wrong tree.

    This is the check that turns the blindness into a named failure
    instead of a mystery. It runs on the repository as it is checked
    out, so it is true of the CI checkout as well as of a developer's
    clone.
    """
    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "rev-parse",
             "--is-shallow-repository"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError(
            f"git is not usable here ({exc}), so the history-reading "
            "tripwires cannot be trusted on this machine") from exc
    assert proc.returncode == 0, (
        f"git rev-parse --is-shallow-repository failed: "
        f"{proc.stderr.strip()}")
    assert proc.stdout.strip() == "false", (
        "this checkout is SHALLOW. The tripwires that read the commit "
        "history cannot see it, so their green means nothing. Fetch the "
        "full history (`git fetch --unshallow`) before trusting them.")


def test_the_detector_can_see_a_shallow_repository():
    """Control: the check above must be able to fail.

    The same question, asked of a repository that IS shallow: a
    temporary one, cloned with `--depth 1`. If the command cannot
    distinguish the two, the green above is decoration.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        shallow = Path(tmp) / "shallow"
        proc = subprocess.run(
            ["git", "init", "-q", "--initial-branch=main", str(shallow)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"could not create a scratch repository: {proc.stderr.strip()}")
        (shallow / "file.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(shallow), "add", "file.txt"],
                       capture_output=True, text=True, timeout=60)
        subprocess.run(
            ["git", "-C", str(shallow), "-c", "user.email=t@example.com",
             "-c", "user.name=t", "commit", "-q", "-m", "one"],
            capture_output=True, text=True, timeout=60)
        # Turn it into a shallow repository the way a server does it.
        (shallow / ".git" / "shallow").write_text(
            subprocess.run(
                ["git", "-C", str(shallow), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=60
            ).stdout.strip() + "\n", encoding="utf-8")
        out = subprocess.run(
            ["git", "-C", str(shallow), "rev-parse", "--is-shallow-repository"],
            capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "true", (
        "the detector could not recognise a shallow repository, so the "
        "check that this checkout is not shallow cannot fail and proves "
        "nothing")
