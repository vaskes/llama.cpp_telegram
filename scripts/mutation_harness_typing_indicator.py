#!/usr/bin/env python3
"""Mutation harness for "the typing indicator cannot kill the turn" (2026-10-27).

Production, 19:15 UTC: a voice message arrived and the turn died before
the model was ever asked. The traceback names the cause --
`telegram.error.NetworkError: httpx.ConnectError` -- raised by
`send_action`, the first thing the handler does. The next update was
served normally, so the hiccup was over by then: the message was lost to
a moment of network trouble, because the handler asked the API for a
decoration without a guard.

Three of the four handlers had no guard at all. The fourth had one copy
of it inline, which is where the copy comes from.

**Group A -- the behaviour.** Each mutation puts the defect back, and the
suite has to notice:

  * the helper loses its guard, so a refused indicator kills the turn;
  * the helper stops sending the indicator;
  * one handler goes back to calling Telegram itself, unguarded;
  * the helper swallows a cancellation as well as a network error.

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

    python3 scripts/mutation_harness_typing_indicator.py [ROOT]
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
    "tests/test_typing_indicator.py",
    "tests/test_slot_release.py",
    "tests/test_slot_release_finally.py",
    "tests/test_early_rollback.py",
    "tests/test_handler_skeleton.py",
    "tests/test_handlers.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

HANDLERS = ROOT / "telegram-bot" / "handlers.py"
TYPING_TEST = ROOT / "tests" / "test_typing_indicator.py"

#: The guard, verbatim. It comes back in group A (as the behaviour) and
#: in group B (as the behaviour the silenced tripwires would have
#: caught), and it is the only way the two groups differ.
GUARDED = """    try:
        await chat.send_action(action='typing')
    except Exception as e:
        log.warning(f"[{label}] send_action FAILED: {type(e).__name__}: {e!r}")
"""

UNGUARDED = """    await chat.send_action(action='typing')
"""

HELPER_CALL = "        await _typing_indicator(update.message.chat, label='handle_voice')\n"
DIRECT_CALL = "        await update.message.chat.send_action(action='typing')\n"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the helper loses its guard", "CAUGHT", [
        (HANDLERS, re.escape(GUARDED), UNGUARDED, 1),
    ]),
    ("the helper stops sending the indicator", "CAUGHT", [
        (HANDLERS,
         r"        await chat\.send_action\(action='typing'\)\n",
         "        pass\n", 1),
    ]),
    ("one handler talks to Telegram itself again", "CAUGHT", [
        (HANDLERS, re.escape(HELPER_CALL), DIRECT_CALL, 1),
    ]),
    ("the helper swallows a cancellation too", "CAUGHT", [
        (HANDLERS,
         r"    except Exception as e:\n"
         r"        log\.warning\(f\"\[\{label\}\] send_action FAILED: "
         r"\{type\(e\)\.__name__\}: \{e!r\}\"\)\n",
         "    except BaseException as e:\n"
         "        log.warning(f\"[{label}] send_action FAILED: "
         "{type(e).__name__}: {e!r}\")\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires are gone, and a refused indicator kills the turn again",
     "SURVIVED", [
         (TYPING_TEST,
          r"(?m)^async def test_the_turn_survives_a_refused_typing_indicator\(\n"
          r"        handler, monkeypatch, tmp_path\):\n",
          "async def test_the_turn_survives_a_refused_typing_indicator(\n"
          "        handler, monkeypatch, tmp_path):\n"
          "    return\n", 1),
         (TYPING_TEST,
          r"(?m)^async def test_a_refused_indicator_is_logged\(capsys\):\n",
          "async def test_a_refused_indicator_is_logged(capsys):\n"
          "    return\n", 1),
         (HANDLERS, re.escape(GUARDED), UNGUARDED, 1),
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
