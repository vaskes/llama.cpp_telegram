#!/usr/bin/env python3
"""Mutation harness for "the record holds a message once" (2026-10-27).

The operator's record is the source of truth -- it is kept for a LoRA
dataset -- and on 2026-10-27 it was measured to hold one message twice:
the operator's "Удали сообщение 1" (Telegram id 3379) sat in rows 10690
and 10693, under two different names for the same person. The second row
was written by the group-absorb path, which stores the message a human
replied to (so that "the message where he said X" is findable at all) and
stored it without asking whether the record already held it.

**Group A -- the behaviour.** Each mutation puts one of the defects back,
and the suite has to notice:

  * the record is asked, and its answer is discarded;
  * the message is swallowed whatever the record says;
  * the record is asked about the wrong message;
  * the lookup ignores the id it was given;
  * a lookup that fails swallows the message.

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

    python3 scripts/mutation_harness_record_once.py [ROOT]
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
    "tests/test_record_message_once.py",
    "tests/test_telegram_id_recorded.py",
    "tests/test_t65_reply_to_human.py",
    "tests/test_dispatch_group_seam.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

ABSORB = ROOT / "telegram-bot" / "dispatch_group.py"
PERSISTENCE = ROOT / "telegram-bot" / "persistence.py"
TEST_ONCE = ROOT / "tests" / "test_record_message_once.py"

# The six tripwires of this rule, silenced one by one in group B.
TRIPWIRES = [
    "test_the_absorb_path_does_not_store_a_message_the_record_already_holds",
    "test_a_message_the_record_does_not_hold_is_still_stored",
    "test_the_check_is_by_message_id_and_not_by_text",
    "test_a_failed_lookup_does_not_swallow_the_message",
    "test_the_absorb_path_reads_the_record_through_persistence",
    "test_the_lookup_is_asked_about_the_message_that_was_replied_to",
]

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the answer of the record is discarded", "CAUGHT", [
        (ABSORB,
         r"                recorded = await _already_recorded\(chat_id, rtm\.message_id\)\n",
         "                await _already_recorded(chat_id, rtm.message_id)\n"
         "                recorded = False\n", 1),
    ]),
    ("the message is swallowed whatever the record says", "CAUGHT", [
        (ABSORB,
         r"                recorded = await _already_recorded\(chat_id, rtm\.message_id\)\n",
         "                await _already_recorded(chat_id, rtm.message_id)\n"
         "                recorded = True\n", 1),
    ]),
    ("the record is asked about the wrong message", "CAUGHT", [
        (ABSORB,
         r"                recorded = await _already_recorded\(chat_id, rtm\.message_id\)\n",
         "                recorded = await _already_recorded(chat_id, msg.message_id)\n", 1),
    ]),
    ("the lookup ignores the id it was given", "CAUGHT", [
        (PERSISTENCE,
         r"        storage\.get_store\(\)\.search_messages, chat_id,\n"
         r"        ids=\[telegram_message_id\],\n",
         "        storage.get_store().search_messages, chat_id,\n"
         "        ids=[],\n", 1),
    ]),
    ("a lookup that fails swallows the message", "CAUGHT", [
        (ABSORB,
         r"            except Exception as exc:\n"
         r"                log\.info\(f\"\[dispatch\] record lookup of the replied-to \"\n"
         r"                    f\"message failed: \{exc!r\}\"\)\n",
         "            except Exception as exc:\n"
         "                log.info(f\"[dispatch] record lookup of the replied-to \"\n"
         "                    f\"message failed: {exc!r}\")\n"
         "                recorded = True\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires are gone, and the message is stored twice again",
     "SURVIVED", [
         (TEST_ONCE,
          r"(?m)^def test_the_absorb_path_does_not_store_a_message_the_record_already_holds\(\n"
          r"        record, written\):\n",
          "def test_the_absorb_path_does_not_store_a_message_the_record_already_holds(\n"
          "        record, written):\n    return\n", 1),
         (TEST_ONCE,
          r"(?m)^def test_a_message_the_record_does_not_hold_is_still_stored\(record, written\):\n",
          "def test_a_message_the_record_does_not_hold_is_still_stored(record, written):\n"
          "    return\n", 1),
         (TEST_ONCE,
          r"(?m)^def test_the_check_is_by_message_id_and_not_by_text\(record, written\):\n",
          "def test_the_check_is_by_message_id_and_not_by_text(record, written):\n"
          "    return\n", 1),
         (TEST_ONCE,
          r"(?m)^def test_a_failed_lookup_does_not_swallow_the_message\(record, written,\n"
          r"                                                      monkeypatch\):\n",
          "def test_a_failed_lookup_does_not_swallow_the_message(record, written,\n"
          "                                                      monkeypatch):\n"
          "    return\n", 1),
         (TEST_ONCE,
          r"(?m)^def test_the_absorb_path_reads_the_record_through_persistence\(\):\n",
          "def test_the_absorb_path_reads_the_record_through_persistence():\n"
          "    return\n", 1),
         (TEST_ONCE,
          r"(?m)^def test_the_lookup_is_asked_about_the_message_that_was_replied_to\(\n"
          r"        record, written, monkeypatch\):\n",
          "def test_the_lookup_is_asked_about_the_message_that_was_replied_to(\n"
          "        record, written, monkeypatch):\n    return\n", 1),
         (ABSORB,
          r"                recorded = await _already_recorded\(chat_id, rtm\.message_id\)\n",
          "                await _already_recorded(chat_id, rtm.message_id)\n"
          "                recorded = False\n", 1),
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
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
