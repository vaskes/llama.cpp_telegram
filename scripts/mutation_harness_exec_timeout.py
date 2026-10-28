#!/usr/bin/env python3
"""Mutation harness for "a command may run for an hour" (2026-10-27).

The operator measured the sandbox's own limit from the inside: a command
was cut off at a minute, and the output it had already produced went
with it. His ruling: the ceiling is an hour.

The numbers are a chain, and a chain is only as good as its weakest
link:

    the command's limit   3600 s   the side-car (AGENT_EXEC_TIMEOUT)
    the proxy's wait      +15 s    the side-car
    the bot's timeout     +15 s    the bot (CLIENT_TIMEOUT_SECONDS)
    the turn's budget    7200 s    the bot (LOOP_BUDGET_SEC)
    the engine's timeout   x1.1    the bot (LLAMA_HTTP_TIMEOUT_SEC)

The turn's budget has to be longer than the *command*, not just than the
answer: the guard that enforces the budget runs at the loop's tail, so a
command that runs for the whole limit has to fit inside the budget --
otherwise the guard throws the answer away the moment the command comes
back, and the user gets "time budget exceeded" instead of the answer.

**Group A -- the behaviour.** Each mutation puts the defect back, and
the suite has to notice:

  * a profile's budget meets the command's limit (or falls below it);
  * the bot believes in a minute again, or in more than the side-car
    accepts;
  * the bot's client timeout meets the proxy's wait;
  * the words the model reads stop meaning the number the code believes
    in, or stop being there at all;
  * the prompt names a limit of its own again.

**Group B -- the tripwires.** The mutation removes the tripwires *and*
the behaviour they are supposed to catch, and the suite has to go
**green**. A green suite with the tripwire gone is the proof that the
tripwire was the thing doing the work.

The rules this harness holds itself to:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse()`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the name of the test that went red is printed;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte;
  * the tripwires are silenced with a `return` at the top of the test
    body, not by renaming the function: a renamed test leaves the file,
    and the CODEBOOK's table quotes how many tests it has;
  * every tripwire that guards the removed behaviour is silenced -- a
    group B that only silences one of them proves nothing, because the
    neighbour caught the mutation instead.

    python3 scripts/mutation_harness_exec_timeout.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tests that guard this rule, and the neighbours of the paths it
#: touches -- a green group B is only a proof if the whole surface is
#: in the run.
TARGETS = [
    "tests/test_limits.py",
    "tests/test_sandbox_contract.py",
    "tests/test_prompts.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

LIMITS = ROOT / "telegram-bot" / "limits.py"
CONTRACT = ROOT / "telegram-bot" / "personal_tools" / "sandbox_contract.py"
PROMPT = ROOT / "telegram-bot" / "prompt_text" / "group_context.py"
CONTRACT_TEST = ROOT / "tests" / "test_sandbox_contract.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the dev profile's budget meets the command's limit", "CAUGHT", [
        (LIMITS,
         r'"LOOP_BUDGET_SEC":        7200,  # 2 h\. Raised 2026-10-27 from 3600 s:',
         '"LOOP_BUDGET_SEC":        3600,  # 2 h. Raised 2026-10-27 from 3600 s:',
         1),
    ]),
    ("the public profile's budget falls below the command's limit", "CAUGHT", [
        (LIMITS,
         r'"LOOP_BUDGET_SEC":        7200,   # 2 h\. Raised 2026-10-27 from 900 s:',
         '"LOOP_BUDGET_SEC":          600,   # 2 h. Raised 2026-10-27 from 900 s:',
         1),
    ]),
    ("the bot believes in a minute again", "CAUGHT", [
        (CONTRACT, r"EXEC_TIMEOUT_SECONDS = 3600", "EXEC_TIMEOUT_SECONDS = 60", 1),
    ]),
    ("the bot believes in more than the side-car accepts", "CAUGHT", [
        (CONTRACT, r"EXEC_TIMEOUT_SECONDS = 3600", "EXEC_TIMEOUT_SECONDS = 7200", 1),
    ]),
    ("the bot's client timeout meets the proxy's wait", "CAUGHT", [
        (CONTRACT,
         r"    EXEC_TIMEOUT_SECONDS \+ PROXY_WAIT_GRACE_SECONDS \+ 15\n",
         "    EXEC_TIMEOUT_SECONDS + PROXY_WAIT_GRACE_SECONDS\n", 1),
    ]),
    ("the words stop meaning the number", "CAUGHT", [
        (CONTRACT, r'EXEC_TIMEOUT_PHRASE = "an hour"',
         'EXEC_TIMEOUT_PHRASE = "a minute"', 1),
    ]),
    ("the description stops naming the limit", "CAUGHT", [
        (CONTRACT, r'f"than \{EXEC_TIMEOUT_PHRASE\}\."', '"than a while."', 1),
        (CONTRACT, r'f"\{EXEC_TIMEOUT_PHRASE\}\."', '"a while."', 1),
    ]),
    ("the prompt names a limit of its own again", "CAUGHT", [
        (PROMPT, r"a bounded command timeout", "60s timeout", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires are gone, and the bot believes in a minute again",
     "SURVIVED", [
         (CONTRACT_TEST,
          r"(?m)^def test_the_exec_limit_is_the_sandbox_knob_and_the_bot_stays_above_it\(\):\n",
          "def test_the_exec_limit_is_the_sandbox_knob_and_the_bot_stays_above_it():\n"
          "    return\n", 1),
         (CONTRACT_TEST,
          r"(?m)^def test_the_phrase_the_model_reads_means_the_number_the_code_believes_in\(\):\n",
          "def test_the_phrase_the_model_reads_means_the_number_the_code_believes_in():\n"
          "    return\n", 1),
         (CONTRACT_TEST,
          r"(?m)^def test_the_description_says_the_limit_the_contract_believes_in\(\):\n",
          "def test_the_description_says_the_limit_the_contract_believes_in():\n"
          "    return\n", 1),
         (CONTRACT, r"EXEC_TIMEOUT_SECONDS = 3600", "EXEC_TIMEOUT_SECONDS = 60", 1),
         (CONTRACT, r'EXEC_TIMEOUT_PHRASE = "an hour"',
          'EXEC_TIMEOUT_PHRASE = "a minute"', 1),
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
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if (wrong or refused or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
