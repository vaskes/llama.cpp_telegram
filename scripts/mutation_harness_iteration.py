#!/usr/bin/env python3
"""Mutation harness for TECH-DEBT #8 T8.1 ("an iteration is a named unit").

Same three rules as the two earlier harnesses: `expect` per operation
and the count is checked BEFORE substitution, a mutation that does not
compile is INVALID rather than SURVIVED, a mutation that hangs the suite
is a catch, and the tree is restored even when the harness is killed.

Why this block needs its own. Every other tripwire in this repo was
written against `call_llama` as one function: "the loop checks
`is_terminal`", "the iteration falls off its end with a value", "the
shutdown guard is above the first POST". The extraction split that
function in two, and the tests were re-pointed by hand -- which is
exactly the situation where a test written after the fact passes for the
wrong reason. A test that was merely re-pointed to `ToolLoop.run_one`
proves that `run_one` exists and that the pointer was updated. It does
not prove the loop still reads the value the iteration decided on.

So these mutations target the seam: the four lines of `run()` that turn
an `IterationResult` into a turn's answer, and the guards inside
`run_one` that nothing outside the method can see.
"""
import atexit
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path("/opt/telegrambot/bot/repo")
IMAGE = "bot-testimg311"
TARGETS = ("tests/test_llm_iteration.py tests/test_llm_stream.py "
           "tests/test_loop_flag_order.py tests/test_c3_loop_guard.py "
           "tests/test_c4_thinking.py tests/test_d1_toolargs.py "
           "tests/test_d3_finish.py tests/test_docs_current.py")
PER_TEST_TIMEOUT = 240
MARKER = Path("/tmp/mutharness_iteration.state")
_mutating = {}

I = ROOT / "telegram-bot" / "llm_iteration.py"
C = ROOT / "telegram-bot" / "call_llama.py"
NL = chr(10)


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


RUN = re.escape("            if result.is_terminal:\n"
                "                return result.terminal\n")

MUTATIONS = [
    # ---- the seam: run() reading the iteration's decision -------------
    # The single most valuable line in the refactor. Two of the exits
    # carry terminal=None on purpose (shutdown, Stop button), so the
    # inference this mutation restores treats both as "keep going" and
    # ships the previous iteration's buffers to a user who cancelled.
    ("the loop infers termination from the value again", I, [
        (r" *if result\.is_terminal:\n *return result\.terminal\n",
         "            if result.terminal is not None:\n"
         "                return result.terminal\n", 1)]),
    ("the loop stops reading the iteration's decision at all", I, [
        (r" *if result\.is_terminal:\n *return result\.terminal\n",
         "", 1)]),
    ("a terminal iteration is answered with None", I, [
        (r" *if result\.is_terminal:\n *return result\.terminal\n",
         "            if result.is_terminal:\n"
         "                return None\n", 1)]),
    # The iteration cap. 15 was chosen; 16 turns a 10s hang into a
    # longer one and no test notices until production. The cap is a
    # setting now (limits.MAX_ITER, LLAMABOT_MAX_ITER) -- the
    # mutation moves it off the setting and onto a literal, which is
    # the same defect wearing a different hat: a deployment that
    # raises the knob would silently keep the old cap.
    ("the iteration cap moves off the setting", I, [
        (r"for iteration in range\(limits\.MAX_ITER\):", "for iteration in range(50):", 1)]),
    # The carry-out after the loop. Removing it turns every exhausted
    # loop into a canned "too many iterations" for a user who was two
    # tokens away from an answer.
    ("the exhausted loop forgets its last fallback", I, [
        (r" *if self\.final_fallback:\n *return self\.final_fallback\n", "", 1)]),
    ("the exhausted loop returns the fallback before the cap", I, [
        (r"for iteration in range\(limits\.MAX_ITER\):",
         "if self.final_fallback:\n            return self.final_fallback\n"
         "        for iteration in range(limits.MAX_ITER):", 1)]),

    # ---- run_one's own guards ----------------------------------------
    # Both exits here carry a real payload; a mutation that turns one
    # into a continuation turns a cancelled turn into a long one.
    ("the shutdown exit stops ending the turn", I, [
        (r" *return IterationResult\.done\(\n"
         r" *'\[bot: shutdown requested, aborting tool loop\]'\)",
         "            return IterationResult.again()", 1)]),
    ("the Stop-button exit stops ending the turn", I, [
        (r" *return IterationResult\.done\(None\)",
         "            return IterationResult.again()", 1)]),
    ("the shutdown guard stops reading the event", I, [
        (r" *if _sd is not None and _sd\.is_set\(\):",
         "        if False:", 1)]),
    # The two flags had a production outage each: `saw_unavailable_tool`
    # was reset below its first set-site, and D2 read it above its
    # initialisation and raised UnboundLocalError on every turn.
    ("the unavailable-tool flag stops being reset per iteration", I, [
        (r" *saw_unavailable_tool = False\n", "", 1)]),
    ("the real-result flag stops being reset per iteration", I, [
        (r" *iter_had_real_result = False\n", "", 1)]),
    # The trailing `again()` is what stops `run()` from reading
    # `.is_terminal` on None. Without it the loop raises AttributeError
    # instead of carrying on.
    ("the iteration falls off its end instead of declaring itself", I, [
        (r" *return IterationResult\.again\(\)\n", "", 1)]),
    # call_llama must still await. An un-awaited ToolLoop.run() returns
    # a coroutine, and `coroutine.is_terminal` is not an attribute --
    # so this one dies loudly, which is why it is here: it proves the
    # await is load-bearing and not decorative.
    ("call_llama stops awaiting the loop", C, [
        (r"custom_tools=CUSTOM_TOOLS,\n *\)\.run\(\)", "custom_tools=CUSTOM_TOOLS\n    )", 1)]),

    # ---- the contract of the named type ------------------------------
    ("the outcome type stops defaulting to 'not terminal'", I, [
        (r" *is_terminal: bool = False\n", "", 1)]),
    ("done() stops saying it is terminal", I, [
        (r"return cls\(is_terminal=True, terminal=value\)",
         "return cls(terminal=value)", 1)]),
]


def run_tests():
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
        backup = Path(f"/tmp/mut_iter_{path.stem}.bak")
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
    return 1 if survived or refused else 0


if __name__ == "__main__":
    sys.exit(main())