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

#: The tree the harness runs against. It used to be the production
#: path, hardcoded -- so the harness could only ever run on the
#: production host, and the anchors that pointed into it read as
#: unreadable everywhere else. A harness that can only prove things on
#: one machine proves nothing in a checkout.
DEFAULT_ROOT = "/opt/telegrambot/bot/repo"
ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT)
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")
#: The suite the mutations are measured against. The notice test is
#: here because the sentence the user reads is guarded there: without
#: it, a mutation of that sentence was caught by nothing at all.
TARGETS = ("tests/test_nonstream_error.py "
           "tests/test_engine_failure_message.py")
PER_TEST_TIMEOUT = 150
MARKER = Path("/tmp/mutharness_stream.state")
_mutating = {}

S = ROOT / "telegram-bot" / "llm_nonstream.py"
#: The sentence the user reads when the engine fails is built here.
NOTICE = ROOT / "telegram-bot" / "user_notice.py"
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
    ("a 500 that offered tools stops retrying without them", S, [
        (r"retry = status == 500 and bool\(req_body\.get\(\"tools\"\)\)",
         "retry = False", 1)]),
    ("a non-500 starts retrying without tools", S, [
        (r"retry = status == 500 and bool\(req_body\.get\(\"tools\"\)\)",
         "retry = bool(req_body.get(\"tools\"))", 1)]),
    ("a request with no tools starts retrying", S, [
        (r"retry = status == 500 and bool\(req_body\.get\(\"tools\"\)\)",
         "retry = status == 500", 1)]),
    ("an exception with no response raises instead of being classified", S, [
        (r" *response = getattr\(exc, \"response\", None\)\n"
         r" *status = getattr\(response, \"status_code\", None\)\n"
         r" *body = response\.text\[:500\] if response is not None else \"\"\n",
         "    response = exc.response\n"
         "    status = response.status_code\n"
         "    body = response.text[:500]\n", 1)]),
    ("the retry path also carries a message", S, [
        # The sentence the user reads is built by the notice module now
        # -- it picks one of three wordings by the kind of failure --
        # so the anchor that expected the inline f-string found
        # nothing: the mutation was REFUSED. The defect it puts back
        # is the same one: a retry carries a message to the user.
        (r'        message="" if retry else engine_failure_message\(exc, status=status\),',
         '        message=engine_failure_message(exc, status=status),', 1)]),
    ("the failure message loses the exception text", NOTICE, [
        # The sentence the user reads comes from the notice module now,
        # and the module picks one of three wordings by the kind of
        # failure. The mutation puts back the defect it is named for:
        # the user gets a message that says nothing about the failure
        # instead of the one that names it.
        (r'    return ENGINE_ERROR_MESSAGE\n',
         '    return GENERIC_FAILURE_MESSAGE\n', 1)]),
    ("the body is no longer truncated", S, [
        (r'body = response\.text\[:500\] if response is not None else ""',
         "body = response.text if response is not None else \"\"", 1)]),
]


def run_tests():
    try:
        if IMAGE:
            r = subprocess.run(
                f"rm -f /tmp/mut.db && docker run --rm "
                f"-v {ROOT}:{ROOT} -w {ROOT} "
                f"-e BOT_TOKEN=1:fake -e CONVERSATIONS_DB=/tmp/mut.db "
                f"{IMAGE} python -m pytest {TARGETS} -q --tb=line -p no:cacheprovider",
                shell=True, capture_output=True, text=True,
                timeout=PER_TEST_TIMEOUT)
        else:
            # The checkout is enough: the suite runs with the current
            # interpreter. The harness used to require the docker image
            # unconditionally, so it could only run on the production
            # host -- and a proof that can only run on one machine is
            # not a proof, it is a ritual.
            env = dict(os.environ, BOT_TOKEN="1:fake",
                       CONVERSATIONS_DB="/tmp/mut.db")
            r = subprocess.run(
                [sys.executable, "-m", "pytest", *TARGETS.split(),
                 "-q", "--tb=line", "-p", "no:cacheprovider"],
                cwd=str(ROOT), capture_output=True, text=True,
                timeout=PER_TEST_TIMEOUT, env=env)
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