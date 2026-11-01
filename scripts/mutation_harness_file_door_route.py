#!/usr/bin/env python3
"""Mutation harness for the file door's route (2026-11-01).

A file in the sandbox is read by the model through the MCP file door.
What it was before this wave: the model never went there.

Measured on production, 2026-11-01, 19:20-19:26 MSK: the operator asked
for a 603 675-byte file to be read into the model's context whole. The
model read a window size out of a screenshot in the chat, decided the
file could not fit, and answered that reading it whole is "physically
impossible" -- without calling a tool. When it did try, in the next
turn, it called `bash_command` with `cat`, and the console cut the file
twice (the side-car at 524 288 bytes, the bot at 8 000 characters). The
door was in the tool list the whole time, and the side-car's read budget
was four times the size of the file.

The fix is one section in the system prompt: it names the door this
chat has, says the door delivers a file whole, says the console cannot,
and says the server -- not the model's arithmetic about a window size it
cannot see -- decides whether a file fits.

**Group A -- the behaviour.** Each mutation puts one of the defects
back, and the tripwires have to notice:

  * the section is not in the prompt at all;
  * the section names a door the chat does not have (a name the model
    cannot call is a name it invents);
  * the registry looks for a tool that is not the door, so the door is
    never found;
  * the tool list is not consulted, so a door outside it is named;
  * the section loses the sentence that says the console is not a door;
  * the section loses the sentence that says the server is the arbiter.

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

    python3 scripts/mutation_harness_file_door_route.py [ROOT]
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()

#: The tripwires that carry the rule. The suite that judges the
#: mutations is this file alone -- the point is to measure what the
#: tripwire does, not what the rest of the repository does.
TARGETS = ["tests/test_file_door_route.py"]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

CALL_LLAMA = ROOT / "telegram-bot" / "call_llama.py"
FILEDOOR = ROOT / "telegram-bot" / "llm_filedoor.py"
MCP_REGISTRY = ROOT / "telegram-bot" / "mcp_registry.py"
LLM_TOOLS = ROOT / "telegram-bot" / "llm_tools.py"
BASH_COMMAND = ROOT / "telegram-bot" / "personal_tools" / "bash_command.py"
TEST_FILE = ROOT / "tests" / "test_file_door_route.py"

#: The def line of every tripwire, exactly as the file has it. A group
#: B mutation replaces the line with itself and a `return` under it: the
#: test stays in the file -- the CODEBOOK quotes how many tests each
#: file has, and a renamed test leaves the collection -- and stops
#: testing anything.
SILENCE = {
    "registry": ("async def test_the_registry_names_the_servers_file_door("
                 "monkeypatch):"),
    "notdoor": ("async def test_a_tool_that_is_not_the_door_is_not_a_door("
                "monkeypatch):"),
    "gate": ("async def test_a_door_the_chat_may_not_use_is_not_named("
             "monkeypatch):"),
    "list": ("async def test_only_a_door_that_is_in_the_tool_list_is_named("
             "monkeypatch):"),
    "prompt": ("async def test_the_system_prompt_carries_the_section("
               "monkeypatch):"),
    "console": ("async def test_the_section_says_the_console_is_not_a_door("
                "monkeypatch):"),
    "whole": ("async def test_the_section_says_the_door_delivers_a_file_"
              "whole(monkeypatch):"),
    "arbiter": ("async def test_the_section_says_the_server_decides_whether_"
                "a_file_fits(\n        monkeypatch):"),
    "nodoor": ("async def test_a_chat_without_a_door_is_told_about_no_door("
               "monkeypatch):"),
    "desc": ("def test_the_door_says_in_its_own_description_that_it_is_the_"
             "door():"),
    "call": ("async def test_the_section_says_to_call_the_tool(monkeypatch):"),
    "limit": ("async def test_the_section_gives_the_limit(monkeypatch):"),
    "stable": ("async def test_the_section_does_not_move_from_turn_to_turn("
               "monkeypatch):"),
    "cut": ("def test_a_cut_output_says_it_is_a_fragment_and_names_the_"
            "door():"),
    "cutbot": ("def test_the_bot_own_cut_counts_as_a_cut():"),
    "cutquiet": ("def test_an_uncut_output_carries_no_such_notice():"),
    "cutfrag": ("def test_a_cut_still_returns_what_reached_the_bot():"),
}


def _silence(key):
    """The op that turns one tripwire off."""
    line = SILENCE[key]
    return (TEST_FILE, re.escape(line) + "\n",
            line + "\n    return\n", 1)


def _group_b(keys, behaviour_op):
    """The tripwires that were doing the work, off, and the defect back."""
    return [_silence(key) for key in keys] + [behaviour_op]


#: Which tests each defect turns red. MEASURED, not guessed: every
#: mutation below was applied to the tree and the red tests were read
#: off the run (2026-11-01). A group B mutation silences exactly those
#: tests, so "the tripwire is gone" means what it says -- and a
#: tripwire nobody silences would make the group B proof a fiction.
CAUGHT_BY = {
    "nosection": ["prompt", "console", "whole", "arbiter", "call",
                  "limit"],
    "hardcode": ["nodoor"],
    "wrongbare": ["registry", "notdoor", "gate", "list", "prompt",
                  "console", "whole", "arbiter", "call", "limit"],
    "narrow": ["list", "nodoor"],
    "nowhole": ["console"],
    "noarbiter": ["arbiter"],
    "nodesc": ["desc"],
    "nocutnotice": ["cut", "cutbot"],
    "cutnodname": ["cut"],
}


#: The behaviour mutations -- the defects this wave put back.
BEHAVIOUR = {
    # The section is not in the prompt at all: the model is never told
    # which tool reads a file, and it goes back to the console.
    "nosection": (CALL_LLAMA,
                  r"\n                \+ instruction_for\(\n"
                  r"                    file_door_names\(all_tools, chat_id\),\n"
                  r"                    console_name=console_name_for\(all_tools\)\)",
                  "", 1),
    # The section names a door even when the chat has none: a name the
    # model cannot call is a name it invents.
    "hardcode": (FILEDOOR,
                 r"    if not door_names:\n        return \"\"\n",
                 "    if not door_names:\n"
                 "        door_names = (\"sandbox_read_file\",)\n", 1),
    # The registry looks for a tool that is not the door, so the door is
    # never found and the section is never built.
    "wrongbare": (MCP_REGISTRY,
                  r"            if bare != MCP_FILE_DOOR_TOOL:\n",
                  "            if bare != \"read_text\":\n", 1),
    # The tool list is not consulted: a door the model cannot call is
    # named anyway.
    "narrow": (LLM_TOOLS,
               r"        if name in shown\n",
               "        if True\n", 1),
    # The section loses the sentence that says the console is not a
    # door -- which is the sentence that pulls the model off the
    # console.
    "nowhole": (FILEDOOR,
                r"    console = \(f\"The console \(`\{console_name\}`\) is not "
                r"a file door: its \"\n",
                "    console = (f\"The console (`{console_name}`) is a \"\n"
                "               \"tool like any other: its \"\n", 1),
    # The door's own description loses the bot's line: the model's
    # decision is made where the tool is described, and the server's
    # words send the model to a bash the bot does not import.
    "nodesc": (MCP_REGISTRY,
               r"    if str\(tool\.get\(\"name\"\) or \"\"\) == "
               r"MCP_FILE_DOOR_TOOL:\n",
               "    if False:\n", 1),
    # The console's cut carries no notice: the model finds out that
    # the output was cut and finds no door in the message, so it keeps
    # measuring the channel -- which is what it did on production on
    # 2026-11-01, 23:00-23:20 MSK.
    "nocutnotice": (BASH_COMMAND,
                    r"        if _output_was_cut\(out, stdout_text\):\n",
                    "        if False:\n", 1),
    # The notice names no door: the model is told the output is a
    # fragment and is not told what to do about it.
    "cutnodname": (BASH_COMMAND,
                   r"imported tool whose name ends in `_read_file` -- "
                   r"which hands you ",
                   "imported tool whose name ends in `_read_anything` -- "
                   "which hands you ", 1),
    # The section loses the sentence that says the server is the
    # arbiter -- which is the sentence that stops the model from
    # deciding in advance that the file will not fit.
    "noarbiter": (FILEDOOR,
                  r"        \"refusal tells you the size and the limit\. Do not decide in \"\n"
                  r"        \"advance that a file will not fit: a window size, a limit or an \"\n"
                  r"        \"estimate quoted in the chat is not the server's answer, and \"\n"
                  r"        \"the only way to know is to call the door\. Compare the file's \"\n",
                  "        \"refusal tells you the size and the limit. \"\n"
                  "        \"Compare the file's \"\n", 1),
}

#: The mutations themselves: (name, expectation, ops).
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the section is not in the prompt", "CAUGHT",
     [BEHAVIOUR["nosection"]]),
    ("the section names a door the chat does not have", "CAUGHT",
     [BEHAVIOUR["hardcode"]]),
    ("the registry looks for a tool that is not the door", "CAUGHT",
     [BEHAVIOUR["wrongbare"]]),
    ("the tool list is not consulted", "CAUGHT",
     [BEHAVIOUR["narrow"]]),
    ("the section loses the console sentence", "CAUGHT",
     [BEHAVIOUR["nowhole"]]),
    ("the section loses the arbiter sentence", "CAUGHT",
     [BEHAVIOUR["noarbiter"]]),
    ("the door's description loses the bot's line", "CAUGHT",
     [BEHAVIOUR["nodesc"]]),
    ("the console's cut carries no notice", "CAUGHT",
     [BEHAVIOUR["nocutnotice"]]),
    ("the console's notice names no door", "CAUGHT",
     [BEHAVIOUR["cutnodname"]]),

    # --- group B: the tripwires -------------------------------------
    ("the tripwires are gone, and the section is not in the prompt",
     "SURVIVED", _group_b(CAUGHT_BY["nosection"], BEHAVIOUR["nosection"])),
    ("the tripwire is gone, and the section names a door the chat does "
     "not have", "SURVIVED",
     _group_b(CAUGHT_BY["hardcode"], BEHAVIOUR["hardcode"])),
    ("the tripwires are gone, and the registry looks for the wrong tool",
     "SURVIVED",
     _group_b(CAUGHT_BY["wrongbare"], BEHAVIOUR["wrongbare"])),
    ("the tripwire is gone, and the tool list is not consulted",
     "SURVIVED", _group_b(CAUGHT_BY["narrow"], BEHAVIOUR["narrow"])),
    ("the tripwire is gone, and the section loses the console sentence",
     "SURVIVED", _group_b(CAUGHT_BY["nowhole"], BEHAVIOUR["nowhole"])),
    ("the tripwire is gone, and the section loses the arbiter sentence",
     "SURVIVED", _group_b(CAUGHT_BY["noarbiter"], BEHAVIOUR["noarbiter"])),
    ("the tripwire is gone, and the door's description loses the bot's "
     "line", "SURVIVED",
     _group_b(CAUGHT_BY["nodesc"], BEHAVIOUR["nodesc"])),
    ("the tripwires are gone, and the console's cut carries no notice",
     "SURVIVED",
     _group_b(CAUGHT_BY["nocutnotice"], BEHAVIOUR["nocutnotice"])),
    ("the tripwire is gone, and the console's notice names no door",
     "SURVIVED",
     _group_b(CAUGHT_BY["cutnodname"], BEHAVIOUR["cutnodname"])),
]


def _command():
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
    print(f"runner: {sys.executable}")
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
