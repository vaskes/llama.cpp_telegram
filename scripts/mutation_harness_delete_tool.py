#!/usr/bin/env python3
"""Mutation harness for the delete_messages tool (2026-10-26).

The operator asked for a tool that removes messages from the Telegram
chat, and in the same breath said the one thing it must not do:
"sqlite наша священная корова -- источник истины и всего того, что
происходило в канале. Базу не трогаем."

A tool that deletes things is worth more tripwires than a tool that
reads them, because the failures are silent: a message that was not
deleted, a message that WAS deleted when it should not have been, and a
history that no longer matches the chat.

**Group A -- the behaviour.** Each mutation puts one of the defects
back, and the suite has to notice:

  * the answer is deleted in halves instead of whole;
  * the administrator's right is not checked, and messages the bot may
    not delete are deleted anyway;
  * the cap on how much one call may remove is gone;
  * a deleted message stays in the registry and is deleted again;
  * the tool is not in the isolation layer's whitelist, so the model
    loops on it;
  * the module reaches for the database;
  * the prompt forgets the tool, so the model never calls it;
  * the tool grows a chat_id parameter, and the model's reach stops
    being the conversation it is in.

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

    python3 scripts/mutation_harness_delete_tool.py [ROOT]
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
    "tests/test_delete_messages_tool.py",
    "tests/test_tool_isolation.py",
    "tests/test_shell_isolation.py",
    "tests/test_prompts.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

TOOL = ROOT / "telegram-bot" / "llm_delete.py"
ISOLATION = ROOT / "telegram-bot" / "tool_isolation.py"
PROMPT = ROOT / "telegram-bot" / "prompt_text" / "group_context.py"
TEST_TOOL = ROOT / "tests" / "test_delete_messages_tool.py"
TEST_ISOLATION = ROOT / "tests" / "test_tool_isolation.py"
TEST_SHELL = ROOT / "tests" / "test_shell_isolation.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the answer is deleted in halves", "CAUGHT", [
        (TOOL,
         r"        if current is not None and group != current and len\(order\) >= count:\n            break\n",
         "        if len(order) >= count:\n            break\n", 1),
    ]),
    ("the administrator's right is not checked", "CAUGHT", [
        (TOOL,
         r"    allowed, reason = await _check_admin\(bot, key\)\n",
         "    allowed, reason = True, \"\"\n", 1),
    ]),
    ("the cap on one call is gone", "CAUGHT", [
        (TOOL,
         r"_MAX_DELETE_COUNT = 20\n",
         "_MAX_DELETE_COUNT = 1000\n", 1),
    ]),
    ("a deleted message stays in the registry", "CAUGHT", [
        (TOOL,
         r"    if deleted:\n        forget_messages\(chat_id, deleted\)\n",
         "    if False:\n        forget_messages(chat_id, deleted)\n", 1),
    ]),
    ("the tool is not in the isolation whitelist", "CAUGHT", [
        (ISOLATION,
         r"    \"delete_messages\",\n\}\)\n",
         "})\n", 1),
    ]),
    ("the module reaches for the database", "CAUGHT", [
        (TOOL,
         r"import time\n",
         "import storage\nimport time\n", 1),
    ]),
    ("the prompt forgets the tool", "CAUGHT", [
        (PROMPT,
         r"  - `delete_messages`       - remove messages from this chat \(only\n"
         r"                              where the bot is an administrator, or\n"
         r"                              in a private chat; absent entirely\n"
         r"                              elsewhere\)\n",
         "", 1),
    ]),
    ("the tool grows a chat_id parameter", "CAUGHT", [
        (TOOL,
         r"                    \"count\": \{\n",
         "                    \"chat_id\": {\n"
         "                        \"type\": \"integer\",\n"
         "                        \"description\": \"the chat to delete from\",\n"
         "                    },\n"
         "                    \"count\": {\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the no-chat_id tripwire is gone, and the tool has a chat_id",
     "SURVIVED", [
        (TEST_TOOL,
         r"(?m)^def test_the_tool_def_carries_no_chat_id\(\):\n",
         "def test_the_tool_def_carries_no_chat_id():\n    return\n", 1),
        (TOOL,
         r"                    \"count\": \{\n",
         "                    \"chat_id\": {\n"
         "                        \"type\": \"integer\",\n"
         "                        \"description\": \"the chat to delete from\",\n"
         "                    },\n"
         "                    \"count\": {\n", 1),
    ]),
    ("the database tripwires are gone, and the module reaches for the db",
     "SURVIVED", [
        (TEST_TOOL,
         r"(?m)^def test_the_module_does_not_import_the_database\(\):\n",
         "def test_the_module_does_not_import_the_database():\n    return\n", 1),
        (TEST_TOOL,
         r"(?m)^async def test_deleting_a_message_does_not_write_to_the_database\(monkeypatch\):\n",
         "async def test_deleting_a_message_does_not_write_to_the_database(monkeypatch):\n    return\n", 1),
        (TOOL,
         r"import time\n",
         "import storage\nimport time\n", 1),
    ]),
    ("the whole-answer tripwires are gone, and the answer is cut in half",
     "SURVIVED", [
        (TEST_TOOL,
         r"(?m)^def test_the_registry_remembers_answers_as_groups\(\):\n",
         "def test_the_registry_remembers_answers_as_groups():\n    return\n", 1),
        (TEST_TOOL,
         r"(?m)^async def test_it_deletes_the_bots_last_answer\(\):\n",
         "async def test_it_deletes_the_bots_last_answer():\n    return\n", 1),
        (TOOL,
         r"        if current is not None and group != current and len\(order\) >= count:\n            break\n",
         "        if len(order) >= count:\n            break\n", 1),
    ]),
    ("the rights tripwires are gone, and the right is not checked",
     "SURVIVED", [
        (TEST_TOOL,
         r"(?m)^async def test_a_plain_member_may_not_delete\(\):\n",
         "async def test_a_plain_member_may_not_delete():\n    return\n", 1),
        (TEST_TOOL,
         r"(?m)^async def test_an_administrator_without_the_right_may_not_delete\(\):\n",
         "async def test_an_administrator_without_the_right_may_not_delete():\n    return\n", 1),
        (TEST_TOOL,
         r"(?m)^async def test_the_rights_are_checked_once_and_cached\(\):\n",
         "async def test_the_rights_are_checked_once_and_cached():\n    return\n", 1),
        (TEST_TOOL,
         r"(?m)^async def test_it_refuses_where_the_bot_may_not_delete\(\):\n",
         "async def test_it_refuses_where_the_bot_may_not_delete():\n    return\n", 1),
        (TOOL,
         r"    allowed, reason = await _check_admin\(bot, key\)\n",
         "    allowed, reason = True, \"\"\n", 1),
    ]),
    ("the whitelist tripwire is gone, and the tool is not registered",
     "SURVIVED", [
        (TEST_ISOLATION,
         r"(?m)^def test_known_tool_names_includes_required_bot_tools\(\):\n",
         "def test_known_tool_names_includes_required_bot_tools():\n    return\n", 1),
        (TEST_SHELL,
         r"(?m)^def test_known_tool_names_exact_set\(\):\n",
         "def test_known_tool_names_exact_set():\n    return\n", 1),
        (TEST_TOOL,
         r"(?m)^def test_the_tool_name_is_registered_for_the_isolation_layer\(\):\n",
         "def test_the_tool_name_is_registered_for_the_isolation_layer():\n    return\n", 1),
        (ISOLATION,
         r"    \"delete_messages\",\n\}\)\n",
         "})\n", 1),
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
