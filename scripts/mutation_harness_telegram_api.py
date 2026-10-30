#!/usr/bin/env python3
"""Mutation harness for the platform's file ceiling (2026-10-30).

GitHub's code scanning filed alert #5 on 2026-10-29: CodeQL's
``py/incomplete-url-substring-sanitization`` on ``config.py``. The check
that decides whether the bot is talking to Telegram's public API was a
substring check on an unparsed URL:

    if TELEGRAM_API.startswith("https://api.telegram.org"):

That string is a HOST, and the check could not tell Telegram from a
host that merely starts with the same characters:
``https://api.telegram.org.evil.example/bot`` passes it. The endpoint
the bot actually talks to has no platform ceiling of its own, so the
wrong answer is the wrong LIMIT on a document the operator is offered.

The host is parsed now. This harness breaks the parsing on purpose and
asks the suite to notice.

**Group A -- the behaviour.** Each mutation puts one of the defects
back, and the suite has to notice:

  * the host is read out of the string again;
  * the host is the whole URL;
  * a URL that cannot be parsed takes the bot down;
  * the platform ceiling is applied to every endpoint.

**Group B -- the tripwires.** The mutation removes the tripwires *and*
the behaviour they are supposed to catch, and the suite has to go
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

    python3 scripts/mutation_harness_telegram_api.py [ROOT]
"""
import atexit
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tests that guard this rule, and the neighbours of the path it
#: touches -- a green group B is only a proof if the whole surface is
#: in the run.
TARGETS = (
    "tests/test_telegram_api_host.py "
    "tests/test_telegram_api_base_url.py "
    "tests/test_limits.py"
)

PER_TEST_TIMEOUT = 300
MARKER = Path("/tmp/mutharness_telegram_api.state")
_mutating = {}

CONFIG = ROOT / "telegram-bot" / "config.py"
TEST_HOST = ROOT / "tests" / "test_telegram_api_host.py"

#: The tripwires of this rule, silenced one by one in group B.
TRIPWIRES = [
    "test_the_public_api_carries_the_platforms_own_ceiling",
    "test_a_host_that_only_starts_like_telegram_is_not_telegram",
    "test_a_path_that_mentions_telegram_is_not_telegram",
    "test_a_local_bot_api_server_has_no_platform_ceiling",
    "test_the_smoke_tests_mock_server_has_no_platform_ceiling",
    "test_no_api_at_all_has_no_platform_ceiling",
    "test_the_host_is_read_case_insensitively",
    "test_a_url_that_cannot_be_parsed_has_no_host",
]


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
            path.write_bytes(Path(backup).read_bytes())
            print(f"\n  RESTORED {path} on signal {signum}", flush=True)
        except OSError:
            pass
    _write_marker()
    os._exit(128 + signum)


for _s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_s, _emergency)
atexit.register(_write_marker)


def _silence(name):
    """The `return` that takes one tripwire out of the run.

    The signature is written out literally rather than captured with a
    backreference: the harness escapes backslashes in a replacement, and
    `\\1` would arrive as a literal backslash and a `1`.
    """
    return (rf"(?m)^def {name}\(monkeypatch\):\n",
            f"def {name}(monkeypatch):\n    return\n")


# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour --------------------------------------
    ("the host is read out of the string again", "CAUGHT", [
        (CONFIG,
         r"    if telegram_api_host\(\) == TELEGRAM_PUBLIC_API_HOST:\n",
         "    if TELEGRAM_API.startswith(\"https://api.telegram.org\"):\n", 1),
    ]),
    ("the host is the whole url", "CAUGHT", [
        (CONFIG,
         r"    return \(host or \"\"\)\.lower\(\)\n",
         "    return raw.lower()\n", 1),
    ]),
    ("a url that cannot be parsed takes the bot down", "CAUGHT", [
        (CONFIG,
         r"    except ValueError:\n"
         r"        # A URL that cannot be parsed has no host to speak of\.\n"
         r"        return \"\"\n",
         "    except ValueError:\n"
         "        raise\n", 1),
    ]),
    ("the platform ceiling is applied to every endpoint", "CAUGHT", [
        (CONFIG,
         r"    platform = telegram_download_limit_bytes\(\)\n"
         r"    if platform is None:\n"
         r"        return MAX_DOC_BYTES\n",
         "    return TELEGRAM_PUBLIC_DOWNLOAD_LIMIT_BYTES\n", 1),
    ]),

    # --- group B: the tripwires --------------------------------------
    ("the tripwires are gone, and the host is read out of the string",
     "SURVIVED", [
        (TEST_HOST, *_silence(TRIPWIRES[0]), 1),
        (TEST_HOST, *_silence(TRIPWIRES[1]), 1),
        (TEST_HOST, *_silence(TRIPWIRES[2]), 1),
        (TEST_HOST, *_silence(TRIPWIRES[3]), 1),
        (TEST_HOST, *_silence(TRIPWIRES[4]), 1),
        (TEST_HOST, *_silence(TRIPWIRES[5]), 1),
        (TEST_HOST, *_silence(TRIPWIRES[6]), 1),
        (TEST_HOST, *_silence(TRIPWIRES[7]), 1),
        (CONFIG,
         r"    if telegram_api_host\(\) == TELEGRAM_PUBLIC_API_HOST:\n",
         "    if TELEGRAM_API.startswith(\"https://api.telegram.org\"):\n", 1),
    ]),
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

    caught = survived = refused = invalid = wrong = 0
    for name, expectation, ops in MUTATIONS:
        backups = {}
        mutated_by_path = {}
        try:
            for path, pattern, repl, expect in ops:
                before = backups.get(path)
                if before is None:
                    before = path.read_bytes()
                    backups[path] = before
                    backup = Path(f"/tmp/mut_telegram_api_{path.name}.bak")
                    backup.write_bytes(before)
                    _mutating[str(path)] = str(backup)
                    _write_marker()
                text = mutated_by_path.get(path)
                if text is None:
                    text = before.decode("utf-8")
                found = list(re.finditer(pattern, text, re.M))
                if len(found) != expect:
                    refused += 1
                    print(f"  REFUSED {name}: expected {expect}, found "
                          f"{len(found)} -- refusing to score a mutation "
                          f"that did not apply where claimed", flush=True)
                    mutated_by_path = None
                    break
                mutated_by_path[path] = re.sub(pattern, repl, text, flags=re.M)
            if mutated_by_path is None:
                continue
            for path, mutated in mutated_by_path.items():
                if mutated == backups[path].decode("utf-8"):
                    invalid += 1
                    print(f"  INVALID {name}: substitution was a no-op",
                          flush=True)
                    break
                path.write_text(mutated, encoding="utf-8")
            else:
                for path in mutated_by_path:
                    comp = subprocess.run(
                        [sys.executable, "-c",
                         f"compile(open({str(path)!r}).read(), "
                         f"{str(path)!r}, 'exec')"],
                        capture_output=True, text=True)
                    if comp.returncode != 0:
                        invalid += 1
                        print(f"  INVALID {name}: does not compile -- caught "
                              f"nothing", flush=True)
                        break
                else:
                    verdict, summary = run_tests()
                    if expectation == "CAUGHT":
                        if verdict == "pass":
                            survived += 1
                            print(f"  SURVIVED {name}  {summary}", flush=True)
                        else:
                            caught += 1
                            w = "CAUGHT" if verdict == "fail" else "CAUGHT-BY-HANG"
                            print(f"  {w} {name}  {summary}", flush=True)
                    else:
                        if verdict == "pass":
                            survived += 1
                            print(f"  SURVIVED {name}  {summary}  "
                                  f"(expected -- the tripwire was the work)",
                                  flush=True)
                        else:
                            wrong += 1
                            print(f"  WRONG VERDICT {name}  {summary}  "
                                  f"(expected green: something else is "
                                  f"still guarding it)", flush=True)
        finally:
            for path, before in backups.items():
                restore(path, before)
                _mutating.pop(str(path), None)
            _write_marker()

    print(f"\n{caught} caught, {survived} survived, {wrong} wrong, "
          f"{refused} refused, {invalid} invalid  ({len(MUTATIONS)} mutations)",
          flush=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if (wrong or refused or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
