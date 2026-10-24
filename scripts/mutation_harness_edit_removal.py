#!/usr/bin/env python3
"""Mutation harness for the removal of the edit-replace feature (2026-10-24).

A feature that is removed is only removed if the removal is *pinned*.
Otherwise the next person who finds the gap -- the wrong answer still on
screen, the user who has to wait for it -- re-adds it, and they will do
it with less understanding than the person who removed it.

So this harness breaks the removal on purpose, in two directions.

**Group A -- the behaviour.** Each mutation puts the feature (or part of
it) back, and the suite has to notice:

  * the subscription asks Telegram for edits again;
  * the Stop button's update type is dropped from the subscription;
  * the guard that ignores an edited message is deleted;
  * the guard never fires;
  * the guard drops every message, not just the edits;
  * the write-only `_bot_replies` map comes back;
  * the reply path tracks bot replies again.

**Group B -- the tripwires.** Each mutation removes a tripwire *and* the
behaviour it is supposed to catch, and the suite has to go **green**. A
green suite with the tripwire gone is the proof that the tripwire was
the thing doing the work: without it, the defect is invisible. This is
the direction that a one-way harness never checks, and it is the one
that turns "the suite is green" into "the suite is green *because of
this test*".

The rules this harness holds itself to, because a mutation run that
cannot report INVALID is a decoration:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte.

Where it runs: with the current interpreter by default, so it works in
a checkout. Set MUTATION_HARNESS_IMAGE to run the same tests inside the
test image instead.

    python3 scripts/mutation_harness_edit_removal.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The files that carry the removal's rules, and the ones that catch a
#: resurrection of it. Everything the harness needs to see is in here:
#: a mutation that no target can notice is a mutation that proves
#: nothing, so the target list has to cover the whole claim.
TARGETS = [
    "tests/test_edit_removal.py",
    # The subscription is guarded twice -- once here, once in the
    # late-import policy file. Group B removes both, so both are in the
    # target list.
    "tests/test_late_import_policy.py",
    # The routing tripwire: one entry point, one de_json.
    "tests/test_dispatch_routing.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

CONFIG = ROOT / "telegram-bot" / "config.py"
DISPATCH = ROOT / "telegram-bot" / "dispatch.py"
STATE = ROOT / "telegram-bot" / "state.py"
HANDLERS = ROOT / "telegram-bot" / "handlers.py"
TEST_REMOVAL = ROOT / "tests" / "test_edit_removal.py"
TEST_LATE = ROOT / "tests" / "test_late_import_policy.py"

# The guard as it is written today. Group A breaks it in three ways;
# group B removes it together with the test that catches it.
GUARD = """    if upd.message is None and upd.edited_message is not None:
        log.info(
            f"[dispatch] ignoring edited_message update_id={upd.update_id} "
            f"(the edit-replace feature was removed on 2026-10-24)")
        return
"""

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the subscription asks Telegram for edits again", "CAUGHT", [
        (CONFIG,
         r'ALLOWED_UPDATES = \("message", "callback_query", "chat_member"\)\n',
         'ALLOWED_UPDATES = ("message", "edited_message", "callback_query",\n'
         '                   "chat_member")\n', 1),
    ]),
    ("the Stop button's update type leaves the subscription", "CAUGHT", [
        (CONFIG,
         r'ALLOWED_UPDATES = \("message", "callback_query", "chat_member"\)\n',
         'ALLOWED_UPDATES = ("message", "chat_member")\n', 1),
    ]),
    ("the guard that ignores an edited message is deleted", "CAUGHT", [
        (DISPATCH, re.escape(GUARD), "", 1),
    ]),
    ("the guard never fires", "CAUGHT", [
        (DISPATCH,
         r"    if upd\.message is None and upd\.edited_message is not None:\n",
         "    if False and upd.edited_message is not None:\n", 1),
    ]),
    ("the guard drops every message, not just the edits", "CAUGHT", [
        (DISPATCH,
         r"    if upd\.message is None and upd\.edited_message is not None:\n",
         "    if True:\n", 1),
    ]),
    ("the write-only map comes back", "CAUGHT", [
        (STATE,
         r"# The `_bot_replies` LRU used to live here: \(chat_id, user_message_id\)\n",
         "_bot_replies = {}\n\n\ndef _register_bot_reply(chat_id, user_message_id, bot_message_id):\n"
         "    _bot_replies[(chat_id, user_message_id)] = bot_message_id\n\n\n"
         "# The `_bot_replies` LRU used to live here: (chat_id, user_message_id)\n", 1),
    ]),
    ("the reply path tracks bot replies again", "CAUGHT", [
        (HANDLERS,
         r"    await _reply\(update, text\)\n",
         "    await _reply(update, text)\n"
         "    try:\n"
         "        _register_bot_reply(1, 2, 3)\n"
         "    except Exception:\n"
         "        pass\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("both subscription tripwires are gone, and the edits come back",
     "SURVIVED", [
        (CONFIG,
         r'ALLOWED_UPDATES = \("message", "callback_query", "chat_member"\)\n',
         'ALLOWED_UPDATES = ("message", "edited_message", "callback_query",\n'
         '                   "chat_member")\n', 1),
        (TEST_REMOVAL, r"def test_the_subscription_does_not_ask_telegram_for_edits\(\):",
         "def _disabled_test_the_subscription_does_not_ask_telegram_for_edits():", 1),
        (TEST_LATE,
         r'    expected = \{"message", "callback_query", "chat_member"\}\n',
         '    expected = {"message", "edited_message", "callback_query",\n'
         '                "chat_member"}\n', 1),
    ]),
    ("the behavioural tripwire is gone, and the guard with it", "SURVIVED", [
        (DISPATCH, re.escape(GUARD), "", 1),
        (TEST_REMOVAL,
         r"async def test_an_edited_message_is_not_answered\(monkeypatch\):",
         "async def _disabled_test_an_edited_message_is_not_answered(monkeypatch):", 1),
    ]),
    ("the 'a normal message is still answered' tripwire is gone, and the "
     "guard drops everything", "SURVIVED", [
        (DISPATCH,
         r"    if upd\.message is None and upd\.edited_message is not None:\n",
         "    if True:\n", 1),
        (TEST_REMOVAL,
         r"async def test_a_normal_message_is_still_answered\(monkeypatch\):",
         "async def _disabled_test_a_normal_message_is_still_answered(monkeypatch):", 1),
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
                text = path.read_text(encoding="utf-8")
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
