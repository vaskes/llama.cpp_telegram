#!/usr/bin/env python3
"""Mutation harness for the boot readiness (2026-10-25).

The 2026-10-25 production incident: the host rebooted, the bot came up
before the host's DNS did, the Application never finished initializing
-- and the bot went on serving traffic it could not answer. Telegram
got a 200 for every message and did not retry, so the messages were
lost, and nothing brought the bot back except a human restarting the
container.

The bug had two halves, and each half is a separate wrong:

  * the boot path published the Application to the registry **before**
    `initialize()` ran, so a failed initialization left a zombie in the
    registry -- and every later update read it, skipped the build, and
    died on `app.bot.id`;
  * the failure was swallowed as "setMyCommands failed (non-fatal)", so
    a bot that could not answer looked exactly like a healthy one.

**Group A -- the behaviour.** Each mutation puts one of those defects
back, and the suite has to notice.

**Group B -- the tripwires.** Each mutation removes a tripwire *and* the
behaviour it is supposed to catch, and the suite has to go **green**. A
green suite with the tripwire gone is the proof that the tripwire was
the thing doing the work: without it, the defect is invisible. This is
the direction a one-way harness never checks, and it is the one that
turns "the suite is green" into "the suite is green *because of this
test*".

The rules this harness holds itself to, because a mutation run that
cannot report INVALID is a decoration:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse()`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the name of the test that went red is printed, because "the suite
    went red" and "the test written for this rule went red" are
    different claims;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte;
  * the tripwires are silenced with a `return` at the top of the test
    body, not by renaming the function: a renamed test leaves the file,
    and the CODEBOOK's table quotes how many tests it has -- so a
    renamed test turns an unrelated tripwire red and hides the defect
    behind it.

Where it runs: with the current interpreter by default, so it works in
a checkout. Set MUTATION_HARNESS_IMAGE to run the same tests inside
the test image instead.

    python3 scripts/mutation_harness_readiness.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The files that carry the rule and the tests that catch it. A mutation
#: that no target can notice is a mutation that proves nothing.
TARGETS = [
    "tests/test_application_readiness.py",
    "tests/test_c3_initialize_failure.py",
    "tests/test_webhook_menu_registration.py",
    "tests/test_dispatch_group_seam.py",
    "tests/test_codebook_test_table.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

WEBHOOK = ROOT / "telegram-bot" / "webhook.py"
DISPATCH = ROOT / "telegram-bot" / "dispatch.py"
DISPATCH_HOOK = ROOT / "telegram-bot" / "dispatch_webhook.py"
TEST_READY = ROOT / "tests" / "test_application_readiness.py"
TEST_C3 = ROOT / "tests" / "test_c3_initialize_failure.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the Application is published before it is initialized", "CAUGHT", [
        (DISPATCH_HOOK,
         r"(?m)^        try:\n            await app\.initialize\(\)\n",
         "        dispatch_polling.set_dispatcher(app)\n"
         "        try:\n            await app.initialize()\n", 1),
    ]),
    ("the initialization is not retried", "CAUGHT", [
        (DISPATCH_HOOK,
         r"(?m)^    attempts: int = _INIT_ATTEMPTS,\n",
         "    attempts: int = 1,\n", 1),
    ]),
    ("a bot that cannot answer comes up anyway", "CAUGHT", [
        (WEBHOOK,
         r"(?m)^    except ApplicationNotReady:\n        raise\n",
         "", 1),
    ]),
    ("the dispatch path trusts the registry", "CAUGHT", [
        (DISPATCH,
         r"(?m)^    if app is None or not dispatch_webhook\._is_initialized\(app\):\n",
         "    if app is None:\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    # The pattern swallows the WHOLE signature, signature lines and all:
    # a pattern that matched only the `def` line would leave the
    # signature's continuation lines in the file, and the result would
    # not compile -- which the harness would report as INVALID, i.e. as
    # a mutation that caught nothing, when in fact the mutation was
    # fine and the pattern was wrong.
    ("the tripwire is gone, and the Application is published too early",
     "SURVIVED", [
        (TEST_READY,
         r"(?m)^async def test_a_failed_boot_publishes_nothing\([^)]*\):\n",
         "async def test_a_failed_boot_publishes_nothing():\n    return\n", 1),
        (TEST_C3,
         r"(?m)^def test_a_failed_initialize_publishes_nothing\(wired\):\n",
         "def test_a_failed_initialize_publishes_nothing(wired):\n    return\n",
         1),
        (TEST_C3,
         r"(?m)^def test_the_registry_never_holds_an_uninitialized_app\(wired\):\n",
         "def test_the_registry_never_holds_an_uninitialized_app(wired):\n"
         "    return\n", 1),
        (DISPATCH_HOOK,
         r"(?m)^        try:\n            await app\.initialize\(\)\n",
         "        dispatch_polling.set_dispatcher(app)\n"
         "        try:\n            await app.initialize()\n", 1),
    ]),
    ("the tripwire is gone, and the initialization is not retried",
     "SURVIVED", [
        (TEST_READY,
         r"(?m)^async def test_the_boot_retries_a_transient_failure\([^)]*\):\n",
         "async def test_the_boot_retries_a_transient_failure():\n    return\n",
         1),
        (DISPATCH_HOOK,
         r"(?m)^    attempts: int = _INIT_ATTEMPTS,\n",
         "    attempts: int = 1,\n", 1),
    ]),
    ("the tripwire is gone, and a bot that cannot answer comes up anyway",
     "SURVIVED", [
        (TEST_READY,
         r"(?m)^async def test_a_bot_that_cannot_answer_does_not_come_up_as_a_server\([^)]*\):\n",
         "async def test_a_bot_that_cannot_answer_does_not_come_up_as_a_server():\n"
         "    return\n", 1),
        (WEBHOOK,
         r"(?m)^    except ApplicationNotReady:\n        raise\n",
         "", 1),
    ]),
    ("the tripwire is gone, and the dispatch path trusts the registry",
     "SURVIVED", [
        (TEST_READY,
         r"(?m)^async def test_the_dispatch_path_repairs_a_zombie\([^)]*\):\n",
         "async def test_the_dispatch_path_repairs_a_zombie():\n    return\n", 1),
        (DISPATCH,
         r"(?m)^    if app is None or not dispatch_webhook\._is_initialized\(app\):\n",
         "    if app is None:\n", 1),
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
    caught by an import error three files away -- and those are not the
    same evidence.
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
                # op: two edits in one file would leave only the last
                # one in the tree, and the harness would score a
                # mutation it never actually applied.
                text = mutated.get(path, path.read_text(encoding="utf-8"))
                found = list(re.finditer(pattern, text, re.M))
                if len(found) != expect:
                    refused += 1
                    refused_here = True
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed -- that is how a harness reports a pass "
                          f"it never measured.", flush=True)
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
                    try:
                        compile(src, str(path), "exec")
                    except SyntaxError as e:
                        invalid += 1
                        print(f"  INVALID {name}: {path.name} does not "
                              f"compile -- it caught nothing ({e})", flush=True)
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
