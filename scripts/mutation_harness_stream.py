#!/usr/bin/env python3
"""Mutation harness for TECH-DEBT #8 block 3 (T3.2).

Same three rules as scripts/mutation_harness.py: `expect` per operation,
a mutation that does not compile is INVALID rather than SURVIVED, a
mutation that hangs the suite is a catch, and the harness restores the
tree even when it is killed.

These are the guards in the branch that nothing drove for as long as it
was inline in call_llama -- which is why block C2 could delete all three
SSE framing guards with a green suite. Each one is worth a mutation here
precisely because the tests were written after the code moved, and a
test written after the fact is exactly the kind that cannot fail.
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
TARGETS = "tests/test_llm_stream.py tests/test_c2_sse.py tests/test_c4_thinking.py"
PER_TEST_TIMEOUT = 150
MARKER = Path("/tmp/mutharness_stream.state")
_mutating = {}

S = ROOT / "telegram-bot" / "llm_stream.py"
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


def _terminal_to_inferred(m):
    """`is_terminal=True` -> the old inference from the value.

    This is the whole point of the outcome type. Both aborts return
    None, and a caller written against `terminal is not None` treats
    them as continuations.
    """
    return m.group(0).replace("is_terminal=True, ", "")


# The fallback's carry-through return, identified by the append_msg= that
# only that return has. Written as a named constant because an inline
# repr() of the pattern is how the backslashes got doubled once already.
CARRY = (r"(\n *)tool_calls_buf=tool_calls_buf,\n"
         r"( *)finish_reason=finish_reason,\n"
         r"( *)saw_unavailable_tool=saw_unavailable_tool,\n"
         r"( *)final_fallback=final_fallback,\n"
         r"( *)append_msg=msg\)")

MUTATIONS = [
    # The whole point of the outcome type. Both aborts return None, so
    # a caller asking `terminal is not None` treats them as
    # continuations and answers a cancelled question with the previous
    # iteration's buffers.
    ("the pre-stream abort stops declaring itself terminal", S, [
        (r"(abort_event set pre-stream[\s\S]*?return StreamOutcome\()"
         r"is_terminal=True, ",
         lambda m: m.group(1), 1)]),
    ("the between-chunk abort stops declaring itself terminal", S, [
        (r"(abort_event set between chunks[\s\S]*?return StreamOutcome\()"
         r"is_terminal=True, ",
         lambda m: m.group(1), 1)]),
    ("the data: prefix guard is gone", S, [
        (r' *if not line or not line\.startswith\("data: "\):\n'
         r' *continue\n',
         "", 1)]),
    ("the [DONE] sentinel is parsed as a chunk instead of breaking", S, [
        (r' *if payload\.strip\(\) == "\[DONE\]":\n *break\n',
         "", 1)]),
    ("a malformed chunk raises instead of being skipped", S, [
        (r" *try:\n *chunk = json\.loads\(payload\)\n"
         r" *except json\.JSONDecodeError:\n *continue\n",
         "                chunk = json.loads(payload)\n", 1)]),
    ("the fallback stops classifying a context overflow", S, [
        (r" *if is_context_overflow\(\n"
         r" *json\.dumps\(data\.get\(\"error\"\) or data\)\[:500\]\):\n"
         r" *log\.info\(\"\[call_llama\] CONTEXT OVERFLOW on fallback\"\)\n"
         r" *return StreamOutcome\(is_terminal=True,\n"
         r" *terminal=OVERFLOW_MESSAGE\)\n",
         "", 1)]),
    # The carry-through return appears twice in the module; these two
    # target the fallback's, anchored on the append_msg= that follows it.
    ("the fallback drops the tool-call map it was handed", S, [
        (CARRY,
         lambda m: (m.group(1) + m.group(2) + "finish_reason=finish_reason," + NL
                    + m.group(3) + "saw_unavailable_tool=saw_unavailable_tool," + NL
                    + m.group(4) + "final_fallback=final_fallback," + NL
                    + m.group(5) + "append_msg=msg)"), 1)]),
    ("the fallback drops the finish_reason it was handed", S, [
        (CARRY,
         lambda m: (m.group(1) + m.group(2) + "tool_calls_buf=tool_calls_buf," + NL
                    + m.group(3) + "saw_unavailable_tool=saw_unavailable_tool," + NL
                    + m.group(4) + "final_fallback=final_fallback," + NL
                    + m.group(5) + "append_msg=msg)"), 1)]),

    ("the final reasoning flush stops forcing", S, [
        (r" *throttle=throttle, force=True\)",
         "                    throttle=throttle)", 1)]),
    ("the reasoning push stops passing throttle through", S, [
        (r"(await push_thinking\(\n *accumulated_reasoning \+ acc\.reasoning,\n"
         r" *thinking_msg=thinking_msg, silent=silent,\n *)throttle=throttle\)",
         lambda m: m.group(1) + "throttle=None)", 1)]),
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
        backup = Path("/tmp/mut_stream.bak")
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
                invalid += 1
                print(f"  INVALID {name}: substitution was a no-op", flush=True)
                continue
            path.write_text(mutated, encoding="utf-8")
            comp = subprocess.run(
                [sys.executable, "-c",
                 f"import ast;ast.parse(open({str(path)!r}).read())"],
                capture_output=True, text=True)
            if comp.returncode != 0:
                invalid += 1
                print(f"  INVALID {name}: does not compile -- caught nothing",
                      flush=True)
                continue
            verdict, summary = run_tests()
            if verdict == "pass":
                survived += 1
                print(f"  SURVIVED {name}  [{total}]  {summary}", flush=True)
            else:
                caught += 1
                w = "CAUGHT" if verdict == "fail" else "CAUGHT-BY-HANG"
                print(f"  {w} {name}  [{total}]  {summary}", flush=True)
        finally:
            restore(path, before)
            _mutating.pop(str(path), None)
            _write_marker()

    print(f"\n{caught} caught, {survived} survived, {refused} refused, "
          f"{invalid} invalid  ({len(MUTATIONS)} mutations)", flush=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())