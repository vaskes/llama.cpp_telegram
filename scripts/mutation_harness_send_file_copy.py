#!/usr/bin/env python3
"""Mutation harness for "the delivery removes nothing" (2026-10-27).

The operator asked the bot for a 50 MB binary. He got it -- and then
found the file gone from the sandbox. He had given no such order, and
the bot's answer had announced it as a fact: "the sandbox copy was
removed". The deletion was the tool's own decision (one copy, not two:
the workspace is a scratchpad), and the surprise landed on the user,
who had to ask who did it.

His ruling: a delivery never removes what it was not told to remove.

**Group A -- the behaviour.** Each mutation puts the defect back, and
the suite has to notice:

  * the delivery deletes the sandbox copy again;
  * the answer claims the copy was removed;
  * the answer says nothing about where the file is;
  * the delete capability comes back into the transport.

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
    and the CODEBOOK's table quotes how many tests it has.

    python3 scripts/mutation_harness_send_file_copy.py [ROOT]
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
    "tests/test_send_file_tool.py",
    "tests/test_file_transport.py",
    "tests/test_llm_files.py",
    "tests/test_tool_isolation.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

DISPATCH = ROOT / "telegram-bot" / "llm_dispatch.py"
TRANSPORT = ROOT / "telegram-bot" / "personal_tools" / "file_transport.py"
SEND_TEST = ROOT / "tests" / "test_send_file_tool.py"
TRANSPORT_TEST = ROOT / "tests" / "test_file_transport.py"

#: The old purge, verbatim. It comes back in group A (as the behaviour)
#: and in group B (as the behaviour the silenced tripwires would have
#: caught), and it is the only way the two groups differ.
OLD_PURGE = '''async def purge(path: str, *, timeout: float = 30.0) -> str:
    """Delete the sandbox copy, and say what happened."""
    url = f"{AGENT_STACK_URL}/files"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.delete(url, params={"path": path},
                                    headers=_headers())
    except httpx.TimeoutException:
        return "the sandbox copy could not be removed (the side-car timed out)"
    except httpx.HTTPError as e:
        return f"the sandbox copy could not be removed ({e!r})"
    if r.status_code == 204:
        return "the sandbox copy was removed"
    if r.status_code == 404:
        return "the sandbox copy was already gone"
    return ("the sandbox copy could not be removed ("
            f"{_error_for_status(r.status_code, r.text)})")


'''

OLD_CALL = '''    # 4. One copy. The file is in the chat; the scratch copy in the
    #    workspace is the one that goes.
    purge_note = await _ft.purge(path)
'''

OLD_TEXT = '''        text=(f"[send_file: sent {name} ({fetched.size} bytes, "
              f"sha256 {fetched.sha256}) to the chat; {purge_note}]"),
'''

NEW_CALL = '''    # 4. The sandbox copy stays. The delivery removes nothing on its
    #    own -- the operator's ruling of 2026-10-27. If the file is
    #    supposed to go, that is a request the model carries out through
    #    the shell, where the intent is explicit.
'''

NEW_TEXT = '''        text=(f"[send_file: sent {name} ({fetched.size} bytes, "
              f"sha256 {fetched.sha256}) to the chat; the sandbox copy is "
              f"still at {path}]"),
'''

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the delivery deletes the sandbox copy again", "CAUGHT", [
        (TRANSPORT,
         r"def _unlink\(path: str\) -> None:\n",
         OLD_PURGE + "def _unlink(path: str) -> None:\n", 1),
        (DISPATCH,
         r"    # 4\. The sandbox copy stays\. The delivery removes nothing on its\n"
         r"    #    own -- the operator's ruling of 2026-10-27\. If the file is\n"
         r"    #    supposed to go, that is a request the model carries out through\n"
         r"    #    the shell, where the intent is explicit\.\n",
         OLD_CALL, 1),
        (DISPATCH,
         r"        text=\(f\"\[send_file: staged \{name\} \(\{fetched\.size\} bytes, \"\n"
         r"              f\"sha256 \{fetched\.sha256\}\) for delivery with the answer; \"\n"
         r"              f\"the sandbox copy is still at \{path\}\]\"\),\n",
         OLD_TEXT, 1),
    ]),
    ("the answer claims the copy was removed", "CAUGHT", [
        (DISPATCH,
         r"              f\"sha256 \{fetched\.sha256\}\) for delivery with the answer; \"\n"
         r"              f\"the sandbox copy is still at \{path\}\]\"\),\n",
         "              f\"sha256 {fetched.sha256}) for delivery with the answer; \"\n"
         "              f\"the sandbox copy was removed]\"),\n", 1),
    ]),
    ("the answer says nothing about where the file is", "CAUGHT", [
        (DISPATCH,
         r"              f\"sha256 \{fetched\.sha256\}\) for delivery with the answer; \"\n"
         r"              f\"the sandbox copy is still at \{path\}\]\"\),\n",
         "              f\"sha256 {fetched.sha256}) for delivery with the answer]\"),\n", 1),
    ]),
    ("the delete capability comes back into the transport", "CAUGHT", [
        (TRANSPORT,
         r"def _unlink\(path: str\) -> None:\n",
         OLD_PURGE + "def _unlink(path: str) -> None:\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires are gone, and the delivery removes the copy again",
     "SURVIVED", [
         (SEND_TEST,
          r"(?m)^async def test_the_sandbox_copy_stays_in_the_sandbox\(sandbox\):\n",
          "async def test_the_sandbox_copy_stays_in_the_sandbox(sandbox):\n"
          "    return\n", 1),
         (SEND_TEST,
          r"(?m)^async def test_the_answer_does_not_claim_the_copy_was_removed\(sandbox\):\n",
          "async def test_the_answer_does_not_claim_the_copy_was_removed(sandbox):\n"
          "    return\n", 1),
         # The staging test catches the same mutation from the other
         # side: it asserts that the tool stages the file and says so
         # ("staged ...bin"). The old text says "sent", so the
         # assertion failed even though this mutation never removed
         # that test -- and a group-B mutation caught by a test it did
         # not remove is not a group-B mutation at all.
         (SEND_TEST,
          r"(?m)^async def test_the_tool_does_not_send_anything\(sandbox\):\n",
          "async def test_the_tool_does_not_send_anything(sandbox):\n"
          "    return\n", 1),
         (TRANSPORT_TEST,
          r"(?m)^def test_the_transport_cannot_delete_a_file\(\):\n",
          "def test_the_transport_cannot_delete_a_file():\n    return\n", 1),
         (TRANSPORT,
          r"def _unlink\(path: str\) -> None:\n",
          OLD_PURGE + "def _unlink(path: str) -> None:\n", 1),
         (DISPATCH,
          r"    # 4\. The sandbox copy stays\. The delivery removes nothing on its\n"
          r"    #    own -- the operator's ruling of 2026-10-27\. If the file is\n"
          r"    #    supposed to go, that is a request the model carries out through\n"
          r"    #    the shell, where the intent is explicit\.\n",
          OLD_CALL, 1),
         (DISPATCH,
          r"        text=\(f\"\[send_file: staged \{name\} \(\{fetched\.size\} bytes, \"\n"
          r"              f\"sha256 \{fetched\.sha256\}\) for delivery with the answer; \"\n"
          r"              f\"the sandbox copy is still at \{path\}\]\"\),\n",
          OLD_TEXT, 1),
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
