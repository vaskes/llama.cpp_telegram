#!/usr/bin/env python3
"""Mutation harness for the wave-1/2/3 blockers. TECH-DEBT #8 T0.4.

Three things this does that the previous version did not, each of which
is a way it had already lied.

**1. `expect` per operation.** Every regex declares how many sites it is
supposed to match. If the count differs the mutation is REFUSED, not
scored. That is T0.4's "n_sub == n_expected before py_compile;
otherwise refuse, not credit".

The bug it closes, from wave 3: an anchor pinned to a 12-space indent
matched `handle_photo` and silently did nothing in `handle_document`
and `handle_text`, which sat at 16 after a reindent. The script printed
"removed in 3 sites" -- the number it FOUND -- compiled, and reported
1353 passed on a tree where blocker 1 was still alive.

Measured on this tree the moment the harness was rewritten: SIX of the
twelve anchors had drifted and every one was refused. Three found 0
sites, one found 7 where it declared 3, two matched text that no longer
exists. Under the old harness all six scored normally.

My first attempt at an automatic no-op check was three predicates,
each falsified against this file:

  * "the anchor is still present afterwards" -- an INSERT mutation
    re-emits its own anchor by construction, so it flagged two working
    wave-2 mutations as dead;
  * "the anchor is absent afterwards" -- flags every insertion;
  * "the pattern is not in the rendered replacement" -- rendered text
    cannot contain a regex source, so it fired on all twelve.

All three ask the wrong question. Whether a mutation applied is not a
question about the text afterwards; it is whether the count is the one
the author intended, and that is answerable before substituting.

**2. INVALID is its own outcome, and a hang is a catch.** A mutation
that does not compile caught nothing -- reporting it as SURVIVED is how
this harness lied before. A mutation that makes the suite HANG is the
most emphatic possible catch: removing the per-user release leaves five
handlers waiting forever on a limit of two.

**3. Crash safety.** This one was learned by breaking it. The harness
writes a mutated file, runs the suite, restores. I killed it with
`pkill` mid-run and it left `sem.release()` replaced by `pass` in
handlers.py -- which hangs every concurrent test and looks exactly like
a regression caused by the day's refactor. It was not.

Three layers, because SIGKILL cannot be caught: try/finally around
every mutation, signal handlers that restore and exit, and a marker
file naming the mutated path plus its backup so recovery survives a
kill -9. The harness restores anything a previous run left behind
before its own preflight.

Every `expect` below is a measured count, not a remembered one. Four
handlers carry a retry block, not three, and seven `_format_executed_
tools(` call sites exist where three were assumed.
"""
import ast
import atexit
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = "/opt/telegrambot/bot/repo"
ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT)
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")
TARGETS = "tests/test_blockers.py"# Short on purpose: a mutation that hangs must be scored within a
# bounded time rather than stalling the whole harness.
PER_TEST_TIMEOUT = 150

H = ROOT / "telegram-bot" / "handlers.py"
#: The unwrap of call_llama's return value -- one form, used by the
#: primary path and the retry alike.
R = ROOT / "telegram-bot" / "llm_result.py"
T = ROOT / "telegram-bot" / "turn.py"
NL = chr(10)


# --- crash safety -----------------------------------------------------

MARKER = Path("/tmp/mutharness.state")
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

def _drop_guard(m):
    """The check is removed; the call stays.

    Group 1 is the call, group 2 the check including its `return`. The
    `pass` takes the indent of the `if` it replaces.
    """
    check = m.group(2)
    indent = check[:len(check) - len(check.lstrip())]
    return m.group(1) + indent + "pass" + NL


#: The four handlers used to carry their own copy of the guard, and
#: each mutation removed the copy of one of them. The copies are gone:
#: the thinking message is opened by one helper now, and each handler
#: checks the result itself. So the mutation is still per-handler -- it
#: removes the check of one handler -- but the site it removes is the
#: `if opened is None` after that handler's call.
GUARD = r"handle_photo"
GUARD_V = r"handle_voice"
GUARD_D = r"handle_document"
GUARD_T = r"handle_text"   # not an f-string


def guard_pattern(label):
    """Group 1 is the call, group 2 the whole check including `return`."""
    return (r"( *opened = await _open_thinking\(update, label='" + label
            + r"'\)\n)"
            r"( *if opened is None:\n"
            r" *return\n)")


MUTATIONS = [
    # The four handlers used to carry their own copy of the unwrap, and
    # the mutation removed all four. There is one copy now --
    # `unwrap_answer` in llm_result.py -- so the mutation removes that
    # one. The defect is the same: the dict is treated as the text.
    ("the retry treats call_llama's dict as text (wave 1 blocker)", R, [
        (r"    text = result\.get\(\"text\"\) or \"\"\n",
         "    text = result\n",
         1),
    ]),
    ("the retry drops the executed-tools preamble (wave 1 blocker)", R, [
        # The trace reaches the chat through the preamble the unwrap
        # puts in front of the prose. The mutation used to drop the
        # preamble where it was assembled -- four copies, one per
        # handler -- and the assembly moved: there is one copy now,
        # and the chat's copy is the one the tripwire looks at. So the
        # mutation drops the chat's preamble, which is the defect it is
        # named for: the chat answers without the tool trace.
        (r"( *if chat_trace:\n)"
         r"( *prose = chat_trace \+ \"\\n\\n\" \+ prose\n)",
         # `pass` takes the indent of the line it replaces -- the body,
         # one level deeper than the `if`. Putting it at the `if`'s own
         # indent is an IndentationError, and the harness scores that
         # INVALID rather than measuring anything.
         lambda m: (m.group(1)
                    + m.group(2).split("prose =")[0] + "pass" + NL),
         1),
    ]),
    # The retry path builds its preamble from `_retry_exec`, which the
    # mutation above does not touch. Four sites, three sites, and it
    # assigns `_retry_text` where the other path assigns `bot_response`.
    # They read the same in a diff and are not the same code -- which is
    # why an anchor written against one found nothing in the other.
    # The retry path used to carry its own copy of the unwrap, built
    # from `_retry_exec` and assigning `_retry_text`. There is one copy
    # now -- `unwrap_answer` serves the primary path and the retry
    # alike -- so a mutation of "the retry's own preamble" has no
    # separate site to remove: it would be the same mutation as the
    # one above, measuring the same thing twice and calling it two
    # proofs. The retry's own guard is
    # test_retry_keeps_the_executed_tools_preamble, and it is that
    # tripwire which the mutation above has to turn red.
    ("thinking-is-None guard removed (handle_photo)", H, [
        (guard_pattern(GUARD), _drop_guard, 1)]),
    ("thinking-is-None guard removed (handle_voice)", H, [
        (guard_pattern(GUARD_V), _drop_guard, 1)]),
    ("thinking-is-None guard removed (handle_document)", H, [
        (guard_pattern(GUARD_D), _drop_guard, 1)]),
    ("thinking-is-None guard removed (handle_text)", H, [
        (guard_pattern(GUARD_T), _drop_guard, 1)]),
    ("the per-user slot is never released (wave 2 blocker)", H, [
        (r"(?P<ind> *)# 2026-10-22: released at the outermost exit[^\n]*\n"
         r"(?:(?P=ind)[^\n]*\n)*?(?P=ind)sem\.release\(\)\n",
         lambda m: m.group("ind") + "pass" + NL,
         2),
    ]),
    ("a second release is inserted (over-release on a BoundedSemaphore)", H, [
        # Three sites, two word orders: "is only registered" in one
        # handler, "is registered only" in the other two. An anchor that
        # named the prose of either would match exactly one and, under
        # the old harness, be reported as if it had removed both.
        (r"( *)(# B1: the abort event is (?:only registered|registered "
         r"only) when a thinking[^\n]*\n)",
         lambda m: (m.group(1) + "try:" + NL
                    + m.group(1) + "    sem.release()" + NL
                    + m.group(1) + "except Exception:" + NL
                    + m.group(1) + "    pass" + NL
                    + m.group(1) + m.group(2)),
         3),
    ]),
    ("rollback goes back to 'delete the newest user row'", T, [
        (r"( *)deleted = await asyncio\.to_thread\(\n"
         r"( *)store\.delete_message_ids, self\.chat_id,\n"
         r"( *)self\.thread_id, self\._row_ids,\n( *)\)",
         lambda m: (m.group(1) + "deleted = await asyncio.to_thread(" + NL
                    + m.group(2) + "store.delete_last_user_message, self.chat_id," + NL
                    + m.group(2) + "self.thread_id," + NL + m.group(4) + ")"),
         1),
    ]),
    ("persist stops recording the row id", T, [
        (r"( *)if row_id is not None:\n( *)(self\._row_ids\.append\(int\(row_id\)\))",
         lambda m: m.group(1) + "if False:" + NL + m.group(2) + m.group(3),
         1),
    ]),
    ("rollback reports True when it deleted nothing", T, [
        (r"( *)if not deleted:\n(?:[ ]*[^\n]*\n)*?[ ]*return False\n",
         lambda m: m.group(1) + "if False:" + NL + m.group(1) + "    pass" + NL,
         1),
    ]),
    ("Stop no longer discards a response that already has text", H, [
        # `if False:` keeps the existing body and its indentation, so
        # this anchor needs only the one line. An earlier version also
        # matched the v0.8.0 comment that follows -- three handlers have
        # it and handle_voice does not, so it could never reach four.
        (r"( *)if bot_response is None or abort_event\.is_set\(\):\n",
         lambda m: m.group(1) + "if False:" + NL,
         4),
    ]),
]


# --- running ----------------------------------------------------------

def _run_once():
    if IMAGE:
        return subprocess.run(
            f"rm -f /tmp/mut.db && docker run --rm "
            f"-v {ROOT}:{ROOT} -w {ROOT} "
            f"-e BOT_TOKEN=1:fake -e CONVERSATIONS_DB=/tmp/mut.db "
            f"{IMAGE} python -m pytest {TARGETS} -q --tb=line -p no:cacheprovider",
            shell=True, capture_output=True, text=True,
            timeout=PER_TEST_TIMEOUT)
    # The checkout is enough: the suite runs with the current
    # interpreter. The harness used to require the docker image
    # unconditionally, so it could only run on the production host --
    # and a proof that can only run on one machine is not a proof, it
    # is a ritual.
    env = dict(os.environ, BOT_TOKEN="1:fake", CONVERSATIONS_DB="/tmp/mut.db")
    return subprocess.run(
        [sys.executable, "-m", "pytest", *TARGETS.split(),
         "-q", "--tb=line", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True,
        timeout=PER_TEST_TIMEOUT, env=env)


def run_tests():
    """('pass' | 'fail' | 'hang', summary)."""
    try:
        r = _run_once()
    except subprocess.TimeoutExpired:
        return "hang", "the suite did not finish"
    out = r.stdout + r.stderr
    tail = [l for l in out.splitlines() if "passed" in l or "failed" in l]
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

    caught = survivors = refused = invalid = 0
    for name, path, ops in MUTATIONS:
        before = path.read_bytes()
        text = before.decode("utf-8")
        backup = Path(f"/tmp/mutharness.{path.name}.bak")
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
                          f"claimed -- this is exactly the wave-3 failure.",
                          flush=True)
                    mutated = None
                    break
                total += len(found)
                mutated = re.sub(pattern, repl, mutated, flags=re.M)

            if mutated is None:
                continue
            if mutated == text:
                invalid += 1
                print(f"  INVALID {name}: substitution produced identical text",
                      flush=True)
                continue

            path.write_text(mutated, encoding="utf-8")
            if path.read_bytes() == before:
                restore(path, before)
                invalid += 1
                print(f"  INVALID {name}: file unchanged after write", flush=True)
                continue

            comp = subprocess.run(
                [sys.executable, "-c",
                 f"import ast;ast.parse(open({str(path)!r}).read())"],
                capture_output=True, text=True)
            if comp.returncode != 0:
                invalid += 1
                print(f"  INVALID {name}: does not compile -- it caught "
                      f"nothing", flush=True)
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
            restore(path, before)
            _mutating.pop(str(path), None)
            _write_marker()

    print(f"\n{caught} caught, {survivors} survived, {refused} refused, "
          f"{invalid} invalid  ({len(MUTATIONS)} mutations)", flush=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())