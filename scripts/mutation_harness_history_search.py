#!/usr/bin/env python3
"""Mutation harness for the chat's own record and the search over it (2026-10-27).

The operator asked for two things in one breath: the bot must be able to
find a message it was sent -- by its text, by who wrote it -- and take
that message back out of the chat, **while the row in the database
stays**. The second half is the one that is easy to lose, because a
deletion that cleans up after itself looks tidier and destroys the
record.

**Group A -- the behaviour.** Each mutation puts one of the defects
back, and the suite has to notice:

  * the row does not carry the Telegram id of the message it came from;
  * the search does not fold the case, so it finds nothing in Russian;
  * the search reaches into another chat;
  * the search does not return the id, so there is nothing to act on;
  * the handlers do not store the id they were given;
  * the group-absorb path stops storing the message that was replied to;
  * the record carries a bare user id again, in the shape of a message id;
  * the tool is not in the isolation layer's whitelist, so the model
    loops on it;
  * the prompt forgets the tool, so the model never calls it;
  * the delete tool does not receive the ids the record vouches for;
  * the dispatcher vouches for an id the record does NOT hold -- the
    guard of 2026-10-26, which is the reason the tool refuses an id at
    all;
  * the tool is not dispatched, so the model calls into the void.

**Group B -- the tripwires.** Each mutation removes a tripwire *and* the
behaviour it is supposed to catch, and the suite has to go **green**. A
green suite with the tripwire gone is the proof that the tripwire was
the thing doing the work.

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
    file, and the CODEBOOK's table quotes how many tests it has.

    python3 scripts/mutation_harness_history_search.py [ROOT]
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
    "tests/test_chat_history_search.py",
    "tests/test_message_search.py",
    "tests/test_telegram_id_recorded.py",
    "tests/test_delete_messages_tool.py",
    "tests/test_tool_isolation.py",
    "tests/test_shell_isolation.py",
    "tests/test_drift_r8_known_tools.py",
    "tests/test_prompts.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

STORAGE = ROOT / "telegram-bot" / "storage.py"
HANDLERS = ROOT / "telegram-bot" / "handlers.py"
ABSORB = ROOT / "telegram-bot" / "dispatch_group.py"
HISTORY = ROOT / "telegram-bot" / "llm_history.py"
DELETE = ROOT / "telegram-bot" / "llm_delete.py"
DISPATCH = ROOT / "telegram-bot" / "llm_dispatch.py"
ISOLATION = ROOT / "telegram-bot" / "tool_isolation.py"
PROMPT = ROOT / "telegram-bot" / "prompt_text" / "group_context.py"
TEST_SEARCH = ROOT / "tests" / "test_chat_history_search.py"
TEST_STORE = ROOT / "tests" / "test_message_search.py"
TEST_IDS = ROOT / "tests" / "test_telegram_id_recorded.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the row does not carry the telegram id", "CAUGHT", [
        (STORAGE,
         r"            \(chat_id, thread_id, role, content, sender_name, rating,\n"
         r"             telegram_message_id\),\n",
         "            (chat_id, thread_id, role, content, sender_name, rating,\n"
         "             None),\n", 1),
    ]),
    ("the search does not fold the case", "CAUGHT", [
        (STORAGE,
         r"casefold\(COALESCE\(CASE WHEN json_valid\(content\) ",
         "lower(COALESCE(CASE WHEN json_valid(content) ", 1),
    ]),
    ("the search reaches into another chat", "CAUGHT", [
        (STORAGE,
         r'        where = \["chat_id = \?"\]\n',
         '        where = ["1 = 1"]\n', 1),
    ]),
    ("the search does not return the telegram id", "CAUGHT", [
        (STORAGE,
         r'        sql = \("SELECT id, role, sender_name, created_at, "\n'
         r'               "telegram_message_id, content FROM messages WHERE "\n',
         '        sql = ("SELECT id, role, sender_name, created_at, "\n'
         '               "content FROM messages WHERE "\n', 1),
    ]),
    ("the text handler does not store the id", "CAUGHT", [
        (HANDLERS,
         r"            await turn\.persist\(user_message, role='user',\n"
         r"                               sender_name=sender_name,\n"
         r"                               telegram_message_id=update\.message\.message_id\)\n",
         "            await turn.persist(user_message, role='user',\n"
         "                               sender_name=sender_name)\n", 1),
    ]),
    ("the absorb path stops storing the message that was replied to",
     "CAUGHT", [
        (ABSORB,
         r"    if rtm is not None and rtm\.message_id is not None:\n",
         "    if False:\n", 1),
    ]),
    ("the record carries a bare user id again", "CAUGHT", [
        (ABSORB,
         r'    return getattr\(user, "first_name", None\) or "unknown"\n',
         '    return f"id={getattr(user, \'id\', \'?\')}"\n', 1),
    ]),
    ("the tool is not in the isolation whitelist", "CAUGHT", [
        (ISOLATION,
         r'    "search_messages",\n\}\)\n',
         "})\n", 1),
    ]),
    ("the prompt forgets the tool", "CAUGHT", [
        (PROMPT,
         r"  - `search_messages`       - search the bot's OWN RECORD of this\n"
         r"                              chat: the messages it was sent, the\n"
         r"                              ones it absorbed from the group, and\n"
         r"                              its own answers\n",
         "", 1),
    ]),
    ("the delete tool does not receive the vouched ids", "CAUGHT", [
        (DELETE,
         r"    allowed = authorized_ids\(chat_id, reply_to_message_id, current_message_id,\n"
         r"                              known_ids\)\n",
         "    allowed = authorized_ids(chat_id, reply_to_message_id, current_message_id)\n", 1),
    ]),
    ("the dispatcher vouches for an id the record does not hold", "CAUGHT", [
        (DISPATCH,
         r"        vouched = await known_ids\(deps\.chat_id, requested_ids\(args\)\)\n",
         "        vouched = set(requested_ids(args))\n", 1),
    ]),
    ("the tool is not dispatched", "CAUGHT", [
        (DISPATCH,
         r'    if name == "search_messages":\n',
         '    if name == "search_messages_never":\n', 1),
    ]),
    ("the result does not show the id", "CAUGHT", [
        (HISTORY,
         r'        where = \(f"telegram id \{tg_id\}" if tg_id is not None\n',
         '        where = ("no telegram id" if True\n', 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the survival tripwire is gone, and the deletion takes the record "
     "with it", "SURVIVED", [
        (TEST_SEARCH,
         r"(?m)^async def test_the_record_survives_the_deletion\(store\):\n",
         "async def test_the_record_survives_the_deletion(store):\n    return\n", 1),
        (DISPATCH,
         r"        ledger = _entry\(\"delete_messages\", args, deps\.iteration,\n",
         "        import storage as _st\n"
         "        _st.get_store().delete_message_ids(\n"
         "            deps.chat_id, 'main', list(requested_ids(args)))\n"
         "        ledger = _entry(\"delete_messages\", args, deps.iteration,\n", 1),
    ]),
    ("the refusal tripwires are gone, and an invented id reaches Telegram",
     "SURVIVED", [
        (TEST_SEARCH,
         r"(?m)^async def test_an_id_the_record_does_not_hold_is_still_refused\(store\):\n",
         "async def test_an_id_the_record_does_not_hold_is_still_refused(store):\n    return\n", 1),
        (TEST_SEARCH,
         r"(?m)^async def test_an_id_of_another_chat_is_refused\(store\):\n",
         "async def test_an_id_of_another_chat_is_refused(store):\n    return\n", 1),
        # The delete tool's own file guards the same property from the
        # other side: it drives the dispatcher with an invented id and
        # requires that nothing was deleted. A group-B mutation that
        # left it standing would be caught by a test it did not claim to
        # remove -- and then it is not a group-B mutation at all.
        (ROOT / "tests" / "test_delete_messages_tool.py",
         r"(?m)^async def test_the_current_message_id_is_refused_when_it_is_not_the_turn\(\):\n",
         "async def test_the_current_message_id_is_refused_when_it_is_not_the_turn():\n    return\n", 1),
        (DISPATCH,
         r"        vouched = await known_ids\(deps\.chat_id, requested_ids\(args\)\)\n",
         "        vouched = set(requested_ids(args))\n", 1),
    ]),
    ("the case tripwire is gone, and the case is not folded", "SURVIVED", [
        (TEST_STORE,
         r"(?m)^def test_the_search_ignores_the_case_of_russian_text\(store\):\n",
         "def test_the_search_ignores_the_case_of_russian_text(store):\n    return\n", 1),
        (STORAGE,
         r"casefold\(COALESCE\(CASE WHEN json_valid\(content\) ",
         "lower(COALESCE(CASE WHEN json_valid(content) ", 1),
    ]),
    ("the id tripwire is gone, and the row does not carry the id",
     "SURVIVED", [
        (TEST_IDS,
         r"(?m)^def test_the_text_handler_stores_the_telegram_id\(store, monkeypatch\):\n",
         "def test_the_text_handler_stores_the_telegram_id(store, monkeypatch):\n    return\n", 1),
        (HANDLERS,
         r"            await turn\.persist\(user_message, role='user',\n"
         r"                               sender_name=sender_name,\n"
         r"                               telegram_message_id=update\.message\.message_id\)\n",
         "            await turn.persist(user_message, role='user',\n"
         "                               sender_name=sender_name)\n", 1),
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
