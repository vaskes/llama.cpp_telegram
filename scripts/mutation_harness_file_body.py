#!/usr/bin/env python3
"""Mutation harness for the file door (2026-11-01).

A file that lives in the sandbox is READ by the model through the MCP
file door. What it was before this wave: the file never arrived.

Three things of the bot's own doing stood in the way, and each of them
is a mutation here:

  * the session detector read the ANSWER's text, found the words
    "session", "unknown" and "expired" IN THE FILE, called the session
    forgotten and killed the door on a read that had succeeded;
  * the registry cut every tool result to 24 000 characters;
  * the iteration cut it again to 12 000, dropping the middle -- so
    even a file that got through arrived without its tail.

The file door is a door of its own: the server's read budget, not the
bot's tool budget, and the bot checks that the text it holds encodes
to exactly the number of bytes the server declared.

**Group A -- the behaviour.** Each mutation puts one of the defects
back, and the tripwires have to notice:

  * the detector reads the answer's content again;
  * the registry cuts a whole file at the tool budget;
  * the iteration drops the middle of a whole file;
  * the ledger keeps a narrower window than the model saw;
  * the client stops counting the bytes (a piece announced as a whole);
  * the client stops recognising a file at all.

**Group B -- the tripwires.** Each mutation removes the tripwires that
caught the defect *and* puts the defect back, and the suite has to go
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
    and the CODEBOOK's table quotes how many tests it has.

    python3 scripts/mutation_harness_file_body.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
#: The image the nightly job built for the harnesses. Empty
#: means "run on this interpreter" -- the local way.
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tripwires that carry the rule. The suite that judges the
#: mutations is this file alone -- the point is to measure what the
#: tripwire does, not what the rest of the repository does.
TARGETS = ["tests/test_mcp_file_body.py"]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

CLIENT = ROOT / "telegram-bot" / "mcp_client.py"
REGISTRY = ROOT / "telegram-bot" / "mcp_registry.py"
DISPATCH = ROOT / "telegram-bot" / "llm_dispatch.py"
ITERATION = ROOT / "telegram-bot" / "llm_iteration.py"
TEST_FILE = ROOT / "tests" / "test_mcp_file_body.py"

#: The def line of every tripwire, exactly as the file has it. A group
#: B mutation replaces the line with itself and a `return` under it: the
#: test stays in the file -- the CODEBOOK quotes how many tests each
#: file has, and a renamed test leaves the collection -- and stops
#: testing anything.
SILENCE = {
    "mentions": ("async def test_a_result_that_mentions_a_session_is_not_a_"
                 "forgotten_session(\n        monkeypatch):"),
    "forgotten": "async def test_a_404_still_means_a_forgotten_session(monkeypatch):",
    "envelope": ("async def test_a_jsonrpc_error_that_names_the_session_is_"
                 "retried(monkeypatch):"),
    "declared": ("async def test_the_declared_size_decides_whether_we_hold_"
                 "the_whole_file(\n        monkeypatch):"),
    "binary": "async def test_a_binary_file_is_still_not_inlined(monkeypatch):",
    "cut": "async def test_the_registry_does_not_cut_a_whole_file(monkeypatch):",
    "capped": "async def test_an_ordinary_result_is_still_capped(monkeypatch):",
    "complete": "async def test_the_model_is_told_the_file_is_complete(monkeypatch):",
    "dispatch": "async def test_the_dispatch_hands_the_model_the_whole_file(monkeypatch):",
    "quote": ("async def test_a_quote_from_the_middle_of_a_file_is_not_a_"
              "deliverable(\n        monkeypatch):"),
    "iteration": "def test_the_iteration_shows_the_model_the_whole_file():",
    "middle": "def test_an_ordinary_result_is_still_cut_for_the_model():",
}


def _silence(key):
    """The op that turns one tripwire off."""
    line = SILENCE[key]
    return (TEST_FILE, re.escape(line) + "\n",
            line + "\n    return\n", 1)


#: Which tests each defect turns red. MEASURED, not guessed: every
#: mutation below was applied to the tree and the red tests were read
#: off the run (2026-11-01). A group B mutation silences exactly those
#: tests, so "the tripwire is gone" means what it says -- and a
#: tripwire nobody silences would make the group B proof a fiction.
CAUGHT_BY = {
    "sniff": ["mentions"],
    "cut": ["complete", "cut", "dispatch", "quote"],
    "middle": ["iteration"],
    "ledger": ["dispatch", "quote"],
    "nocheck": ["binary", "declared"],
    "nofile": ["complete", "cut", "declared", "dispatch", "mentions", "quote"],
}


def _group_b(keys, behaviour_op):
    """The tripwires that were doing the work, off, and the defect back."""
    return [_silence(key) for key in keys] + [behaviour_op]


#: The behaviour mutations -- the defects this wave put back.
BEHAVIOUR = {
    # The detector reads the answer again: the file's own words about
    # sessions are taken for the wire's words.
    "sniff": (CLIENT,
              r"        if \"error\" in data and _names_a_forgotten_session\(\n"
              r"                json\.dumps\(data\[\"error\"\], ensure_ascii=False\)\):\n",
              "        if _names_a_forgotten_session(text) or (\n"
              "                \"error\" in data and _names_a_forgotten_session(\n"
              "                    json.dumps(data[\"error\"],\n"
              "                              ensure_ascii=False))):\n",
              1),
    # The registry cuts a whole file at the ordinary tool budget.
    "cut": (REGISTRY,
            r"    if file is not None:\n        limit = MAX_FILE_RESULT_CHARS\n",
            "    if file is not None:\n        limit = MAX_RESULT_CHARS\n",
            1),
    # The iteration drops the middle of the file.
    "middle": (ITERATION,
               r"    if getattr\(outcome, \"file\", None\) is not None:\n"
               r"        return outcome\.text\n",
               "    if False:\n        return outcome.text\n",
               1),
    # The ledger keeps a narrower window than the model saw.
    "ledger": (DISPATCH,
               r"        ledger\[\"output\"\] = text\n",
               "        ledger[\"output\"] = _ledger_output(text)\n",
               1),
    # The client stops counting the bytes: a piece is announced as the
    # whole file.
    "nocheck": (CLIENT,
                r"    if len\(text\.encode\(\"utf-8\"\)\) != size:\n"
                r"        return None\n",
                "", 1),
    # The client stops recognising a file at all.
    "nofile": (CLIENT,
               r"        return text, _file_facts\(structured, text\)\n",
               "        return text, None\n", 1),
}

#: The mutations themselves: (name, expectation, ops).
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the session detector reads the answer's content again", "CAUGHT",
     [BEHAVIOUR["sniff"]]),
    ("the registry cuts a whole file at the tool budget", "CAUGHT",
     [BEHAVIOUR["cut"]]),
    ("the iteration drops the middle of a whole file", "CAUGHT",
     [BEHAVIOUR["middle"]]),
    ("the ledger keeps less than the model saw", "CAUGHT",
     [BEHAVIOUR["ledger"]]),
    ("the client stops counting the bytes", "CAUGHT",
     [BEHAVIOUR["nocheck"]]),
    ("the client stops recognising a file", "CAUGHT",
     [BEHAVIOUR["nofile"]]),

    # --- group B: the tripwires -------------------------------------
    ("the tripwire is gone, and the detector reads the answer again",
     "SURVIVED", _group_b(CAUGHT_BY["sniff"], BEHAVIOUR["sniff"])),
    ("the tripwires are gone, and the registry cuts the file",
     "SURVIVED", _group_b(CAUGHT_BY["cut"], BEHAVIOUR["cut"])),
    ("the tripwire is gone, and the iteration drops the middle",
     "SURVIVED", _group_b(CAUGHT_BY["middle"], BEHAVIOUR["middle"])),
    ("the tripwires are gone, and the ledger keeps less",
     "SURVIVED", _group_b(CAUGHT_BY["ledger"], BEHAVIOUR["ledger"])),
    ("the tripwires are gone, and the bytes are not counted",
     "SURVIVED", _group_b(CAUGHT_BY["nocheck"], BEHAVIOUR["nocheck"])),
    ("the tripwires are gone, and no file is recognised",
     "SURVIVED", _group_b(CAUGHT_BY["nofile"], BEHAVIOUR["nofile"])),
]


def _command():
    """The suite runs in the image, when the nightly job gave us one.

    The nightly job builds the bot's own image and hands its name in
    `MUTATION_HARNESS_IMAGE`, because the harnesses have to run in the
    environment the code ships into -- the image the bot runs in, with
    its dependencies, not the runner's own Python.

    A harness that runs on the runner's Python instead measures
    something else: the runner has no pytest, so the suite does not
    even start, the preflight reads "(no summary)", and the job goes
    red on a night this harness was supposed to measure. That is what
    happened to this harness on 2026-11-07 (mutation-nightly, the
    document channel's night) -- and it would happen to every night
    this harness is picked.
    """
    if IMAGE:
        return ["docker", "run", "--rm", "-v", f"{ROOT}:/w", "-w", "/w",
                "-e", "CONVERSATIONS_DB=/tmp/mut.db",
                "-e", "ALLOWED_USER_IDS=1",
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
    import os
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(_command(), cwd=str(ROOT), capture_output=True,
                              text=True, timeout=SUITE_TIMEOUT, env=env)
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
    verdict, summary, who = run_tests()
    print("  ", summary, flush=True)
    if verdict != "pass":
        # The names, not just the count. A preflight that says "2 failed"
        # and nothing else sends the reader to the tree; one that says
        # WHICH two sent them to the test. Measured on 2026-11-12: the
        # mutation-nightly job went red with "2 failed, 73 passed" and
        # nothing to look at -- the log had no way to name the two.
        for name in who:
            print(f"    RED: {name}", flush=True)
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
