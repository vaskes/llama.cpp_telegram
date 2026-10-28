#!/usr/bin/env python3
"""Mutation harness for T70.5 — chunked summaries (2026-10-24).

The operator's decision is a set of rules in code:

  * the old history is summarised in CHUNKS, not in one lump;
  * a chunk below the volume budget is not summarised at all — it is
    loaded raw;
  * a chunk that WAS summarised is marked in the database, and a marked
    chunk is never summarised again;
  * the context holds the finished summaries and the messages that were
    never summarised — and nothing else;
  * a summary stands in the place of its own chunk, so the model reads
    the timeline in order;
  * a failed chunk stays unmarked: its messages are loaded raw and it is
    retried;
  * the volumes are budgets: the raw tail, the old part as a whole and
    the fresh window are all capped.

Every one of those is a rule that a test can turn red. This harness
breaks each of them on purpose and requires the suite to notice, because
a rule in code that nothing can catch is a rule that has stopped being a
rule.

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
test image instead.

    python3 scripts/mutation_harness_t70_5.py [ROOT]
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
    # The chunking, the marker and the assembly.
    "tests/test_t70_5_chunked_summaries.py",
    # The model call, the failure paths and the trigger placement.
    "tests/test_t70_summarize.py",
    # The pure assembler and the budgets.
    "tests/test_t70_context_budget.py",
    # The storage: the transaction, the marker, the orphan.
    "tests/test_storage.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

ART = ROOT / "telegram-bot" / "context_budget.py"
#: The summary code moved out of `storage.py` into `storage_summaries.py`
#: when the storage module was split. The anchor still pointed at the
#: old file, found nothing there, and the mutation was REFUSED -- so
#: the harness counted it as neither caught nor survived and the proof
#: was green and empty. The code is unchanged; the path is.
STO = ROOT / "telegram-bot" / "storage_summaries.py"
SUM = ROOT / "telegram-bot" / "summarize.py"
REA = ROOT / "telegram-bot" / "reanchor.py"

# (name, path, [(pattern, replacement, expected sites)])
MUTATIONS = [
    # --- the marker -----------------------------------------------------
    ("the chunk is not marked in the database", STO, [
        (r"            c\.execute\(\n"
         r"                \"UPDATE messages SET summary_id = \? \"\n"
         r"                \"WHERE chat_id = \? AND thread_id = \? \"\n"
         r"                \"AND id >= \? AND id <= \? AND summary_id IS NULL\",\n"
         r"                \(summary_id, chat_id, thread_id, first_msg_id, last_msg_id\),\n"
         r"            \)\n",
         "            pass\n", 1),
    ]),
    ("the summary row is not written", STO, [
        (r"            cur = c\.execute\(\n"
         r"                \"INSERT INTO summaries \(chat_id, thread_id, text, n_messages, \"\n",
         "            cur = c.execute(\n"
         "                \"INSERT INTO summaries (chat_id, thread_id, text, n_messages, \"\n"
         "                \"' || 'x' || '\", 0, 0, 0,\n", 1),
    ]),
    ("the marked messages are loaded raw as well", ART, [
        (r"            if sid is not None and sid in summary_by_id:\n"
         r"                # The summary stands for this message\. Loading both would\n"
         r"                # tell the model the same stretch of history twice\.\n"
         r"                continue\n",
         "            pass\n", 1),
    ]),
    ("the marked messages are not excluded from the raw tail", STO, [
        (r"                  AND \(summary_id IS NULL OR summary_id NOT IN \(\n"
         r"                        SELECT id FROM summaries\n"
         r"                        WHERE chat_id = \? AND thread_id = \?\n"
         r"                          AND last_created_at < \?\)\)\n",
         "                  AND 1 = 1\n", 1),
    ]),
    # --- the volume rule -------------------------------------------------
    ("the old part is summarised even below the budget", SUM, [
        (r"        if pending_tokens <= budget\.summary_tail_tokens:\n",
         "        if False:\n", 1),
    ]),
    ("the old part is never summarised", SUM, [
        (r"        if pending_tokens <= budget\.summary_tail_tokens:\n",
         "        if True:\n", 1),
    ]),
    # --- the chunking ----------------------------------------------------
    ("the chunking drops a message", SUM, [
        (r"        current\.append\(m\)\n        current_tokens \+= tokens\n",
         "        current_tokens += tokens\n", 1),
    ]),
    # --- the assembly ----------------------------------------------------
    ("the summaries are not used at all", ART, [
        (r"        for s in summary_by_id\.values\(\):\n",
         "        for s in []:\n", 1),
    ]),
    ("the summary is not placed in the position of its chunk", ART, [
        (r"        units\.sort\(key=lambda u: u\[\"order\"\]\)\n",
         "        units.sort(key=lambda u: 0)\n", 1),
    ]),
    ("the raw tail is not capped", ART, [
        (r"                if raw_tokens \+ unit\[\"tokens\"\] > self\.summary_tail_tokens:\n",
         "                if False:\n", 1),
    ]),
    ("the old part is not capped", ART, [
        (r"            if total_tokens \+ unit\[\"tokens\"\] > self\.summary_context_tokens:\n",
         "            if False:\n", 1),
    ]),
    ("the fresh window loses its message cap", ART, [
        (r"            fresh = fresh\[-self\.max_messages:\]\n",
         "            pass\n", 1),
    ]),
    # --- the failure path -------------------------------------------------
    ("a failed chunk is marked anyway", SUM, [
        (r"            if not text:\n"
         r"                # Leave the chunk unmarked: it stays raw in the context and\n"
         r"                # is retried at the next re-anchor\.\n"
         r"                failed \+= 1\n"
         r"                continue\n",
         "            if not text:\n"
         "                failed += 1\n"
         "                text = \"сводка\"\n", 1),
    ]),
    # --- the trigger placement -------------------------------------------
    # The rule is "the summarisation happens at the re-anchor, not at
    # process start and not per request". Breaking the CALL is what the
    # rule is about -- the early return for a disabled age filter is an
    # optimisation, not a rule, and mutating it changed nothing.
    ("the re-anchor stops summarising", REA, [
        (r"        await summarise_after_reanchor\(\)\n",
         "        pass\n", 1),
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
