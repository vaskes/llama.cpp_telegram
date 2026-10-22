#!/usr/bin/env python3
"""Mutation harness for the T1.1 selftest extractions.

Every other mutation harness in this repo asks whether a guard was
removed. This one asks a different question, and it is the one the
extraction can actually fail.

`selftest_golden.log` pins the sequence of markers `_selftest` emits.
That catches a block that stopped running. It does not catch a block
that still runs and now reports a *different* verdict, because the
mutation below replaces an expectation with a wrong one and the
sequence of marker *lines* is unchanged -- the text of each line
changes instead, and the golden does hold that.

So the targets here are the extracted leaf modules, and the mutation
that matters is the one that keeps every line the golden looks for
while inverting what the block claims. A harness that only deleted
code would report `15/15 caught` and prove nothing about this class.

Two rules carried over from `scripts/mutation_harness.py`, because both
were learned by being wrong:

  * `n_sub == n_expected` is checked BEFORE substitution. A drifted
    anchor is REFUSED, not scored. Six of twelve wave-1 anchors had
    rotted and the old harness credited all six as clean deletions.
  * a mutation that does not compile is INVALID, not SURVIVED. And a
    mutation the target hangs on is CAUGHT-BY-HANG, which is the
    loudest signal available -- not a harness crash.

Usage:

    python scripts/mutation_harness_selftest.py [/opt/telegrambot/bot/repo]
"""
import atexit
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = "/opt/telegrambot/bot/repo"
ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT)
IMAGE = "bot-testimg311"
# The runtime golden plus the direct block tests: the golden watches the
# blocks run in order, the block tests watch each one being addressable
# and self-contained. Neither alone catches a block that still runs and
# now reports the wrong thing -- which is what the mutations below do.
TARGETS = "tests/test_selftest_runtime.py tests/test_selftest_blocks.py"
PER_TEST_TIMEOUT = 300

A = ROOT / "telegram-bot" / "selftest_access.py"
H = ROOT / "telegram-bot" / "selftest_handlers.py"
S = ROOT / "telegram-bot" / "selftest_storage.py"
L = ROOT / "telegram-bot" / "selftest_llm.py"
I = ROOT / "telegram-bot" / "selftest_infra.py"
# `selftest_llm`'s blocks are driven one at a time, not through run_all,
# because the handlers/storage summaries sit between its first three and
# the golden pins the sequence. So the seam mutation for it is on
# selftest.py: drop one positional call.
ST = ROOT / "telegram-bot" / "selftest.py"
NL = chr(10)


# --- crash safety -----------------------------------------------------

MARKER = Path("/tmp/mutharness_selftest.state")
_mutating = {}


def _write_marker():
    if _mutating:
        MARKER.write_text("\n".join(f"{k}\t{v}" for k, v in _mutating.items()),
                          encoding="utf-8")
    else:
        MARKER.unlink(missing_ok=True)


def recover_from_marker():
    if not MARKER.exists():
        return
    for line in MARKER.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        path, backup = line.split("\t", 1)
        try:
            Path(path).write_bytes(Path(backup).read_bytes())
            print(f"  RECOVERED {path} from {backup}", flush=True)
        except OSError as e:
            print(f"  COULD NOT RECOVER {path}: {e}", flush=True)
    MARKER.unlink(missing_ok=True)


def restore(path: Path, before: bytes):
    path.write_bytes(before)
    assert path.read_bytes() == before, f"{path}: restore did not take"


def _emergency_restore(signum, _frame):
    for path, backup in list(_mutating.items()):
        try:
            Path(path).write_bytes(Path(backup).read_bytes())
            print(f"\n  RESTORED {path} on signal {signum}", flush=True)
        except OSError:
            pass
    _write_marker()
    os._exit(128 + signum)


for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_sig, _emergency_restore)
atexit.register(_write_marker)


# --- the mutations ----------------------------------------------------
#
# Each one has to keep the code compiling and keep every marker line the
# golden matches. Where a mutation would merely delete a block, that is
# already covered by `test_selftest_runtime`; these are the ones that
# change what a block *claims*.

MUTATIONS = [
    # `mute_ok &= ok` is the whole verdict of the noise-filter block.
    # The obvious mutation is `|=`, and it SURVIVES -- correctly. Every
    # case passes on a clean tree, so `&=` and `|=` agree on the result
    # and no target can tell them apart. A mutation that is equivalent to
    # the code on the only tree anybody runs measures nothing, and calling
    # that a weak test would be the wrong lesson: the test is fine, the
    # mutation is vacuous.
    #
    # So this one breaks an expectation as well. Inverting one case and
    # weakening the accumulation together is the only way to ask the
    # question the first mutation could not: when a case really fails,
    # does the verdict still report it?
    ("noise filter: a case fails and the accumulation is weakened",
     A, [
         (r"(_FakeFromUser\(True,\s*'ClaudeBot'\), 'whatever',\s*group_chat,\s*)True\)",
          r"\1False)", 1),
         (r"( *)(mute_ok &= ok\n)", r"\1mute_ok |= ok\n", 1)]),

    # Same shape in the reply-to block: invert one expectation AND make
    # only the last case decide. With a real failure in the middle, a
    # passing last case has to be visible as a FAIL -- if the block still
    # reports "all pass", the verdict is not wired to the cases at all.
    ("reply-to: a case fails and only the last case decides",
     A, [
         (r'(\"reply to another human in group\", \"supergroup\", True, 10, 20, \"agreed\", )True\)',
          r'\1False)', 1),
         (r'( *)(if got != expected:\n'
          r' *log\.warning\(f"  \[FAIL\] \{label\}: got=\{got\} expected=\{expected\}"\)\n'
          r'( *)rpl_ok = False\n)',
          r"\1\2\3pass\n", 1),
     ]),

    # The verdict line of each block is the only thing that separates
    # "the block ran and passed" from "the block never ran". Deleting
    # it keeps every [OK]/[FAIL] marker and loses the summary, which is
    # a golden diff -- but the point of the mutation is that the block
    # still returns True, so the direct block test is the thing that
    # notices the summary is gone.
    ("noise filter: the summary line is dropped", A, [
        (r"( *)(log\.warning\(f\"\[selftest\] noise-filter tests:[^\n]*\n)",
         r"\1pass\n", 1)]),

    ("reply-to: the summary line is dropped", A, [
        (r"( *)(log\.warning\(f\"\[selftest\] reply-to-other-user tests:[^\n]*\n)",
         r"\1pass\n", 1)]),

    # `run_all` is the seam between the module and `_selftest`, and it is
    # four lines. This is the same class as removing `.run()` from
    # `call_llama`, which survived 333 tests on the T8.1 extraction
    # because every test drove `run()` directly and none could see
    # whether its caller called it.
    #
    # The `return all_ok` anchor has to name `run_all` explicitly: this
    # module has one per `check_*` plus one in the driver, and the first
    # version of this harness expected 1 site and found 2 -- which is the
    # expect-guard working, not a failure of the mutation.
    ("access run_all returns True instead of the accumulated verdict",
     A, [
         (r"( *)(all_ok &= await check_reply_to_other_user\(\)\n)",
          r"\1pass\n", 1),
         (r"(f\"\{'all pass' if all_ok else 'FAILED'\}\"\)\n)( *)return all_ok",
          r"\1\2return True", 1)]),

    # `return all_ok` inside a check becomes a constant, with a case
    # inverted so the constant is actually wrong. Every marker is still
    # logged -- including the `[FAIL]` line for the inverted case -- so
    # the golden cannot see this at all: only a test that reads the
    # verdict can, which is what `test_every_block_passes_when_run_alone`
    # is for.
    #
    # The first version of this mutation replaced the return on its own
    # and SURVIVED, correctly. On a clean tree every case passes, so
    # `all_ok` is True and `return True` is the same value; the mutation
    # was vacuous, not the test weak. Breaking an expectation as well is
    # what makes the question answerable: when a case really fails, does
    # what the block returns say so?
    ("noise filter: a case fails and the return ignores it", A, [
         (r"(_FakeFromUser\(True,\s*'ClaudeBot'\), 'whatever',\s*group_chat,\s*)True\)",
          r"\1False)", 1),
         (r"(?s)(async def check_noise_filter\(\) -> bool:.*?\n)( *)(return all_ok\n)",
          r"\1\2return True\n", 1)]),

    # The false label that lived at the end of the storage rating
    # round-trip, corrected by this slice. Reverting it is the honest
    # check that the golden is really holding the label text and not
    # just "a summary line exists here".
    ("storage rating round-trip: the false group-mode label is back",
     S, [
         (r"log\.warning\(f\"\[selftest\] storage rating round-trip: \"",
          'log.warning(f"[selftest] group-mode tests: "', 1)]),

    # `_should_mute_in_group` called on the wrong object. Every case still
    # runs and every marker still fires, so this changes the verdict
    # rather than the volume of output.
    ("noise filter: the filter is called on the chat instead of the update",
     A, [
         (r"got = _should_mute_in_group\(upd\)", "got = _should_mute_in_group(chat)", 1)]),

    # The seam a positional driver can lose. Removing `.run()` from
    # `call_llama` returned a ToolLoop instead of an answer and left 333
    # tests green; this is the same shape on the other side of an
    # extraction. Dropping the call means the block runs nowhere, its
    # markers leave the golden -- which is a diff, not a failure, so
    # without `test_every_block_is_awaited_by_selftest` it would be read
    # and accepted.
    ("a positional selftest_llm block is never awaited", ST, [
        (r" *await selftest_llm\.check_react_to_message\(\)\n", "", 1)]),

    # NOTE: two mutations that used to live here were removed after the
    # first run, and the reason is the most useful thing this harness
    # has to say.
    #
    # "a positional block's verdict is discarded" and "the all_ok
    # initialiser is no longer an assignment" both SURVIVED. The
    # verdict one did so for the ordinary reason -- on a clean tree every
    # block returns True, so dropping an `&=` changes nothing.
    #
    # But asking why it was vacuous at all is what found the real thing:
    # `all_ok` in `_selftest` was written twelve times and read zero
    # times. It had been write-only before T1.1 too -- the smoke test
    # has no exit code and reports by logging -- but it *looked*
    # load-bearing, and that is exactly how the storage summary came to
    # read the aggregate of everything before it and claim to be the
    # group-mode tests.
    #
    # So the flag is gone rather than given a reader. Every leaf now
    # returns its own verdict AND logs its own summary line, and the
    # golden pins those lines, so an aggregate would restate what is
    # already held. `test_every_block_is_awaited_by_selftest` replaces
    # the direction the flag used to provide.

    # The one that actually shipped during this extraction. A block that
    # carries two sections -- `check_group_context` runs the F2/NEW-2
    # global LLM semaphore and then the GROUP_CONTEXT injection -- has
    # two `all_ok &=` lines, and the builder converted only the last one
    # into the return value. The earlier assignment was then to a name
    # that had never been bound inside the function, which is an
    # UnboundLocalError at the first `all_ok &= sem_ok`.
    #
    # It is in this harness because nothing structural can see it: the
    # name is a local of a function, so no AST check resolves it, and the
    # golden only notices because the selftest process dies half way
    # through. A mutation that deletes the initialiser reproduces exactly
    # that, and the runtime test is what catches it.
    ("a check_* uses all_ok without initialising it", L, [
        (r"(async def check_group_context\(\) -> bool:\n)    all_ok = True\n",
         r"\1", 1)]),

    # Same shape, different block: the per-handler abort_event key
    # assigns `aek_ok` twice through a try/except and the verdict line
    # is the only thing that returns it. Deleting it leaves the block
    # running and reporting every marker, and returning None -- which
    # reads as "failed" to `all_ok &=` and as "no opinion" to anyone
    # reading the block.
    #
    # The summary line sits between the assignment and the return, which
    # is the shape the builder produces for every block here. The first
    # version of this anchor assumed the two were adjacent and was
    # REFUSED for finding 0 sites -- which is the guard working, not a
    # broken mutation.
    ("infra: the abort_event key block returns nothing", I, [
        (r"( *)(all_ok = aek_ok\n)([^\n]*\n)( *)return all_ok\n",
         r"\1\2\3", 1)]),

    # NOT SCORED, and recorded here because a mutation that cannot be
    # made to red is worse than no mutation: it reads as coverage.
    #
    # "the donsetch env guard is removed" survived, and correctly. The
    # harness runs with DONSETCH_URL pointed at a stub, so the guard's
    # early return never fires and removing it changes nothing here. It
    # IS reachable -- `test_every_block_passes_when_run_alone` drives
    # this block in a subprocess with neither DONSETCH_URL nor
    # LLAMABOT_SELFTEST set, and in that environment removing the guard
    # turns a skip into a NameError on `donsetch_call`.
    #
    # Answering it would mean a second harness run with the variable
    # unset, and the golden pins the markers this block emits, so the
    # environments are not interchangeable. Dropped rather than counted.
]


def run_tests():
    """Run the target tests in the image. Returns (verdict, summary)."""
    try:
        r = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{ROOT}:/repo", "-w", "/repo",
             "-e", "BOT_TOKEN=1:fake",
             "-e", f"CONVERSATIONS_DB=/tmp/mh_selftest_{os.getpid()}.db",
             IMAGE,
             "python", "-m", "pytest", *TARGETS.split(),
             "-q", "--tb=line", "-p", "no:cacheprovider"],
            capture_output=True, text=True, timeout=PER_TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "hang", f"(timeout after {PER_TEST_TIMEOUT}s)"
    tail = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
    return ("fail" if r.returncode != 0 else "pass"), \
           (tail[-1] if tail else "(no summary)")


def main():
    recover_from_marker()
    print("=== preflight on the CLEAN tree ===", flush=True)
    verdict, summary = run_tests()
    print("  ", summary, flush=True)
    if verdict != "pass":
        print("PREFLIGHT FAILED -- the harness would be measuring itself")
        return 2
    if verdict == "hang":
        print("PREFLIGHT HUNG -- refusing to start")
        return 2

    caught = survivors = refused = invalid = 0
    for name, path, ops in MUTATIONS:
        before = path.read_bytes()
        text = before.decode("utf-8")
        backup = Path(f"/tmp/mhselftest.{path.name}.bak")
        backup.write_bytes(before)
        _mutating[str(path)] = str(backup)
        _write_marker()

        mutated, total = text, 0
        try:
            for pattern, repl, expect in ops:
                found = list(re.finditer(pattern, mutated, re.M))
                if len(found) != expect:
                    refused += 1
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed.", flush=True)
                    mutated = None
                    break
                total += len(found)
                mutated = re.sub(pattern, repl, mutated, flags=re.M)

            if mutated is None:
                continue
            if mutated == text:
                refused += 1
                print(f"  REFUSED {name}: substitution produced no change",
                      flush=True)
                continue

            # A mutation that does not compile measures nothing, and
            # reporting it as SURVIVED would be a lie about coverage.
            try:
                compile(mutated, str(path), "exec")
            except SyntaxError as e:
                invalid += 1
                print(f"  INVALID  {name}: does not compile ({e.msg})",
                      flush=True)
                continue

            path.write_text(mutated, encoding="utf-8")
            verdict, summary = run_tests()
            if verdict == "hang":
                caught += 1
                print(f"  CAUGHT-BY-HANG {name}: the tests did not finish",
                      flush=True)
            elif verdict == "fail":
                caught += 1
                print(f"  CAUGHT  {name}  ({summary})", flush=True)
            else:
                survivors += 1
                print(f"  SURVIVED {name}  <-- the target does not see this",
                      flush=True)
        finally:
            restore(path, before)
            _mutating.pop(str(path), None)
            _write_marker()

    print(f"\n=== {len(MUTATIONS)} mutations: {caught} caught, "
          f"{survivors} survived, {refused} refused, {invalid} invalid",
          flush=True)
    return 0 if survivors == 0 and refused == 0 and invalid == 0 else 1


if __name__ == "__main__":
    sys.exit(main())