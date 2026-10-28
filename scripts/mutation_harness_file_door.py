#!/usr/bin/env python3
"""Mutation harness for the file door and for the marked cuts (2026-10-25).

Two claims are on trial here, and they are the two halves of the change
that moved a file out of the sandbox through an HTTP door instead of
through base64 in a terminal pane.

**Group A -- the behaviour.** Each mutation puts the defect back, and
the suite has to notice:

  * the file is not pulled through the door (the document carries
    nothing);
  * the size is not known before the bytes are pulled (a file nobody
    could deliver is downloaded anyway);
  * the channel's own limit is not checked;
  * the temp file of a pull is left in the bot's container;
  * the tool result the model reads is cut without a word;
  * the ledger goes back to keeping the tail, and the sandbox's marker
    about a lost beginning goes with it;
  * the transcript is cut without a word;
  * the command output the model reads is cut without a word;
  * the side-car's version is not asked, so a 404 from a side-car that
    has never heard of a file door is reported as a missing file.

**Group B -- the tripwires.** Each mutation removes the tripwire *and*
the behaviour it is supposed to catch, and the suite has to go
**green**. A green suite with the tripwire gone is the proof that the
tripwire was the thing doing the work: without it, the defect is
invisible. This is the direction a one-way harness never checks, and it
is the one that turns "the suite is green" into "the suite is green
*because of this test*".

**Retired on 2026-10-27.** Two mutations here used to measure "the
sandbox copy is deleted after the delivery". The operator reversed that
rule the same day -- a delivery never removes what it was not told to
remove -- so the code those mutations pointed at is gone, and a mutation
whose site no longer exists is a REFUSED one, forever. The rule as it
stands now is measured by `scripts/mutation_harness_send_file_copy.py`,
which puts the deletion back and watches the new tripwires catch it.

The rules this harness holds itself to, because a mutation run that
cannot report INVALID is a decoration:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the name of the test that went red is printed, because "the suite
    went red" and "the test written for this rule went red" are
    different claims;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte.

    python3 scripts/mutation_harness_file_door.py [ROOT]
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
    "tests/test_send_file_tool.py",
    "tests/test_file_transport.py",
    "tests/test_truncation_markers.py",
    "tests/test_console_output_in_chat.py",
    "tests/test_d4_dispatch.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

DISPATCH = ROOT / "telegram-bot" / "llm_dispatch.py"
#: The cleanup of the fetched temp file moved here when the delivery
#: became a step of its own: the tool stages the bytes, the handler
#: sends them with the answer, and the shelf drops what nobody came
#: back for. The anchor still pointed at the old cleanup in the
#: dispatcher, found nothing there, and the mutation was REFUSED.
FILES = ROOT / "telegram-bot" / "llm_files.py"
TRANSPORT = ROOT / "telegram-bot" / "personal_tools" / "file_transport.py"
ITERATION = ROOT / "telegram-bot" / "llm_iteration.py"
RESULT = ROOT / "telegram-bot" / "llm_result.py"
BASH = ROOT / "telegram-bot" / "personal_tools" / "bash_command.py"

TEST_SEND = ROOT / "tests" / "test_send_file_tool.py"
TEST_TRANSPORT = ROOT / "tests" / "test_file_transport.py"
TEST_MARKERS = ROOT / "tests" / "test_truncation_markers.py"
TEST_CONSOLE = ROOT / "tests" / "test_console_output_in_chat.py"
TEST_D4 = ROOT / "tests" / "test_d4_dispatch.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the file is not pulled through the door", "CAUGHT", [
        (TRANSPORT,
         r"                    async for chunk in r\.aiter_bytes\(\):\n",
         "                    async for chunk in ():\n", 1),
    ]),
    ("the size is not known before the bytes are pulled", "CAUGHT", [
        (DISPATCH,
         r"    if limit and size > limit:\n"
         r"        return ToolExecution\(text=\(\n"
         r"            f\"\[send_file: \{name\} is \{size\} bytes; the sandbox allows \"\n"
         r"            f\"\{limit\} bytes\]\"\)\)\n",
         "", 1),
    ]),
    ("the channel's own limit is not checked", "CAUGHT", [
        (DISPATCH,
         r"    if size > MAX_SEND_FILE_BYTES:\n"
         r"        return ToolExecution\(text=\(\n"
         r"            f\"\[send_file: \{name\} is \{size\} bytes; Telegram's sendDocument \"\n"
         r"            f\"accepts at most \{MAX_SEND_FILE_BYTES\} bytes\]\"\)\)\n",
         "", 1),
    ]),
    ("the temp file of a pull is left behind", "CAUGHT", [
        # The cleanup of the pulled temp file is the delivery's job now:
        # the bytes have to outlive the tool call (they ARE the file the
        # operator gets), so they are removed when the answer goes out.
        # The anchor pointed at the old cleanup in the dispatcher, which
        # was removed when the delivery became a step of its own -- the
        # mutation found nothing there and was REFUSED.
        (FILES,
         r"    finally:\n"
         r"        try:\n"
         r"            import os\n"
         r"\n"
         r"            os\.unlink\(item\.path\)\n"
         r"        except OSError:\n"
         r"            pass\n",
         "    finally:\n        pass\n", 1),
    ]),
    ("the tool result the model reads is cut without a word", "CAUGHT", [
        (ITERATION,
         r"'content': condense\(outcome\.text, MAX_TOOL_RESULT_CHARS\),",
         "'content': outcome.text[:MAX_TOOL_RESULT_CHARS],", 1),
    ]),
    ("the ledger keeps the tail, and the sandbox's marker with it", "CAUGHT", [
        (DISPATCH,
         r"    return condense\(stdout, MAX_LEDGER_STDOUT_CHARS\)\n",
         "    return stdout[-MAX_LEDGER_STDOUT_CHARS:]\n", 1),
    ]),
    ("the transcript is cut without a word", "CAUGHT", [
        (RESULT,
         r"            out = condense\(out, MAX_TRANSCRIPT_OUTPUT_CHARS\)\n",
         "            out = out[-MAX_TRANSCRIPT_OUTPUT_CHARS:]\n", 1),
    ]),
    ("the side-car's version is not asked", "CAUGHT", [
        (TRANSPORT,
         r"            version = await _sidecar_version\(timeout=timeout\)\n",
         "            version = None\n", 1),
    ]),
    ("the command output the model reads is cut without a word", "CAUGHT", [
        (BASH,
         r"        parts\.append\(f\"stdout:\\n\{condense\(out, 8000\)\}\"\)\n",
         "        parts.append(f\"stdout:\\n{out[-8000:]}\")\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires for the door are gone, and so is the door", "SURVIVED", [
        # The whole file goes: the door is guarded by several tests, and
        # a group-B mutation that disables one of them and leaves the
        # others in place proves nothing -- the suite would go red for
        # the wrong reason.
        (TEST_SEND, r"(?s)\A.*\Z", "# the tripwires are gone\n", 1),
        (TEST_TRANSPORT, r"(?s)\A.*\Z", "# the tripwires are gone\n", 1),
        (TRANSPORT,
         r"                    async for chunk in r\.aiter_bytes\(\):\n",
         "                    async for chunk in ():\n", 1),
    ]),
    ("the tripwire for the size is gone, and so is the size check",
     "SURVIVED", [
        (TEST_SEND,
         r"async def test_the_size_is_known_before_the_bytes_are_pulled\(sandbox\):",
         "async def _disabled_test_the_size_is_known_before_the_bytes_are_pulled(sandbox):", 1),
        # The refusal text is pinned by a second test, and that test
        # guards the same branch. A group-B mutation has to silence every
        # tripwire of the behaviour it removes, or the suite stays red and
        # the verdict measures a neighbour instead of the proof.
        (TEST_SEND,
         r"async def test_the_refusal_of_an_oversized_file_is_pinned_verbatim\(sandbox\):",
         "async def _disabled_test_the_refusal_of_an_oversized_file_is_pinned_verbatim(sandbox):", 1),
        (DISPATCH,
         r"    if limit and size > limit:\n"
         r"        return ToolExecution\(text=\(\n"
         r"            f\"\[send_file: \{name\} is \{size\} bytes; the sandbox allows \"\n"
         r"            f\"\{limit\} bytes\]\"\)\)\n",
         "", 1),
    ]),
    ("the tripwire for the channel limit is gone, and so is the check",
     "SURVIVED", [
        (TEST_SEND,
         r"async def test_a_file_over_the_channel_limit_is_refused\(sandbox, monkeypatch\):",
         "async def _disabled_test_a_file_over_the_channel_limit_is_refused(sandbox, monkeypatch):", 1),
        (DISPATCH,
         r"    if size > MAX_SEND_FILE_BYTES:\n"
         r"        return ToolExecution\(text=\(\n"
         r"            f\"\[send_file: \{name\} is \{size\} bytes; Telegram's sendDocument \"\n"
         r"            f\"accepts at most \{MAX_SEND_FILE_BYTES\} bytes\]\"\)\)\n",
         "", 1),
    ]),
    ("the tripwire for the version is gone, and so is the probe", "SURVIVED", [
        (TEST_TRANSPORT,
         r"async def test_a_side_car_without_the_file_door_says_so\(sandbox\):",
         "async def _disabled_test_a_side_car_without_the_file_door_says_so(sandbox):", 1),
        (TRANSPORT,
         r"            version = await _sidecar_version\(timeout=timeout\)\n",
         "            version = None\n", 1),
    ]),
    ("the tripwire for the temp file is gone, and so is the cleanup",
     "SURVIVED", [
        (TEST_SEND,
         r"async def test_the_temp_file_is_removed_after_the_delivery\(sandbox, tmp_path\):",
         "async def _disabled_test_the_temp_file_is_removed_after_the_delivery(sandbox, tmp_path):", 1),
        (FILES,
         r"    finally:\n"
         r"        try:\n"
         r"            import os\n"
         r"\n"
         r"            os\.unlink\(item\.path\)\n"
         r"        except OSError:\n"
         r"            pass\n",
         "    finally:\n        pass\n", 1),
    ]),
    ("the tripwires for the marked tool result are gone, and so is the mark",
     "SURVIVED", [
        (TEST_MARKERS,
         r"async def test_the_tool_result_the_model_reads_is_marked\(monkeypatch\):",
         "async def _disabled_test_the_tool_result_the_model_reads_is_marked(monkeypatch):", 1),
        (TEST_D4,
         r"def test_the_result_cap_is_applied_by_the_caller\(\):",
         "def _disabled_test_the_result_cap_is_applied_by_the_caller():", 1),
        (ITERATION,
         r"'content': condense\(outcome\.text, MAX_TOOL_RESULT_CHARS\),",
         "'content': outcome.text[:MAX_TOOL_RESULT_CHARS],", 1),
    ]),
    ("the tripwires for the ledger are gone, and so is the mark",
     "SURVIVED", [
        (TEST_MARKERS,
         r"def test_the_ledger_keeps_both_ends\(\):",
         "def _disabled_test_the_ledger_keeps_both_ends():", 1),
        (TEST_MARKERS,
         r"def test_the_sandbox_head_marker_survives_the_ledger_cut\(\):",
         "def _disabled_test_the_sandbox_head_marker_survives_the_ledger_cut():", 1),
        (TEST_MARKERS,
         r"async def test_the_ledger_stdout_is_marked\(monkeypatch\):",
         "async def _disabled_test_the_ledger_stdout_is_marked(monkeypatch):", 1),
        (TEST_CONSOLE,
         r"async def test_the_ledger_does_not_carry_the_whole_output\(monkeypatch\):",
         "async def _disabled_test_the_ledger_does_not_carry_the_whole_output(monkeypatch):", 1),
        (TEST_CONSOLE,
         r"def test_the_transcript_shows_both_ends_of_the_output\(\):",
         "def _disabled_test_the_transcript_shows_both_ends_of_the_output():", 1),
        (DISPATCH,
         r"    return condense\(stdout, MAX_LEDGER_STDOUT_CHARS\)\n",
         "    return stdout[-MAX_LEDGER_STDOUT_CHARS:]\n", 1),
    ]),
    ("the tripwires for the marked transcript are gone, and so is the mark",
     "SURVIVED", [
        (TEST_MARKERS,
         r"def test_the_transcript_is_marked\(\):",
         "def _disabled_test_the_transcript_is_marked():", 1),
        (TEST_CONSOLE,
         r"def test_the_transcript_shows_both_ends_of_the_output\(\):",
         "def _disabled_test_the_transcript_shows_both_ends_of_the_output():", 1),
        (RESULT,
         r"            out = condense\(out, MAX_TRANSCRIPT_OUTPUT_CHARS\)\n",
         "            out = out[-MAX_TRANSCRIPT_OUTPUT_CHARS:]\n", 1),
    ]),
    ("the tripwire for the marked model view is gone, and so is the mark",
     "SURVIVED", [
        (TEST_MARKERS,
         r"def test_the_model_view_of_the_output_is_marked\(\):",
         "def _disabled_test_the_model_view_of_the_output_is_marked():", 1),
        (BASH,
         r"        parts\.append\(f\"stdout:\\n\{condense\(out, 8000\)\}\"\)\n",
         "        parts.append(f\"stdout:\\n{out[-8000:]}\")\n", 1),
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
                # op: two renames in one file would leave only the last
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
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
