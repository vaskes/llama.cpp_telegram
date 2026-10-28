#!/usr/bin/env python3
"""Mutation harness for the answer splitting (T-FREEZE-CHUNK-1, 2026-10-24).

Telegram rejects a text message over 4096 characters. The bot answers in
4000-character pieces, and the rule is that **every** path does it: a
handler that sends the answer on its own is one long answer away from
`Message_too_long`, and the user sees the API error instead of the
answer.

A rule in code that no test can turn red is a rule that has stopped
being a rule. So this harness breaks the rule on purpose, in two
directions.

**Group A -- the behaviour.** Each mutation puts the defect back, and
the suite has to notice:

  * the answer is not split any more;
  * the piece is bigger than Telegram accepts;
  * the piece is trimmed at the boundary, and the words run together;
  * the photo, document, voice or text handler sends the answer on its
    own, without the split.

**Group B -- the tripwires.** Each mutation removes a tripwire *and* the
behaviour it is supposed to catch, and the suite has to go **green**. A
green suite with the tripwire gone is the proof that the tripwire was
the thing doing the work: without it, the defect is invisible. This is
the direction a one-way harness never checks, and it is the one that
turns "the suite is green" into "the suite is green *because of this
test*".

Two of the group-B mutations, because the two kinds of tripwire protect
different things:

  * the behavioural test (a long answer through the real handler) is
    the only thing that sees the answer arrive as one piece -- the
    structural tripwire cannot see the size of what was sent;
  * the structural tripwire is the only thing that sees the **voice**
    and **text** handlers bypass the helper, because those two paths
    have no behavioural test of their own.

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

Where it runs: with the current interpreter by default, so it works in
a checkout. Set MUTATION_HARNESS_IMAGE to run the same tests inside the
test image instead.

    python3 scripts/mutation_harness_chunking.py [ROOT]
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
#: that no target can notice is a mutation that proves nothing, so the
#: target list has to cover the whole claim.
TARGETS = [
    "tests/test_answer_chunking.py",
    # The structural tripwire in the handler-tail file: every handler
    # sends its answer through the helper.
    "tests/test_handler_tail.py",
    # The skeleton: the helper is a stage, called once per handler.
    "tests/test_handler_skeleton.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

HANDLERS = ROOT / "telegram-bot" / "handlers.py"
CHAT_UTILS = ROOT / "telegram-bot" / "chat_utils.py"
TEST_CHUNKING = ROOT / "tests" / "test_answer_chunking.py"
TEST_TAIL = ROOT / "tests" / "test_handler_tail.py"
TEST_SKELETON = ROOT / "tests" / "test_handler_skeleton.py"

# The helper as it is written today. Group A breaks it; group B removes
# it together with the tests that catch it.
#: The loop that splits the answer. The call grew a `parse_mode` and
#: the result is remembered now, so the old anchor -- which expected
#: the bare `await send_reply(...)` -- matched nothing: the mutation
#: was REFUSED, and a REFUSED mutation was counted as neither caught
#: nor survived. The behaviour is the same; the call is not.
SPLIT_LOOP = """    for chunk in chunks:
        # strip=False: a chunk trimmed at the boundary loses the space
        # between the last word of this chunk and the first word of the
        # next one.
        sent = await send_reply(update, chunk, strip=False, parse_mode="HTML")
"""

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the answer is not split any more", "CAUGHT", [
        (HANDLERS, re.escape(SPLIT_LOOP),
         "        await send_reply(update, text, strip=False, parse_mode=\"HTML\")\n", 1),
    ]),
    ("the piece is bigger than Telegram accepts", "CAUGHT", [
        (CHAT_UTILS, r"TELEGRAM_TEXT_CHUNK = 4000\n",
         "TELEGRAM_TEXT_CHUNK = 100000\n", 1),
    ]),
    ("the piece is trimmed at the boundary", "CAUGHT", [
        (HANDLERS,
         r"        sent = await send_reply\(update, chunk, strip=False, parse_mode=\"HTML\"\)\n",
         "        sent = await send_reply(update, chunk, parse_mode=\"HTML\")\n", 1),
    ]),
    ("the photo handler sends the answer on its own", "CAUGHT", [
        (HANDLERS,
         r"(?m)^        if bot_response:\n            await _send_answer_text\(update, bot_response\)\n",
         "        if bot_response:\n            await send_reply(update, bot_response)\n", 1),
    ]),
    ("the document handler sends the answer on its own", "CAUGHT", [
        (HANDLERS,
         r"(?m)^            if bot_response:\n"
         r"                await _send_answer_text\(update, bot_response\)\n"
         r"            # The files go out even when there is no prose: an answer that\n"
         r"            # is nothing but a document has no prose to send, and dropping\n"
         r"            # the file would leave the user with nothing at all\.\n"
         r"            await send_files\(update, answer\.files, thinking=thinking\)\n"
         r"        except Exception as e:\n"
         r"            log\.exception\(f\"\[ERR doc\]",
         "            if bot_response:\n"
         "                await send_reply(update, bot_response)\n"
         "            # The files go out even when there is no prose: an answer that\n"
         "            # is nothing but a document has no prose to send, and dropping\n"
         "            # the file would leave the user with nothing at all.\n"
         "            await send_files(update, answer.files, thinking=thinking)\n"
         "        except Exception as e:\n"
         "            log.exception(f\"[ERR doc]", 1),
    ]),
    ("the voice handler sends the answer on its own", "CAUGHT", [
        (HANDLERS,
         r"(?m)^        if bot_response:\n            # The answer may be longer than Telegram accepts in one\n"
         r"            # message; _send_answer_text splits it\. The rating path is\n"
         r"            # short and never reaches the split\.\n"
         r"            await _send_answer_text\(update, bot_response\)\n",
         "        if bot_response:\n            await send_reply(update, bot_response)\n", 1),
    ]),
    ("the text handler sends the answer on its own", "CAUGHT", [
        (HANDLERS,
         r"(?m)^            if bot_response:\n"
         r"                await _send_answer_text\(update, bot_response\)\n"
         r"            # The files go out even when there is no prose: an answer that\n"
         r"            # is nothing but a document has no prose to send, and dropping\n"
         r"            # the file would leave the user with nothing at all\.\n"
         r"            await send_files\(update, answer\.files, thinking=thinking\)\n"
         r"        except Exception as e:\n"
         r"            import traceback",
         "            if bot_response:\n"
         "                await send_reply(update, bot_response)\n"
         "            # The files go out even when there is no prose: an answer that\n"
         "            # is nothing but a document has no prose to send, and dropping\n"
         "            # the file would leave the user with nothing at all.\n"
         "            await send_files(update, answer.files, thinking=thinking)\n"
         "        except Exception as e:\n"
         "            import traceback", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the behavioural test is gone, and so is the split", "SURVIVED", [
        (TEST_CHUNKING,
         r"async def test_a_long_answer_to_a_photo_arrives_in_several_messages\(\):",
         "async def _disabled_test_a_long_answer_to_a_photo_arrives_in_several_messages():", 1),
        (TEST_CHUNKING,
         r"async def test_a_long_answer_to_a_document_arrives_in_several_messages\(\):",
         "async def _disabled_test_a_long_answer_to_a_document_arrives_in_several_messages():", 1),
        (TEST_CHUNKING,
         r"def test_the_helper_is_where_the_split_happens\(\):",
         "def _disabled_test_the_helper_is_where_the_split_happens():", 1),
        # The short-answer test catches the same mutation from the
        # other side: with the split gone, a short answer would be
        # sent through a path the helper no longer has. Left standing,
        # it caught a mutation this one claims to have removed -- and a
        # group-B mutation caught by a test it did not remove is not a
        # group-B mutation at all.
        (TEST_CHUNKING,
         r"async def test_a_short_answer_to_a_photo_is_still_one_message\(\):",
         "async def _disabled_test_a_short_answer_to_a_photo_is_still_one_message():", 1),
        (HANDLERS, re.escape(SPLIT_LOOP),
         "        await send_reply(update, text, strip=False, parse_mode=\"HTML\")\n", 1),
    ]),
    ("the structural tripwires are gone, and the photo handler sends on "
     "its own", "SURVIVED", [
        (TEST_CHUNKING,
         r"def test_no_handler_sends_the_answer_on_its_own\(\):",
         "def _disabled_test_no_handler_sends_the_answer_on_its_own():", 1),
        (TEST_CHUNKING,
         r"def test_every_handler_sends_its_answer_through_the_helper\(\):",
         "def _disabled_test_every_handler_sends_its_answer_through_the_helper():", 1),
        (TEST_CHUNKING,
         r"async def test_a_long_answer_to_a_photo_arrives_in_several_messages\(\):",
         "async def _disabled_test_a_long_answer_to_a_photo_arrives_in_several_messages():", 1),
        (TEST_TAIL,
         r"def test_every_handler_sends_the_answer_through_the_chunking_helper\(handler\):",
         "def _disabled_test_every_handler_sends_the_answer_through_the_chunking_helper(handler):", 1),
        (TEST_TAIL,
         r"def test_the_reply_still_comes_after_the_persist\(handler\):",
         "def _disabled_test_the_reply_still_comes_after_the_persist(handler):", 1),
        (TEST_SKELETON,
         r"def test_the_skeleton_runs_once_per_stage\(handler\):",
         "def _disabled_test_the_skeleton_runs_once_per_stage(handler):", 1),
        (TEST_SKELETON,
         r"def test_the_skeleton_runs_in_order\(handler\):",
         "def _disabled_test_the_skeleton_runs_in_order(handler):", 1),
        (TEST_SKELETON,
         r"def test_the_reply_is_sent_after_the_turn_is_persisted\(handler\):",
         "def _disabled_test_the_reply_is_sent_after_the_turn_is_persisted(handler):", 1),
        (HANDLERS,
         r"(?m)^        if bot_response:\n            await _send_answer_text\(update, bot_response\)\n",
         "        if bot_response:\n            await send_reply(update, bot_response)\n", 1),
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
