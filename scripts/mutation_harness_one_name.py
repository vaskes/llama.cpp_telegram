#!/usr/bin/env python3
"""Mutation harness for "one person, one name in the record" (2026-10-27).

The record is the source of truth -- the operator keeps this history for a
LoRA dataset -- and it is written by two paths: the text handler and the
group-absorb path. On 2026-10-27 the two disagreed. The text handler
wrote "Vasisualy Lohankin"; the absorb path wrote "Vasisualy" (or the
handle "@V_bot") for the same person, in the same chat. Measured on the
production record: rows 10690 and 10693, one message, two names.

The operator's decision: the full Telegram nickname is canonical, and
anything shorter is an artefact of a second resolver.

**Group A -- the behaviour.** Each mutation puts one of the defects back,
and the suite has to notice:

  * the handle wins over the full name;
  * the last name is dropped;
  * the absorb path resolves the name itself again;
  * the module that uses the policy grows its own copy of it.

**Group B -- the tripwires.** The mutation removes the tripwires *and* the
behaviour they are supposed to catch, and the suite has to go **green**. A
green suite with the tripwire gone is the proof that the tripwire was the
thing doing the work.

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
    and the CODEBOOK's table quotes how many tests it has.

    python3 scripts/mutation_harness_one_name.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tests that guard this rule, and the neighbours of the path it
#: touches -- a green group B is only a proof if the whole surface is
#: in the run.
TARGETS = [
    "tests/test_one_name_per_person.py",
    "tests/test_dispatch_group_seam.py",
    "tests/test_record_message_once.py",
    "tests/test_t65_reply_to_human.py",
    "tests/test_prompt_guard.py",
    "tests/test_s1_sender_sanitisation.py",
    "tests/test_handler_skeleton.py",
    "tests/test_handler_middle.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

SENDER = ROOT / "telegram-bot" / "sender_name.py"
HANDLERS = ROOT / "telegram-bot" / "handlers.py"
ABSORB = ROOT / "telegram-bot" / "dispatch_group.py"
TEST_ONE_NAME = ROOT / "tests" / "test_one_name_per_person.py"

# The tripwires of this rule, silenced one by one in group B, with the
# signature each one takes.
TRIPWIRES = [
    ("test_the_absorb_path_writes_the_full_name", "written"),
    ("test_the_absorb_path_names_the_target_the_same_way", "written"),
    ("test_both_writers_use_the_same_resolver", "written"),
    ("test_there_is_one_resolver_and_not_two", ""),
    ("test_the_absorb_module_does_not_resolve_names_itself", ""),
    ("test_the_comparison_can_see_a_wrong_value", ""),
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
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the handle wins over the full name", "CAUGHT", [
        (SENDER,
         r"    full = _safe_sender_label\(\(first_raw \+ \" \" \+ last_raw\)\.strip\(\)\)\n"
         r"    if full:\n"
         r"        return full\n"
         r"    username_raw = getattr\(user, \"username\", None\) or \"\"\n",
         "    username_raw = getattr(user, \"username\", None) or \"\"\n"
         "    if username_raw:\n"
         "        return \"@\" + username_raw\n"
         "    full = _safe_sender_label((first_raw + \" \" + last_raw).strip())\n"
         "    if full:\n"
         "        return full\n", 1),
    ]),
    ("the last name is dropped", "CAUGHT", [
        (SENDER,
         r"    last_raw = getattr\(user, \"last_name\", None\) or \"\"\n",
         "    last_raw = \"\"\n", 1),
    ]),
    ("the absorb path resolves the name itself again", "CAUGHT", [
        (ABSORB,
         r"    return display_name\(user\) or \"unknown\"\n",
         "    username = getattr(user, \"username\", None)\n"
         "    if username:\n"
         "        return f\"@{username}\"\n"
         "    return display_name(user) or \"unknown\"\n", 1),
    ]),
    ("the module that uses the policy grows its own copy", "CAUGHT", [
        (HANDLERS,
         r"from sender_name import display_name as _sender_display_name\n",
         "from sender_name import display_name as _policy_display_name\n"
         "\n"
         "\n"
         "def _sender_display_name(user):\n"
         "    \"\"\"A second copy of the naming policy.\"\"\"\n"
         "    username = getattr(user, \"username\", None)\n"
         "    if username:\n"
         "        return f\"@{username}\"\n"
         "    return _policy_display_name(user)\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires are gone, and the second resolver is back",
     "SURVIVED", [
        (TEST_ONE_NAME, *_silence(*TRIPWIRES[0]), 1),
        (TEST_ONE_NAME, *_silence(*TRIPWIRES[1]), 1),
        (TEST_ONE_NAME, *_silence(*TRIPWIRES[2]), 1),
        (TEST_ONE_NAME, *_silence(*TRIPWIRES[3]), 1),
        (TEST_ONE_NAME, *_silence(*TRIPWIRES[4]), 1),
        (TEST_ONE_NAME, *_silence(*TRIPWIRES[5]), 1),
        (ABSORB,
         r"    return display_name\(user\) or \"unknown\"\n",
         "    username = getattr(user, \"username\", None)\n"
         "    if username:\n"
         "        return f\"@{username}\"\n"
         "    return display_name(user) or \"unknown\"\n", 1),
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
