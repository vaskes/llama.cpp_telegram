#!/usr/bin/env python3
"""Mutation harness for the transcript budget (CODEBOOK X-27) and the
one-message-per-turn rule (X-28).

Same rules as the other harnesses: `expect` per operation and the
count is checked BEFORE substitution, a mutation that does not compile
is INVALID rather than SURVIVED, a mutation that hangs the suite is a
catch, and the tree is restored even when the harness is killed.

Why this block needs its own. The two defects it covers are the ones
that only show up at SCALE, and a test written against a small example
proves nothing about them:

  * the transcript had a cap per command and no cap on the whole thing,
    so its size grew with the number of commands the model ran
    (production, 2026-10-28: 44 commands -> 77 044 characters -> 22
    chat messages in front of a 4 110-character answer);
  * a send that timed out was retried with the same text, and
    `sendMessage` has no idempotency key, so the retry was a COPY
    (production, the same day: 8 of the 22 messages delivered twice).

So the mutations here target the two seams where the scale is decided:
the call site that hands the budget to the renderer, and the branch
that decides what to do with a timeout.
"""
import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

# The repository root, wherever this file is run from. It used to be
# hard-coded to /opt/telegrambot/bot/repo -- a path that stopped
# existing when the production host lost its git clone (2026-10-28,
# the "a release is the deployable unit" refactor). A harness that
# cannot find the tree it is supposed to mutate measures nothing.
ROOT = Path(__file__).resolve().parent.parent
IMAGE = "bot-testimg311"
TARGETS = ("tests/test_transcript_budget.py tests/test_t48_timeout.py "
           "tests/test_limits.py tests/test_t44.py tests/test_llm_result.py")
PER_TEST_TIMEOUT = 240
MARKER = Path("/tmp/mutharness_transcript.state")
_mutating = {}

L = ROOT / "telegram-bot" / "llm_result.py"
H = ROOT / "telegram-bot" / "handlers.py"
LIM = ROOT / "telegram-bot" / "limits.py"


def _write_marker():
    if _mutating:
        MARKER.write_text("\n".join(f"{k}\t{v}" for k, v in _mutating.items()))
    else:
        MARKER.unlink(missing_ok=True)


def recover():
    if not MARKER.exists():
        return
    for line in MARKER.read_text().splitlines():
        if "\t" not in line:
            continue
        path, backup = line.split("\t", 1)
        try:
            Path(path).write_bytes(Path(backup).read_bytes())
            print(f"  RECOVERED {path}", flush=True)
        except OSError as e:
            print(f"  COULD NOT RECOVER {path}: {e}", flush=True)
    MARKER.unlink(missing_ok=True)


def restore(path, before):
    path.write_bytes(before)
    assert path.read_bytes() == before


def _emergency(signum, _f):
    for path, backup in list(_mutating.items()):
        try:
            Path(path).write_bytes(Path(backup).read_bytes())
            print(f"\n  RESTORED {path} on signal {signum}", flush=True)
        except OSError:
            pass
    _write_marker()
    os._exit(128 + signum)


for _s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_s, _emergency)
atexit.register(_write_marker)


MUTATIONS = [
    # ---- X-27: the chat's copy of the transcript ----------------------
    # The call site. This is the line that makes the difference between
    # "the chat gets the whole transcript" and "the chat gets a bounded
    # one": without it the flood is back, and the tests that measure
    # the size in chat messages are the ones that catch it.
    ("the chat's copy stops being bounded", L, [
        (r"executed, budget=limits\.MAX_TRANSCRIPT_CHARS\)",
         "executed)", 1)]),
    # The renderer. Same defect one level down: the function that
    # knows how to bound a transcript is asked not to.
    ("the renderer stops bounding the transcript", L, [
        (r"head, tail, dropped = _fit_blocks\(blocks, budget\)",
         "head, tail, dropped = blocks, [], 0", 1)]),
    # The head. The first commands are what the turn started with; a
    # cut that keeps only the tail is a cut that hides the beginning.
    ("the head of the command list is dropped", L, [
        (r"head\.append\(b\)", "pass", 1)]),
    # The tail. The last commands are what the turn ended with, and
    # the part the model quotes back in its answer.
    ("the tail of the command list is dropped", L, [
        (r"for i in range\(len\(blocks\) - 1, len\(head\) - 1, -1\):",
         "for i in range(len(head) - 1, len(head) - 1, -1):", 1)]),
    # The marker. A cut that says nothing about its own size is a cut
    # the reader cannot trust.
    ("the drop is not marked", L, [
        (r" *if dropped:\n *lines\.append\(COMMANDS_DROPPED\.format"
         r"\(dropped=dropped,\n *total=len\(blocks\)\)\)\n", "", 1)]),
    # The marker's number. A marker that says "0 commands are not
    # shown" while thirty of them are gone is worse than no marker.
    ("the marker lies about how many commands were dropped", L, [
        (r"lines\.append\(COMMANDS_DROPPED\.format\(dropped=dropped,",
         "lines.append(COMMANDS_DROPPED.format(dropped=0,", 1)]),
    # The remembered copy. Delivery and memory are two different
    # artefacts: bounding the memory too would lose what a command
    # printed, and the next turn would have no answer to "what did it
    # say?".
    ("the remembered trace is bounded too", L, [
        (r"trace = _format_executed_tools\(executed\)\n",
         "trace = _format_executed_tools(\n"
         "            executed, budget=limits.MAX_TRANSCRIPT_CHARS)\n", 1)]),
    # A zero budget means "no transcript in the chat" -- the operator's
    # way of saying the chat wants the answer alone.
    ("a zero budget still produces a transcript", L, [
        (r" *if budget is not None and budget <= 0:\n *return \"\"\n", "", 1)]),
    # The per-command cap. One command that printed a megabyte must
    # not become a megabyte-long message.
    ("the per-command output cap is dropped", L, [
        (r"out = condense\(out, MAX_TRANSCRIPT_OUTPUT_CHARS\)", "out = out", 1)]),
    # The setting itself. A budget that is a constant in the code is
    # the same defect X-26 was about, one file over.
    ("the transcript budget moves off the setting", LIM, [
        (r"MAX_TRANSCRIPT_CHARS = _env_or\(\"LLAMABOT_MAX_TRANSCRIPT_CHARS\", 4000\)",
         "MAX_TRANSCRIPT_CHARS = 4000", 1)]),

    # ---- X-28: one user turn, one message ----------------------------
    # The branch that used to retry. `sendMessage` has no idempotency
    # key: resending a message Telegram already accepted delivers it
    # again under a new id. Measured: 8 copies in one turn.
    ("a timed-out send is repeated", H, [
        (r" *log\.info\(f\"\[_reply\] TimedOut on attempt \{attempt\+1\}/3: \"\n"
         r" *\"Telegram most likely delivered it; not resending\"\)\n *return None\n",
         "", 1)]),
    # The other way round: a timeout that raises turns one user turn
    # into a delivered answer AND an error message.
    ("a timed-out send raises", H, [
        (r" *log\.info\(f\"\[_reply\] TimedOut on attempt \{attempt\+1\}/3: \"\n"
         r" *\"Telegram most likely delivered it; not resending\"\)\n *return None\n",
         "            raise\n", 1)]),
]


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def run_tests():
    """Run the harness's targets, in the container if there is one.

    The container is the production-like environment (the clean
    CONVERSATIONS_DB, the fake token, the 3.11 interpreter). Where
    there is no Docker -- a development sandbox, a laptop -- the same
    targets run locally with the same environment, because a harness
    that can only run in one place is a harness that is not run.
    """
    env = dict(os.environ)
    env.update({"BOT_TOKEN": "1:fake", "CONVERSATIONS_DB": "/tmp/mut.db"})
    if _docker_available():
        try:
            r = subprocess.run(
                f"rm -f /tmp/mut.db && docker run --rm "
                f"-v {ROOT}:{ROOT} -w {ROOT} "
                f"-e BOT_TOKEN=1:fake -e CONVERSATIONS_DB=/tmp/mut.db "
                f"{IMAGE} python -m pytest {TARGETS} -q --tb=line -p no:cacheprovider",
                shell=True, capture_output=True, text=True,
                timeout=PER_TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "hang", "the suite did not finish"
    else:
        Path("/tmp/mut.db").unlink(missing_ok=True)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", *TARGETS.split(),
                 "-q", "--tb=line", "-p no:cacheprovider"],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
                timeout=PER_TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "hang", "the suite did not finish"
    out = r.stdout + r.stderr
    tail = [l for l in out.splitlines() if "passed" in l or "failed" in l]
    return ("fail" if r.returncode != 0 else "pass"), \
           (tail[-1] if tail else "(no summary)")


def main():
    recover()
    verdict, summary = run_tests()
    print("=== preflight on the CLEAN tree ===", flush=True)
    print("  ", summary, flush=True)
    if verdict != "pass":
        print("PREFLIGHT FAILED -- the harness would be measuring itself")
        return 2

    caught = survived = refused = invalid = 0
    for name, path, ops in MUTATIONS:
        before = path.read_bytes()
        text = before.decode("utf-8")
        backup = Path(f"/tmp/mut_transcript_{path.stem}.bak")
        backup.write_bytes(before)
        _mutating[str(path)] = str(backup)
        _write_marker()
        mutated, total = text, 0
        try:
            for pattern, repl, expect in ops:
                found = list(re.finditer(pattern, mutated, re.M))
                if len(found) != expect:
                    refused += 1
                    print(f"  REFUSED {name}: expected {expect}, found "
                          f"{len(found)} -- refusing to score a mutation "
                          f"that did not apply where claimed", flush=True)
                    mutated = None
                    break
                total += len(found)
                mutated = re.sub(pattern, repl, mutated, flags=re.M)
            if mutated is None:
                continue
            if mutated == text:
                refused += 1
                print(f"  REFUSED {name}: substitution changed nothing",
                      flush=True)
                continue
            try:
                compile(mutated, str(path), "exec")
            except SyntaxError as e:
                invalid += 1
                print(f"  INVALID  {name}: does not compile ({e.msg} L{e.lineno})",
                      flush=True)
                continue
            path.write_text(mutated)
            verdict, summary = run_tests()
            if verdict == "hang":
                caught += 1
                print(f"  CAUGHT-BY-HANG  {name}", flush=True)
            elif verdict == "fail":
                caught += 1
                print(f"  CAUGHT  {name}  ({summary})", flush=True)
            else:
                survived += 1
                print(f"  SURVIVED  {name}  ({summary})", flush=True)
        finally:
            restore(path, before)
            _mutating.pop(str(path), None)
            _write_marker()

    print(f"\n=== {len(MUTATIONS)} mutations: "
          f"{caught} caught, {survived} survived, "
          f"{refused} refused, {invalid} invalid ===", flush=True)
    return 1 if (survived or refused or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
