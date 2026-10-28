#!/usr/bin/env python3
"""Mutation harness for the answer-files feature (2026-10-24).

When the model answers with a document, the document has to arrive as a
Telegram document, and three things that look like documents must not.
Every one of those is a rule in code, and a rule in code that no test
can turn red is a rule that has stopped being a rule.

So this harness breaks each rule on purpose and requires the suite to
notice. The rules it breaks:

  * the executed-tool trace is not a file (a ```bash fence whose lines
    all start with "$ ");
  * the fence tag decides the extension, and an unknown tag is .txt;
  * a document with no fence around it is still a document;
  * the name of a file is found above the block, in a comment on its
    first line, or in the prose that follows it;
  * a fence that does not start a line is still a fence;
  * an unterminated fence is a fragment, not a file;
  * the code leaves the prose, or it is in the chat twice;
  * a diagnostic carries no files -- its reasoning tail is not a
    deliverable;
  * the executed-tool trace is still in front of the answer;
  * a file that cannot be uploaded goes out as text;
  * the topic id goes with the document;
  * every handler sends the files of its own answer;
  * the bot remembers the code it just sent -- the chat gets the prose,
    the database keeps the whole answer;
  * the files go out even when there is no prose, so an answer that is
    nothing but a document still reaches the user.

The rules this harness holds itself to, because a mutation run that
cannot report INVALID is a decoration:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the mutated file is restored whatever happens, and the restore is
    verified byte for byte.

Where it runs: with the current interpreter by default, so it works in
a checkout. Set MUTATION_HARNESS_IMAGE to run the same tests inside the
test image instead (the image has the dependencies; a bare checkout may
not).

    python3 scripts/mutation_harness_artifact.py [ROOT]
"""
import ast
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

TARGETS = [
    "tests/test_llm_artifact.py",
    "tests/test_llm_files.py",
    "tests/test_llm_result.py",
    # The persistence of the answer: the chat gets the prose, the bot
    # remembers the whole answer. The rule lives in handlers.py and is
    # tested in the file that owns the handler tail -- a harness that
    # did not run it would report a survivor it never looked for.
    "tests/test_handler_tail.py",
    # The seam: the real handler, the real parser, the real sender, the
    # real database, with the engine replaced by the answer shapes that
    # were measured on the live engine. A harness that did not run it
    # would never look at whether the handler wires the feature
    # together -- the part the unit tests cannot see.
    "tests/test_artifact_e2e.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

NL = chr(10)

ART = ROOT / "telegram-bot" / "llm_artifact.py"
RES = ROOT / "telegram-bot" / "llm_result.py"
FILES = ROOT / "telegram-bot" / "llm_files.py"
HAND = ROOT / "telegram-bot" / "handlers.py"

# (name, path, [(pattern, replacement, expected sites)])
MUTATIONS = [
    ("the executed-tool trace becomes a file", ART, [
        (r"        if _is_tool_trace\(block\):\n            continue\n",
         "        if False and _is_tool_trace(block):\n            continue\n", 2),
    ]),
    ("the fence tag stops deciding the extension", ART, [
        (r'    "json": "\.json", "jsonc": "\.json", "json5": "\.json",\n',
         '    "jsonc": ".json", "json5": ".json",\n', 1),
    ]),
    ("a document with no fence is not a document", ART, [
        (r"    raw = _raw_document\(stripped\)\n",
         "    raw = None\n", 1),
    ]),
    ("the name in the prose after the block is not looked for", ART, [
        (r"    if name is None:\n        name = _name_after\(after\)\n",
         "    if name is None:\n        name = None\n", 1),
    ]),
    ("the comment on the first line is not looked for", ART, [
        (r"    if name is None:\n        name = _name_in_comment\(block\)\n",
         "    if name is None:\n        name = None\n", 1),
    ]),
    ("the name above the block is not looked for", ART, [
        (r"    name = _name_above\(before\)\n",
         "    name = None\n", 1),
    ]),
    ("a fence that does not start a line is not a fence", ART, [
        (r'    return _INLINE_FENCE_RE\.sub\("\\n```", text\)\.splitlines\(\)\n',
         "    return text.splitlines()\n", 1),
    ]),
    ("an unterminated fence becomes a file", ART, [
        (r"        if close >= count:\n            break\n",
         "        if False:\n            pass\n", 1),
    ]),
    ("the code stays in the prose as well as in the file", ART, [
        (r"        removed\.update\(range\(block\.start, block\.end \+ 1\)\)\n",
         "        pass\n", 1),
    ]),
    ("a diagnostic starts carrying files", RES, [
        (r"    if not isinstance\(result, dict\):\n"
         r"        return Answer\(result, \[\], result if isinstance\(result, str\) else \"\"\)\n",
         "    if not isinstance(result, dict):\n"
         "        prose, files = extract_files(result)\n"
         "        return Answer(prose, files, result)\n", 1),
    ]),
    ("the executed-tool trace is no longer in front of the answer", RES, [
        # The trace that goes into the chat is built apart from the raw
        # one now -- the delivery and the memory are two artefacts --
        # so the variable is named chat_trace and the anchor that
        # expected the old name found nothing: the mutation was
        # REFUSED.
        (r'                if chat_trace:\n'
         r'                    prose = chat_trace \+ "\\n\\n" \+ prose\n',
         "                if chat_trace:\n"
         "                    pass\n", 1),
    ]),
    ("a file that cannot be uploaded is dropped", FILES, [
        (r"            await _send_as_text\(update, artifact, kwargs\)\n"
         r"            return\n",
         "            return\n", 1),
    ]),
    ("the topic id does not go with the document", FILES, [
        (r"    if thread_id is None:\n        return \{\}\n",
         "    return {}\n", 1),
    ]),
    ("the chat gets the code as well as the document", HAND, [
        # The answer is put in a variable first now and the function
        # returns that variable, so the anchor that expected a bare
        # "return answer.text" found nothing: the mutation was
        # REFUSED. The defect it puts back is the same one: the chat
        # gets the raw log -- the console noise -- instead of the prose.
        (r"    sent = answer\.text\n",
         "    sent = answer.raw\n", 1),
    ]),
    ("the handlers send no files", HAND, [
        (r"answer\.files", "[]", 4),
    ]),
    ("the bot forgets the code it just sent", HAND, [
        (r"        context, update, chat_id, thread_id, answer\.raw, rating_active,\n",
         "        context, update, chat_id, thread_id, answer.text, rating_active,\n", 1),
    ]),
    # The files have to go out even when there is no prose: an answer
    # that is nothing but a document has no prose to send, and a guard
    # on the prose drops exactly those files. Two mutations, because
    # the two pairs of handlers sit at different indentation levels.
    ("the files only go out when there is prose (photo, voice)", HAND, [
        # The handlers pass the thinking message to send_files now, so
        # the anchor that expected the two-argument call matched
        # nothing: the mutation was REFUSED, and a REFUSED mutation is
        # neither caught nor survived.
        (r"(?m)^        await send_files\(update, answer\.files, thinking=thinking\)$",
         "            await send_files(update, answer.files)", 2),
    ]),
    ("the files only go out when there is prose (text, document)", HAND, [
        (r"(?m)^            await send_files\(update, answer\.files, thinking=thinking\)$",
         "                await send_files(update, answer.files)", 2),
    ]),
]


def _restore(path: Path, before: bytes):
    path.write_bytes(before)
    assert path.read_bytes() == before, f"{path}: the restore did not take"


def _emergency_restore(signum, _frame):
    for path, before in list(_MUTATING.items()):
        try:
            path.write_bytes(before)
        except OSError:
            pass
    print(f"\n  RESTORED {len(_MUTATING)} file(s) on signal {signum}")
    sys.exit(128 + signum)


_MUTATING = {}

for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_sig, _emergency_restore)


def _command():
    if IMAGE:
        return ["docker", "run", "--rm", "-v", f"{ROOT}:/work", "-w", "/work",
                IMAGE, "python", "-m", "pytest", *TARGETS, "-q",
                "-p", "no:cacheprovider"]
    return [sys.executable, "-m", "pytest", *TARGETS, "-q",
            "-p", "no:cacheprovider"]


def run_tests():
    """('pass' | 'fail' | 'hang', summary)."""
    try:
        proc = subprocess.run(_command(), cwd=str(ROOT), capture_output=True,
                              text=True, timeout=SUITE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "hang", "the suite did not finish"
    out = proc.stdout + proc.stderr
    tail = [line for line in out.splitlines()
            if " passed" in line or " failed" in line or "error" in line.lower()]
    return ("fail" if proc.returncode != 0 else "pass"), \
           (tail[-1].strip() if tail else "(no summary)")


def main():
    print(f"root: {ROOT}")
    print(f"runner: {'docker ' + IMAGE if IMAGE else sys.executable}")
    print("=== preflight on the CLEAN tree ===", flush=True)
    verdict, summary = run_tests()
    print("  ", summary, flush=True)
    if verdict != "pass":
        print("PREFLIGHT FAILED -- the harness would be measuring itself")
        return 2

    caught = survivors = refused = invalid = 0
    for name, path, ops in MUTATIONS:
        before = path.read_bytes()
        text = before.decode("utf-8")
        mutated, total = text, 0
        try:
            for pattern, replacement, expect in ops:
                found = list(re.finditer(pattern, mutated, re.M))
                if len(found) != expect:
                    refused += 1
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed -- that is how a harness reports a pass "
                          f"it never measured.", flush=True)
                    mutated = None
                    break
                total += len(found)
                mutated = re.sub(pattern, replacement.replace("\\", "\\\\"),
                                 mutated, flags=re.M)
            if mutated is None:
                continue
            if mutated == text:
                invalid += 1
                print(f"  INVALID {name}: the substitution produced identical "
                      f"text", flush=True)
                continue

            _MUTATING[str(path)] = before
            path.write_text(mutated, encoding="utf-8")
            if path.read_bytes() == before:
                _restore(path, before)
                invalid += 1
                print(f"  INVALID {name}: the file is unchanged after the "
                      f"write", flush=True)
                continue

            try:
                compile(mutated, str(path), "exec")
            except SyntaxError as e:
                invalid += 1
                print(f"  INVALID {name}: does not compile -- it caught "
                      f"nothing ({e})", flush=True)
                continue

            verdict, summary = run_tests()
            if verdict == "pass":
                survivors += 1
                print(f"  SURVIVED {name}  [{total} site(s)]  {summary}",
                      flush=True)
            else:
                caught += 1
                word = "CAUGHT" if verdict == "fail" else "CAUGHT-BY-HANG"
                print(f"  {word} {name}  [{total} site(s)]  {summary}",
                      flush=True)
        finally:
            _restore(path, before)
            _MUTATING.pop(str(path), None)

    print(f"\n{caught} caught, {survivors} survived, {refused} refused, "
          f"{invalid} invalid  ({len(MUTATIONS)} mutations)", flush=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if (survivors or refused or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
