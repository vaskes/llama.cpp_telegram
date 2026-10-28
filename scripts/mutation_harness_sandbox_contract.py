#!/usr/bin/env python3
"""Mutation harness for the sandbox contract (2026-10-25).

The bot claims things about a service it does not contain: which
version of the side-car it talks to, whether a command in that sandbox
may become root, what the container's limits are. On 2026-10-25 the
operator turned the sandbox's privilege mode on and every one of those
claims went stale at once -- and nothing noticed, because nothing
compared the claim with the thing the claim was about.

So the claims live in one module (`personal_tools/sandbox_contract.py`),
the documents quote them, and the tripwire compares them with the
side-car this repo actually pins.

**Group A -- the behaviour.** Each mutation puts one of the old
defects back, and the suite has to notice:

  * the contract names a version the pinned sandbox does not have;
  * the privilege mode is claimed by a sandbox too old to have it;
  * the description the model reads drops the policy, or claims that
    sudo is impossible while it is not;
  * a 403 from the door is reported as a token problem again;
  * the meta door's refusal is read as metadata again (the bot then
    believes it knows the size of a file nobody described);
  * the documents drop the posture, quote a number the sandbox does not
    have, or state the posture in prose instead of through the knobs.

**Group B -- the tripwires.** Each mutation removes a tripwire *and* the
behaviour it is supposed to catch, and the suite has to go **green**. A
green suite with the tripwire gone is the proof that the tripwire was
the thing doing the work.

The rules this harness holds itself to:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the name of the test that went red is printed;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte;
  * the tripwires are silenced with a `return` at the top of the test
    body, not by renaming the function: a renamed test leaves the
    file, and the CODEBOOK's table quotes how many tests it has.

Where it runs: with the current interpreter by default, so it works in
a checkout. Set MUTATION_HARNESS_IMAGE to run the same tests inside the
test image instead.

    python3 scripts/mutation_harness_sandbox_contract.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The files that carry the rule and the tests that catch it.
TARGETS = [
    "tests/test_sandbox_contract.py",
    "tests/test_file_transport.py",
    "tests/test_send_file_tool.py",
    # The CODEBOOK names the tripwire; the row has to stay true.
    "tests/test_codebook_test_table.py",
    # The documents are part of the rule, and their sizes are checked.
    "tests/test_docs_current.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

CONTRACT = ROOT / "telegram-bot" / "personal_tools" / "sandbox_contract.py"
BASH = ROOT / "telegram-bot" / "personal_tools" / "bash_command.py"
TRANSPORT = ROOT / "telegram-bot" / "personal_tools" / "file_transport.py"
DOC = ROOT / "docs" / "BASH-SANDBOX.md"
TEST_CONTRACT = ROOT / "tests" / "test_sandbox_contract.py"
TEST_TRANSPORT = ROOT / "tests" / "test_file_transport.py"

#: The version the contract names right now, read from the contract.
#: The anchor used to be the literal ``"2.9.1"`` -- a version this repo
#: stopped pinning months ago. The mutation it belongs to then found no
#: site, was REFUSED, and the harness scored a refused mutation as
#: neither caught nor survived: the proof was green and empty.
#:
#: So the anchor names the version that is *there*, and the mutation
#: moves it to a version the pinned sandbox does not declare. A pin
#: bump changes the tree, not this file: the harness follows the pin
#: instead of rotting on the next one.
def _pinned_version() -> str:
    text = CONTRACT.read_text(encoding="utf-8")
    m = re.search(r'SANDBOX_PINNED_VERSION = "([^"]+)"', text)
    if not m:
        raise SystemExit(
            "the contract does not name a pinned version -- the harness "
            "cannot build an anchor for a line that is not there")
    return m.group(1)


PINNED_VERSION = _pinned_version()
#: A version the pinned sandbox does not declare, so the tripwire that
#: compares the contract with the sandbox's own source has to go red.
#: The patch number is one lower, so the wrong version stays below the
#: pinned one and cannot collide with a future release.
WRONG_VERSION = ".".join(
    PINNED_VERSION.split(".")[:-1] + [str(int(PINNED_VERSION.split(".")[-1]) - 1)])

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the contract names a version the sandbox does not have", "CAUGHT", [
        (CONTRACT,
         rf'SANDBOX_PINNED_VERSION = "{re.escape(PINNED_VERSION)}"',
         f'SANDBOX_PINNED_VERSION = "{WRONG_VERSION}"', 1),
    ]),
    ("the privilege mode is claimed by a sandbox too old for it", "CAUGHT", [
        (CONTRACT,
         r"PRIVILEGE_MODE_SUPPORTED = _version_tuple\(SANDBOX_PINNED_VERSION\) >= \(2, 8, 0\)",
         "PRIVILEGE_MODE_SUPPORTED = _version_tuple(SANDBOX_PINNED_VERSION) >= (3, 0, 0)", 1),
    ]),
    ("the description the model reads drops the sudo policy", "CAUGHT", [
        (BASH,
         r'"web pages with curl/wget\. " \+ SUDO_POLICY',
         '"web pages with curl/wget. Do NOT use it for: anything that would need sudo."', 1),
    ]),
    ("the description claims sudo is impossible while it is not", "CAUGHT", [
        (CONTRACT,
         r"(?m)^    if PRIVILEGE_MODE:\n",
         "    if False:\n", 1),
    ]),
    ("a 403 is reported as a token problem again", "CAUGHT", [
        (TRANSPORT,
         r'        return f"the sandbox refused to read the file: \{detail\}"',
         '        return (f"the sandbox rejected AGENT_STACK_TOKEN (HTTP 403); "\n'
         '                f"check the token in the bot\'s environment")', 1),
    ]),
    ("the meta door's refusal is read as metadata again", "CAUGHT", [
        (TRANSPORT,
         r"(?m)^    if r\.status_code != 200:\n        if r\.status_code == 404:\n",
         "    if r.status_code not in (200, 403):\n        if r.status_code == 404:\n", 1),
    ]),
    ("the document drops the posture", "CAUGHT", [
        (DOC,
         r"The privilege mode is ON: a command may become root inside the container, so the container is the perimeter and what is inside it is the blast radius\.",
         "The privilege mode is ON.", 1),
    ]),
    ("the document quotes a memory limit the sandbox does not have", "CAUGHT", [
        (DOC,
         r"`mem_limit: 4g`",
         "`mem_limit: 2G`", 1),
    ]),
    ("the document states the posture in prose", "CAUGHT", [
        (DOC,
         r"The stock posture is an empty capability set and new privileges",
         "The stock posture is `cap_drop: ALL` and `no-new-privileges`, an empty capability set and new privileges", 1),
    ]),
    ("the old posture claim comes back in the bot's own comment", "CAUGHT", [
        (BASH,
         r"#     container's posture is in sandbox_contract\.py -- a read-only\n",
         "#     non-root, cap_drop ALL, no-new-privileges, pids_limit 256,\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the version tripwire is gone, and the version is wrong", "SURVIVED", [
        (TEST_CONTRACT,
         r"(?m)^def test_the_contract_names_the_version_the_pinned_sandbox_declares\(\):\n",
         "def test_the_contract_names_the_version_the_pinned_sandbox_declares():\n    return\n", 1),
        (CONTRACT,
         rf'SANDBOX_PINNED_VERSION = "{re.escape(PINNED_VERSION)}"',
         f'SANDBOX_PINNED_VERSION = "{WRONG_VERSION}"', 1),
    ]),
    ("the permission tripwires are gone, and a 403 is a token again",
     "SURVIVED", [
        (TEST_CONTRACT,
         r"(?m)^def test_a_403_from_the_door_is_a_permission_and_a_401_is_the_token\(\):\n",
         "def test_a_403_from_the_door_is_a_permission_and_a_401_is_the_token():\n    return\n", 1),
        (TEST_TRANSPORT,
         r"(?m)^async def test_a_file_the_agent_cannot_read_is_a_permission_not_a_transport\(\n        sandbox\):\n",
         "async def test_a_file_the_agent_cannot_read_is_a_permission_not_a_transport(\n        sandbox):\n    return\n", 1),
        (TEST_TRANSPORT,
         r"(?m)^async def test_the_meta_door_refuses_an_unreadable_file_the_same_way\(sandbox\):\n",
         "async def test_the_meta_door_refuses_an_unreadable_file_the_same_way(sandbox):\n    return\n", 1),
        (TRANSPORT,
         r'        return f"the sandbox refused to read the file: \{detail\}"',
         '        return (f"the sandbox rejected AGENT_STACK_TOKEN (HTTP 403); "\n'
         '                f"check the token in the bot\'s environment")', 1),
    ]),
    ("the meta tripwire is gone, and the refusal is metadata again", "SURVIVED", [
        (TEST_TRANSPORT,
         r"(?m)^async def test_the_meta_door_refuses_an_unreadable_file_the_same_way\(sandbox\):\n",
         "async def test_the_meta_door_refuses_an_unreadable_file_the_same_way(sandbox):\n    return\n", 1),
        (TRANSPORT,
         r"(?m)^    if r\.status_code != 200:\n        if r\.status_code == 404:\n",
         "    if r.status_code not in (200, 403):\n        if r.status_code == 404:\n", 1),
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
            if " passed" in line or " failed" in line or "error" in line.lower()]
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
                # including the ops that already ran on it. Re-reading
                # the file from disk each time would undo the previous
                # op, and the harness would score a mutation it never
                # actually applied.
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
    # A refused or invalid mutation is a hole in the proof, not a pass:
    # the mutation never reached the code, so it proved nothing about
    # it. Counting it as neither caught nor survived let a dead anchor
    # sit in this file for months while the harness printed a green
    # line. The rule the harness holds itself to says so: "a drifted
    # anchor is REFUSED, never reported as SURVIVED" -- and a refused
    # mutation has to be loud enough to stop the run.
    if refused or invalid:
        print(f"HOLE IN THE PROOF: {refused} refused, {invalid} invalid "
              f"-- those mutations proved nothing", flush=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if wrong or refused or invalid else 0


if __name__ == "__main__":
    sys.exit(main())
