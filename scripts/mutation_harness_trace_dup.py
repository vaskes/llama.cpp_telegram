#!/usr/bin/env python3
"""Mutation harness for the transcript that duplicated a delivered file.

Measured on production, 2026-10-25.  The operator asked for an HTML
file; the model wrote it with a heredoc, called `send_file`, and the
document arrived.  Then the chat got a second message: the transcript
of the heredoc -- the same HTML, twice over inside it, because a
heredoc command carries the whole file in its own text and the sandbox
answers with the pane that shows what was typed.

The rule the fix puts in place: a file that reached the chat takes the
transcript with it.  The transcript still goes to the chat when no file
was delivered, still stays in what the bot remembers, and a delivery
that FAILED does not count -- its output is then the only witness of
what the model wrote.

**Group A -- the behaviour.** Each mutation puts one of the old
defects back, and the suite has to notice:

  * the transcript is shown again after a delivered file;
  * a delivered file is not recognised as one;
  * a delivery that failed counts as a delivery;
  * the transcript is dropped from the memory as well as from the chat;
  * a file that came out of the model's own text counts as a delivered
    file (it must not: the transcript cannot be showing it twice).

**Group B -- the tripwires.** Each mutation removes a tripwire *and*
the behaviour it is supposed to catch, and the suite has to go
**green**.  A green suite with the tripwire gone is the proof that
the tripwire was the thing doing the work.

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

    python3 scripts/mutation_harness_trace_dup.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tests that judge the rule.
TARGETS = [
    "tests/test_trace_not_duplicating_a_delivered_file.py",
    # The transcript itself, and the places that put it in front of an
    # answer.  They have to stay green: the fix changes WHEN the
    # transcript goes out, not WHAT it says.
    "tests/test_t44.py",
    "tests/test_llm_result.py",
    "tests/test_executed_tools_accumulate.py",
    "tests/test_artifact_e2e.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

RESULT = ROOT / "telegram-bot" / "llm_result.py"
TEST = ROOT / "tests" / "test_trace_not_duplicating_a_delivered_file.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the transcript is shown again after a delivered file", "CAUGHT", [
        (RESULT,
         r"            if not _delivered_a_file\(executed\):\n",
         "            if True:\n", 1),
    ]),
    ("a delivered file is not recognised as one", "CAUGHT", [
        (RESULT,
         r"        if entry\.get\(\"sha256\"\):\n",
         "        if False:\n", 1),
    ]),
    ("a delivery that failed counts as a delivery", "CAUGHT", [
        (RESULT,
         r"        if entry\.get\(\"sha256\"\):\n",
         "        if entry.get(\"name\") == \"send_file\":\n", 1),
    ]),
    ("the transcript is dropped from the memory too", "CAUGHT", [
        (RESULT,
         r"            raw = trace \+ \"\\n\\n\" \+ text\n",
         "            raw = text\n", 1),
    ]),
    ("a file out of the model's own text counts as a delivered file",
     "CAUGHT", [
        (RESULT,
         r"            if not _delivered_a_file\(executed\):\n",
         "            if not (_delivered_a_file(executed) or files):\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires are gone, and the file is delivered twice",
     "SURVIVED", [
        (TEST,
         r"(?m)^def test_the_transcript_is_kept_out_of_the_chat_when_a_file_was_delivered\(\):\n",
         "def test_the_transcript_is_kept_out_of_the_chat_when_a_file_was_delivered():\n    return\n", 1),
        (TEST,
         r"(?m)^def test_the_bot_still_remembers_the_transcript\(\):\n",
         "def test_the_bot_still_remembers_the_transcript():\n    return\n", 1),
        (TEST,
         r"(?m)^def test_the_measured_turn_delivers_the_file_once\(\):\n",
         "def test_the_measured_turn_delivers_the_file_once():\n    return\n", 1),
        (RESULT,
         r"            if not _delivered_a_file\(executed\):\n",
         "            if True:\n", 1),
    ]),
    ("the tripwire is gone, and a failed delivery hides the transcript",
     "SURVIVED", [
        (TEST,
         r"(?m)^def test_a_delivery_that_failed_keeps_the_transcript\(\):\n",
         "def test_a_delivery_that_failed_keeps_the_transcript():\n    return\n", 1),
        (RESULT,
         r"        if entry\.get\(\"sha256\"\):\n",
         "        if entry.get(\"name\") == \"send_file\":\n", 1),
    ]),
    ("the tripwire is gone, and the memory loses the transcript",
     "SURVIVED", [
        (TEST,
         r"(?m)^def test_the_bot_still_remembers_the_transcript\(\):\n",
         "def test_the_bot_still_remembers_the_transcript():\n    return\n", 1),
        # The memory half of the rule was already guarded: this tripwire
        # has been there since the trace moved into `raw`.  Both have to
        # go for the defect to be invisible -- one of them catching it
        # is enough to keep the suite red.
        (ROOT / "tests" / "test_llm_result.py",
         r"(?m)^def test_the_remembered_answer_carries_the_tool_trace\(\):\n"
         r"    \"\"\"The model has always been shown its own tool trace, and the\n"
         r"    history it gets is the same text\.\"\"\"\n",
         "def test_the_remembered_answer_carries_the_tool_trace():\n"
         "    \"\"\"The model has always been shown its own tool trace, and the\n"
         "    history it gets is the same text.\"\"\"\n    return\n", 1),
        (RESULT,
         r"            raw = trace \+ \"\\n\\n\" \+ text\n",
         "            raw = text\n", 1),
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
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
