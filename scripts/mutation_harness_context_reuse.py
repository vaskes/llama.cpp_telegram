#!/usr/bin/env python3
"""Mutation harness for the engine's prefix cache (2026-11-01).

The bot talks to an engine that keeps the KV cache of the last prompt and
matches the next one against it prefix by prefix. The match is
all-or-nothing: one changed digit and NOTHING is reused. Measured on the
production engine on 2026-11-01, with the engine's own chat template:

  * the same body sent twice, unchanged:  `20 552 = 20 547 reused + 5
    read` (0.07 s) against `20 552 = 0 reused + 20 552 read` (41.3 s);
  * a context that slides (the newest 20 messages): `reused 0` on every
    turn -- 13 077 tokens re-read in 40.2 s;
  * a growing prefix of 37 165 tokens: reused whole, 1.1 s.

So there are two ways for the bot to throw the reuse away, and both of them
are invisible from the outside: the bot answers, just slowly, forever.

  1. the BEGINNING of the prompt moves -- a tool description that names
     this turn's ids;
  2. the HISTORY moves -- a context that slides instead of folding.

**Group A -- the behaviour.** Each mutation puts one of the defects back,
and the suite has to notice:

  * the description takes the turn's ids again;
  * the tools block is built with the turn's ids again;
  * the turn context is not stored with the row, so the next turn's
    re-render differs from the live one;
  * the turn context is stored INSIDE the user's text;
  * the turn context travels to the engine as a message field;
  * the context slides again (the count ceiling back to 20);
  * the fold drops the newest message -- the one the turn is about.

**Group B -- the tripwires.** Each mutation removes a tripwire *and* the
behaviour it is supposed to catch, and the suite has to go **green**. A
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
    body, not by renaming the function: a renamed test leaves the
    file, and the CODEBOOK's table quotes how many tests it has.

    python3 scripts/mutation_harness_context_reuse.py [ROOT]
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
    "tests/test_context_reuse.py",
    "tests/test_delete_messages_tool.py",
    "tests/test_message_id_in_context.py",
    "tests/test_t70_context_budget.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

TOOL = ROOT / "telegram-bot" / "llm_delete.py"
TOOLS = ROOT / "telegram-bot" / "llm_tools.py"
MESSAGES = ROOT / "telegram-bot" / "llm_messages.py"
PERSIST = ROOT / "telegram-bot" / "persistence.py"
BUDGET = ROOT / "telegram-bot" / "context_budget.py"
TEST_REUSE = ROOT / "tests" / "test_context_reuse.py"
TEST_DELETE = ROOT / "tests" / "test_delete_messages_tool.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the description takes the turn's ids again", "CAUGHT", [
        (TOOL,
         r"def delete_messages_tool_def\(\) -> dict:\n",
         "def delete_messages_tool_def(*, current_message_id: Any = None) -> dict:\n", 1),
        (TOOL,
         r"        \"Delete messages from this Telegram chat\. Use it when the \"\n",
         "        f\"Delete messages from this Telegram chat (turn {current_message_id}). \"\n"
         "        \"Use it when the \"\n", 1),
    ]),
    ("the tools block is built with the turn's ids again", "CAUGHT", [
        (TOOLS,
         r"async def build_tool_defs\(\*, bot, chat_id, chat_type\) -> list\[dict\]:\n",
         "async def build_tool_defs(*, bot, chat_id, chat_type,\n"
         "                          reply_to_message_id=None,\n"
         "                          current_message_id=None) -> list[dict]:\n", 1),
    ]),
    ("the turn context is not stored with the row", "CAUGHT", [
        (PERSIST,
         r"    if turn_context:\n        msg\[\"_turn_context\"\] = turn_context\n",
         "", 1),
    ]),
    ("the turn context is stored inside the user's text", "CAUGHT", [
        (PERSIST,
         r"    if turn_context:\n        msg\[\"_turn_context\"\] = turn_context\n",
         "    if turn_context:\n"
         "        msg[\"content\"] = str(content) + \"\\n\" + turn_context\n", 1),
    ]),
    ("the turn context travels to the engine as a field", "CAUGHT", [
        (MESSAGES,
         r"        clean = \{k: v for k, v in m\.items\(\) if not k\.startswith\(\"_\"\)\}\n"
         r"        return \{\n            \*\*clean,\n",
         "        clean = {k: v for k, v in m.items() if not k.startswith(\"_\")}\n"
         "        return {\n            **clean,\n"
         "            \"_turn_context\": m.get(\"_turn_context\"),\n", 1),
    ]),
    ("the context slides again", "CAUGHT", [
        (BUDGET,
         r"DEFAULT_MAX_MESSAGES = 400\n",
         "DEFAULT_MAX_MESSAGES = 20\n", 1),
    ]),
    ("the fold drops the newest message", "CAUGHT", [
        (BUDGET,
         r"        while start < len\(selected\) - 1 and total > self\.token_budget:\n",
         "        while start < len(selected) and total > self.token_budget:\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the static-description tripwires are gone, and the description changes",
     "SURVIVED", [
        (TEST_REUSE,
         r"(?m)^def test_the_tool_description_takes_no_per_turn_arguments\(\):\n",
         "def test_the_tool_description_takes_no_per_turn_arguments():\n    return\n", 1),
        (TEST_DELETE,
         r"(?m)^def test_the_tool_description_cannot_depend_on_the_turn\(\):\n",
         "def test_the_tool_description_cannot_depend_on_the_turn():\n    return\n", 1),
        (TOOL,
         r"def delete_messages_tool_def\(\) -> dict:\n",
         "def delete_messages_tool_def(*, current_message_id: Any = None) -> dict:\n", 1),
        (TOOL,
         r"        \"Delete messages from this Telegram chat\. Use it when the \"\n",
         "        f\"Delete messages from this Telegram chat (turn {current_message_id}). \"\n"
         "        \"Use it when the \"\n", 1),
    ]),
    ("the static-tools tripwire is gone, and the tools take the ids",
     "SURVIVED", [
        (TEST_REUSE,
         r"(?m)^def test_the_tool_list_builder_takes_no_per_turn_arguments\(\):\n",
         "def test_the_tool_list_builder_takes_no_per_turn_arguments():\n    return\n", 1),
        (TOOLS,
         r"async def build_tool_defs\(\*, bot, chat_id, chat_type\) -> list\[dict\]:\n",
         "async def build_tool_defs(*, bot, chat_id, chat_type,\n"
         "                          reply_to_message_id=None,\n"
         "                          current_message_id=None) -> list[dict]:\n", 1),
    ]),
    ("the round-trip tripwires are gone, and the row keeps no context",
     "SURVIVED", [
        (TEST_REUSE,
         r"(?m)^async def test_the_render_from_the_database_is_the_same_as_the_live_one\(\n"
         r"        tmp_path, monkeypatch\):\n",
         "async def test_the_render_from_the_database_is_the_same_as_the_live_one(\n"
         "        tmp_path, monkeypatch):\n    return\n", 1),
        (TEST_REUSE,
         r"(?m)^async def test_the_record_keeps_the_users_own_text\(tmp_path, monkeypatch\):\n",
         "async def test_the_record_keeps_the_users_own_text(tmp_path, monkeypatch):\n    return\n", 1),
        (PERSIST,
         r"    if turn_context:\n        msg\[\"_turn_context\"\] = turn_context\n",
         "", 1),
    ]),
    ("the record tripwire is gone, and the context is inside the text",
     "SURVIVED", [
        (TEST_REUSE,
         r"(?m)^async def test_the_record_keeps_the_users_own_text\(tmp_path, monkeypatch\):\n",
         "async def test_the_record_keeps_the_users_own_text(tmp_path, monkeypatch):\n    return\n", 1),
        (TEST_REUSE,
         r"(?m)^async def test_the_render_from_the_database_is_the_same_as_the_live_one\(\n"
         r"        tmp_path, monkeypatch\):\n",
         "async def test_the_render_from_the_database_is_the_same_as_the_live_one(\n"
         "        tmp_path, monkeypatch):\n    return\n", 1),
        (PERSIST,
         r"    if turn_context:\n        msg\[\"_turn_context\"\] = turn_context\n",
         "    if turn_context:\n"
         "        msg[\"content\"] = str(content) + \"\\n\" + turn_context\n", 1),
    ]),
    ("the engine-field tripwire is gone, and the field travels", "SURVIVED", [
        (TEST_REUSE,
         r"(?m)^def test_the_turn_context_never_reaches_the_engine_as_a_field\(\):\n",
         "def test_the_turn_context_never_reaches_the_engine_as_a_field():\n    return\n", 1),
        (MESSAGES,
         r"        clean = \{k: v for k, v in m\.items\(\) if not k\.startswith\(\"_\"\)\}\n"
         r"        return \{\n            \*\*clean,\n",
         "        clean = {k: v for k, v in m.items() if not k.startswith(\"_\")}\n"
         "        return {\n            **clean,\n"
         "            \"_turn_context\": m.get(\"_turn_context\"),\n", 1),
    ]),
    ("the growing-context tripwires are gone, and the context slides",
     "SURVIVED", [
        (TEST_REUSE,
         r"(?m)^def test_the_context_only_ever_grows\(\):\n",
         "def test_the_context_only_ever_grows():\n    return\n", 1),
        (TEST_REUSE,
         r"(?m)^def test_the_prompt_of_the_next_turn_starts_with_the_prompt_of_the_last\(\):\n",
         "def test_the_prompt_of_the_next_turn_starts_with_the_prompt_of_the_last():\n    return\n", 1),
        (BUDGET,
         r"DEFAULT_MAX_MESSAGES = 400\n",
         "DEFAULT_MAX_MESSAGES = 20\n", 1),
    ]),
    ("the fold tripwire is gone, and the fold drops the newest", "SURVIVED", [
        (TEST_REUSE,
         r"(?m)^def test_a_message_alone_over_the_budget_is_still_kept\(\):\n",
         "def test_a_message_alone_over_the_budget_is_still_kept():\n    return\n", 1),
        (BUDGET,
         r"        while start < len\(selected\) - 1 and total > self\.token_budget:\n",
         "        while start < len(selected) and total > self.token_budget:\n", 1),
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
    return 1 if (wrong or refused or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
