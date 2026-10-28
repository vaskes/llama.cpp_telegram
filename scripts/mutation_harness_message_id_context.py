#!/usr/bin/env python3
"""Mutation harness for the Telegram id in the conversation (2026-10-27).

The operator's live test of 2026-10-27 (01:21 MSK) failed in a way
that is worth writing down, because it was not a bug in the delete
tool: the tool was never called.

  * the operator wrote "Сообщение 1", "Сообщение 2", "Сообщение 3",
    "Сообщение 4" and then "Удали сообщение 1";
  * the bot answered «Сообщение 1» — 3369 (right, by luck), then
    «Сообщение 1» (3354), «Сообщение 2» (3357) -- both wrong, and both
    ids of the BOT's own messages, taken from the list in the delete
    tool's description;
  * on the last turn it called no tool at all (reasoning_chars=9851,
    tool_calls=0) and asked the operator to clarify.

The cause: the id of a message the USER wrote was nowhere in anything
the model was shown. A message named by its text had no id to resolve
to, and a model without an id takes a number out of whatever bag of
numbers it can see -- here, the bag of the bot's own ids.

So the id now rides along with the text, and the rules say where an id
may come from. **Group A -- the behaviour.** Each mutation puts one of
the defects back, and the suite has to notice:

  * the marker is not rendered, so the model reads no ids at all;
  * the context query drops the id, so the history carries text only;
  * the summarising query drops the id, so the raw tail loses it;
  * the decoder drops the id between the query and the engine;
  * the photo turn -- the one turn that is not persisted -- loses it;
  * the tool description forgets the marker, so the model reads an id
    and refuses to pass it;
  * the tool description loses the fence around the bot's own ids, so
    the model offers 3354 as the user's message again;
  * the prompt forgets the marker, so the model does not know that the
    conversation is a source of ids.

**Group B -- the tripwires.** Each mutation removes a tripwire *and*
the behaviour it is supposed to catch, and the suite has to go
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
    body, not by renaming the function: a renamed test leaves the
    file, and the CODEBOOK's table quotes how many tests it has;
  * a prompt mutation updates the content pin with it, so that the pin
    is not the thing doing the catching -- the rule about the prompt is
    checked by a rule of its own.

    python3 scripts/mutation_harness_message_id_context.py [ROOT]
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
    "tests/test_message_id_in_context.py",
    "tests/test_delete_messages_tool.py",
    "tests/test_chat_history_search.py",
    "tests/test_message_search.py",
    "tests/test_telegram_id_recorded.py",
    "tests/test_call_llama.py",
    "tests/test_s1_sender_sanitisation.py",
    "tests/test_prompts.py",
    "tests/test_codebook_test_table.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

MESSAGES = ROOT / "telegram-bot" / "llm_messages.py"
STORAGE = ROOT / "telegram-bot" / "storage.py"
SUMMARIES = ROOT / "telegram-bot" / "storage_summaries.py"
PERSISTENCE = ROOT / "telegram-bot" / "persistence.py"
HANDLERS = ROOT / "telegram-bot" / "handlers.py"
DELETE = ROOT / "telegram-bot" / "llm_delete.py"
DISPATCH = ROOT / "telegram-bot" / "llm_dispatch.py"
PROMPT = ROOT / "telegram-bot" / "prompt_text" / "group_context.py"
TEST_NEW = ROOT / "tests" / "test_message_id_in_context.py"
TEST_SEARCH = ROOT / "tests" / "test_chat_history_search.py"
TEST_PROMPTS = ROOT / "tests" / "test_prompts.py"
TEST_DELETE = ROOT / "tests" / "test_delete_messages_tool.py"

#: The sha of the prompt as it looks after the mutation below. It is
#: written out because the harness must not depend on the tree it
#: measures: the pin is updated WITH the mutation, so that the catch
#: comes from the rule and not from the pin.
MUTATED_PROMPT_PIN = "6649fcf44b0132a0"


# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the marker is not rendered at all", "CAUGHT", [
        (MESSAGES,
         r'    return ID_MARKER\.format\(id=tid\) \+ "\\n"\n',
         '    return ""\n', 1),
    ]),
    ("the context query drops the telegram id", "CAUGHT", [
        (STORAGE,
         r"            SELECT id, role, content, sender_name, rating, created_at,\n"
         r"                   summary_id, telegram_message_id FROM \(\n",
         "            SELECT id, role, content, sender_name, rating, created_at,\n"
         "                   summary_id FROM (\n", 1),
    ]),
    ("the summarising query drops the telegram id", "CAUGHT", [
        (SUMMARIES,
         r"                SELECT id, role, content, sender_name, rating, created_at,\n"
         r"                       summary_id, telegram_message_id\n",
         "                SELECT id, role, content, sender_name, rating, created_at,\n"
         "                       summary_id\n", 1),
    ]),
    ("the decoder drops the id between the query and the engine", "CAUGHT", [
        (PERSISTENCE,
         r'        if r\.get\("telegram_message_id"\) is not None:\n'
         r'            msg\["_telegram_message_id"\] = r\["telegram_message_id"\]\n',
         '        if False:\n'
         '            msg["_telegram_message_id"] = r["telegram_message_id"]\n', 1),
    ]),
    ("the photo turn loses the id", "CAUGHT", [
        (HANDLERS,
         r'                        "_sender_name": sender_name,\n'
         r'                        "_telegram_message_id": update\.message\.message_id\}\n',
         '                        "_sender_name": sender_name}\n', 1),
    ]),
    ("the tool description forgets the marker", "CAUGHT", [
        (DELETE,
         r"\[telegram id N\]",
         "the id marked in the conversation", 3),
    ]),
    ("the tool description loses the fence around the bot's own ids",
     "CAUGHT", [
         (DELETE,
           r'\+ "\. These are the BOT\'s messages, not the user\'s -- never "\n',
           '+ ". These are the BOT\'s messages -- "\n', 1),
     ]),
    ("the prompt forgets the marker", "CAUGHT", [
        (PROMPT,
         r"ids of the bot's own messages, and the ids marked `\[telegram id N\]`",
         "ids of the bot's own messages", 1),
        (PROMPT,
         r"`\[telegram id N\]` marker above its text",
         "the marker above its text", 1),
        # The pin moves WITH the mutation. If it did not, the pin
        # would catch every prompt change, and the catch would prove
        # nothing about the rule.
        (TEST_PROMPTS,
         r'"GROUP_CONTEXT": "[0-9a-f]{16}"',
         f'"GROUP_CONTEXT": "{MUTATED_PROMPT_PIN}"', 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the id tripwires are gone, and the context carries text only",
     "SURVIVED", [
         (TEST_NEW,
          r"(?m)^async def test_the_model_sees_the_id_next_to_the_text_it_wrote\(store\):\n",
          "async def test_the_model_sees_the_id_next_to_the_text_it_wrote(store):\n    return\n", 1),
         (TEST_NEW,
          r"(?m)^async def test_every_message_with_an_id_is_marked\(store\):\n",
          "async def test_every_message_with_an_id_is_marked(store):\n    return\n", 1),
         (TEST_NEW,
          r"(?m)^async def test_the_marker_is_a_line_of_its_own_above_the_text\(store\):\n",
          "async def test_the_marker_is_a_line_of_its_own_above_the_text(store):\n    return\n", 1),
         (TEST_NEW,
          r"(?m)^def test_the_context_query_returns_the_id\(store\):\n",
          "def test_the_context_query_returns_the_id(store):\n    return\n", 1),
         (TEST_NEW,
          r"(?m)^async def test_the_id_the_model_read_is_one_the_bot_vouches_for\(store\):\n",
          "async def test_the_id_the_model_read_is_one_the_bot_vouches_for(store):\n    return\n", 1),
         (STORAGE,
          r"            SELECT id, role, content, sender_name, rating, created_at,\n"
          r"                   summary_id, telegram_message_id FROM \(\n",
          "            SELECT id, role, content, sender_name, rating, created_at,\n"
          "                   summary_id FROM (\n", 1),
     ]),
    ("the vouching tripwires are gone, and an invented id reaches Telegram",
     "SURVIVED", [
         (TEST_NEW,
          r"(?m)^async def test_the_id_the_model_read_is_one_the_bot_vouches_for\(store\):\n",
          "async def test_the_id_the_model_read_is_one_the_bot_vouches_for(store):\n    return\n", 1),
         (TEST_NEW,
          r"(?m)^async def test_an_id_that_is_not_in_the_record_is_still_refused\(store\):\n",
          "async def test_an_id_that_is_not_in_the_record_is_still_refused(store):\n    return\n", 1),
         (TEST_SEARCH,
          r"(?m)^async def test_an_id_the_record_does_not_hold_is_still_refused\(store\):\n",
          "async def test_an_id_the_record_does_not_hold_is_still_refused(store):\n    return\n", 1),
         (TEST_SEARCH,
          r"(?m)^async def test_an_id_of_another_chat_is_refused\(store\):\n",
          "async def test_an_id_of_another_chat_is_refused(store):\n    return\n", 1),
         (TEST_DELETE,
          r"(?m)^async def test_the_current_message_id_is_refused_when_it_is_not_the_turn\(\):\n",
          "async def test_the_current_message_id_is_refused_when_it_is_not_the_turn():\n    return\n", 1),
         (DISPATCH,
          r"        vouched = await known_ids\(deps\.chat_id, requested_ids\(args\)\)\n",
          "        vouched = set(requested_ids(args))\n", 1),
     ]),
    ("the description tripwire is gone, and the description loses the marker",
     "SURVIVED", [
         (TEST_NEW,
          r"(?m)^def test_the_tool_description_names_the_marker\(\):\n",
          "def test_the_tool_description_names_the_marker():\n    return\n", 1),
         (DELETE,
          r"\[telegram id N\]",
          "the id marked in the conversation", 3),
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
