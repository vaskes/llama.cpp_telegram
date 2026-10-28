#!/usr/bin/env python3
"""Mutation harness for the release workflow (2026-10-27).

T-FREEZE-AUTO-3. The workflow builds the image and publishes it on the
release when a tag is pushed. A workflow is code that runs without a
human in the loop, so the properties that make it safe are pinned by
`tests/test_release_workflow.py` -- and a pinned property is a claim
until something tries to break it.

**Group A -- the behaviour.** Each mutation puts one of the defects
back, and the suite has to notice:

  * the workflow reaches the production host;
  * the workflow reads a secret of the operator's;
  * a pull request releases;
  * every push to a branch releases;
  * the manual trigger is gone;
  * the token may write more than the release;
  * the image is not built;
  * the image is not attached to the release;
  * the tag is not validated;
  * the topology is copied into the workflow.

**Group B -- the tripwires.** The mutation removes the tripwires *and*
the behaviour they are supposed to catch, and the suite has to go
**green**. A green suite with the tripwire gone is the proof that the
tripwire was the thing doing the work.

The rules this harness holds itself to:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the name of the test that went red is printed;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte;
  * the tripwires are silenced with a `return` at the top of the test
    body, not by renaming the function: a renamed test leaves the file,
    and the CODEBOOK's table quotes how many tests it has.

    python3 scripts/mutation_harness_release_workflow.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tests that read the workflows, and the guard on the release one.
TARGETS = [
    "tests/test_release_workflow.py",
    "tests/test_python_version_compat.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
TEST_WORKFLOW = ROOT / "tests" / "test_release_workflow.py"

# The tripwires of this rule, silenced one by one in group B, with the
# signature each one takes.
TRIPWIRES = [
    ("test_it_releases_on_a_tag_and_not_on_every_push", "text"),
    ("test_the_workflow_can_be_run_by_hand", "text"),
    ("test_the_workflow_cannot_reach_the_production_host", "text"),
    ("test_the_workflow_holds_no_secret", "text"),
    ("test_the_token_may_write_the_release_and_nothing_else", "text"),
    ("test_the_image_is_built_from_the_repository", "text"),
    ("test_the_image_is_attached_to_the_release_for_that_tag", "text"),
    ("test_the_tag_is_validated_before_it_reaches_a_command", "text"),
    ("test_the_topology_is_not_in_the_workflow", "text"),
]


def _silence(name, signature):
    """The `return` that takes one tripwire out of the run.

    A renamed test would leave the file, and the CODEBOOK's table quotes
    how many tests it has -- so the count would move and the harness
    would be measuring the wrong defect.

    The signature is written out literally rather than captured with a
    backreference: the harness escapes backslashes in a replacement, and
    `\\1` would arrive as a literal backslash and a `1`.
    """
    return (rf"(?m)^def {name}\({signature}\):\n",
            f"def {name}({signature}):\n    return\n")


# (name, expectation, [(path, pattern, replacement, expected_sites)])
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the workflow reaches the production host", "CAUGHT", [
        (WORKFLOW,
         r"      - name: Build the image\n",
         "      - name: Reach the host\n"
         "        run: ssh -i ~/.ssh/deploy_key root@10.0.0.1 'deploy'\n"
         "      - name: Build the image\n", 1),
    ]),
    ("the workflow reads a secret", "CAUGHT", [
        (WORKFLOW,
         r"      - name: Build the image\n",
         "      - name: Read the key\n"
         "        env:\n"
         "          DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}\n"
         "        run: echo \"$DEPLOY_SSH_KEY\"\n"
         "      - name: Build the image\n", 1),
    ]),
    ("a pull request releases", "CAUGHT", [
        (WORKFLOW,
         r"  push:\n    tags:\n      - 'v\*'\n",
         "  push:\n    tags:\n      - 'v*'\n  pull_request:\n", 1),
    ]),
    ("every push to a branch releases", "CAUGHT", [
        (WORKFLOW,
         r"  push:\n    tags:\n      - 'v\*'\n",
         "  push:\n    branches:\n      - main\n    tags:\n      - 'v*'\n", 1),
    ]),
    ("the manual trigger is gone", "CAUGHT", [
        (WORKFLOW,
         r"  workflow_dispatch:\n", "", 1),
    ]),
    ("the token may write the packages", "CAUGHT", [
        (WORKFLOW,
         r"permissions:\n  contents: write\n",
         "permissions:\n  contents: write\n  packages: write\n", 1),
    ]),
    ("the image is not built", "CAUGHT", [
        (WORKFLOW,
         r"        run: docker build -t \"telegram-bot:\$TAG\" telegram-bot/\n",
         "        run: echo \"nothing to build\"\n", 1),
    ]),
    ("the image is not attached to the release", "CAUGHT", [
        (WORKFLOW,
         r"            gh release upload \"\$TAG\" bot-image\.tar\.gz deploy-side\.tar\.gz --clobber\n",
         "", 1),
        (WORKFLOW,
         r"            gh release create \"\$TAG\" --title \"\$TAG\" --notes-file release-notes\.md bot-image\.tar\.gz deploy-side\.tar\.gz\n",
         "            gh release create \"$TAG\" --title \"$TAG\" --notes-file release-notes.md\n", 1),
    ]),
    ("the tag is not validated", "CAUGHT", [
        (WORKFLOW,
         r"\^v\[0-9A-Za-z\]\[0-9A-Za-z\._-\]\{0,60\}\$",
         ".*", 1),
    ]),
    ("the topology is copied into the workflow", "CAUGHT", [
        (WORKFLOW,
         r"      - name: Build the image\n",
         "      - name: The stack\n"
         "        run: docker compose -f docker-compose.production.yml up -d\n"
         "      - name: Build the image\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires are gone, and the workflow reaches the host",
     "SURVIVED", [
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[0]), 1),
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[1]), 1),
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[2]), 1),
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[3]), 1),
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[4]), 1),
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[5]), 1),
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[6]), 1),
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[7]), 1),
        (TEST_WORKFLOW, *_silence(*TRIPWIRES[8]), 1),
        (WORKFLOW,
         r"      - name: Build the image\n",
         "      - name: Reach the host\n"
         "        run: ssh -i ~/.ssh/deploy_key root@10.0.0.1 'deploy'\n"
         "      - name: Build the image\n", 1),
    ]),
]


def _command():
    if IMAGE:
        return ["docker", "run", "--rm", "-v", f"{ROOT}:/w", "-w", "/w",
                "-e", "CONVERSATIONS_DB=/tmp/mut.db", "-e", "ALLOWED_USER_IDS=1",
                IMAGE, "python", "-m", "pytest", *TARGETS, "-q", "-rf",
                "-p", "no:cacheprovider"]
    return [sys.executable, "-m", "pytest", *TARGETS, "-q", "-rf",
            "-p", "no:cacheprovider"]


def run_tests():
    """('pass' | 'fail' | 'hang', summary, who_failed).

    `who_failed` is the list of the test names that went red. A harness
    that reports only "the suite went red" cannot tell a mutation that
    was caught by the test that was written for it from one that was
    caught by an import error three files away.
    """
    try:
        proc = subprocess.run(_command(), cwd=str(ROOT), capture_output=True,
                              text=True, timeout=SUITE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "hang", "the suite did not finish", []
    out = proc.stdout + proc.stderr
    tail = [line for line in out.splitlines()
            if " passed" in line or " failed" in line]
    if not tail:
        tail = [line for line in out.splitlines() if "error" in line.lower()]
    who = [line.split("::")[-1].split(" ")[0]
           for line in out.splitlines() if line.startswith("FAILED ")]
    return ("fail" if proc.returncode != 0 else "pass"), \
           (tail[-1].strip() if tail else "(no summary)"), who


def _restore(path: Path, before: bytes):
    path.write_bytes(before)
    if path.read_bytes() != before:
        raise SystemExit(f"RESTORE FAILED for {path} -- the tree is dirty")


def main():
    print(f"root: {ROOT}")
    print(f"runner: {'docker ' + IMAGE if IMAGE else sys.executable}")
    print("=== preflight on the CLEAN tree ===", flush=True)
    verdict, summary, _who = run_tests()
    print("  ", summary, flush=True)
    if verdict != "pass":
        print("PREFLIGHT FAILED -- the harness would be measuring itself")
        return 2

    caught = survivors = refused = invalid = 0
    wrong = []
    for name, expectation, ops in MUTATIONS:
        before = {}
        try:
            mutated = {}
            total = 0
            refused_here = False
            for path, pattern, replacement, expect in ops:
                # The source of an op is the CURRENT state of the file,
                # including the ops that already ran on it.
                text = mutated.get(path, path.read_text(encoding="utf-8"))
                found = list(re.finditer(pattern, text, re.M))
                if len(found) != expect:
                    refused += 1
                    refused_here = True
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed.", flush=True)
                    break
                total += len(found)
                mutated[path] = re.sub(pattern, replacement.replace("\\", "\\\\"),
                                       text, flags=re.M)
            if refused_here:
                continue

            for path, src in mutated.items():
                if src == path.read_text(encoding="utf-8"):
                    invalid += 1
                    print(f"  INVALID {name}: the substitution produced "
                          f"identical text in {path.name}", flush=True)
                    break
                before[path] = path.read_bytes()
                path.write_text(src, encoding="utf-8")
            else:
                for path, src in mutated.items():
                    if path.suffix == ".py":
                        try:
                            compile(src, str(path), "exec")
                        except SyntaxError as e:
                            invalid += 1
                            print(f"  INVALID {name}: {path.name} does not "
                                  f"compile -- it caught nothing ({e})",
                                  flush=True)
                            break
                else:
                    verdict, summary, who = run_tests()
                    red = verdict == "fail"
                    hung = verdict == "hang"
                    by = ("  by: " + ", ".join(who[:3])) if who else ""
                    if expectation == "CAUGHT":
                        if red or hung:
                            caught += 1
                            word = "CAUGHT" if red else "CAUGHT-BY-HANG"
                            print(f"  {word} {name}  [{total} site(s)]  "
                                  f"{summary}{by}", flush=True)
                        else:
                            wrong.append(name)
                            survivors += 1
                            print(f"  SURVIVED {name}  [{total} site(s)]  "
                                  f"{summary}{by}  <-- expected CAUGHT",
                                  flush=True)
                    else:  # SURVIVED
                        if not red and not hung:
                            survivors += 1
                            print(f"  SURVIVED {name}  [{total} site(s)]  "
                                  f"{summary}  (the tripwire was the thing "
                                  f"catching it)", flush=True)
                        else:
                            wrong.append(name)
                            print(f"  CAUGHT {name}  [{total} site(s)]  "
                                  f"{summary}{by}  <-- expected SURVIVED: "
                                  f"something else is also guarding this",
                                  flush=True)
        finally:
            for path, blob in before.items():
                _restore(path, blob)

    print(f"\n{caught} caught, {survivors} survived, {refused} refused, "
          f"{invalid} invalid  ({len(MUTATIONS)} mutations)", flush=True)
    if wrong:
        print("WRONG VERDICT:", wrong, flush=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if (wrong or refused or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
