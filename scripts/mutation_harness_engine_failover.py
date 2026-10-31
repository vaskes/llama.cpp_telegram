#!/usr/bin/env python3
"""Mutation harness for the engine failover (2026-10-31).

The operator runs one engine instance at a time -- they share the GPUs
-- and keeps the other one standing by. On 2026-10-31 the engine fork
changed, the engine moved to another port, and the bot was left pointing
at a port nobody listened on: it could not reach the engine at all.

So the bot has to be able to move with the engine, and the rules are
narrow on purpose:

  * it does NOT probe which port is up -- a probe per turn would double
    the traffic and make the answer depend on which port answered first;
  * it uses the active endpoint, and only a FAILURE makes it try the
    other one;
  * the endpoint that answered becomes the one the bot keeps using;
  * a request the engine refused (4xx) is NOT sent to the other
    endpoint -- it is the request that is wrong, not the endpoint;
  * a stream that breaks while it is being read is not retried: the
    user has already seen part of the answer.

Each mutation below breaks one of those on purpose.

**Group A -- the behaviour.** The suite has to notice.

**Group B -- the tripwires.** Each mutation removes a tripwire *and* the
behaviour it is supposed to catch, and the suite has to go **green**. A
green suite with the tripwire gone is the proof that the tripwire was
the thing doing the work -- the direction a one-way harness never
checks.

The rules this harness holds itself to, because a mutation run that
cannot report INVALID is a decoration:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * several operations on the SAME file accumulate. Reading the file
    from disk for each of them would leave only the last one applied,
    and the harness would report a catch it never measured;
  * `compile()` validates, not `ast.parse()`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte.

Where it runs: with the current interpreter by default, so it works in
a checkout. Set MUTATION_HARNESS_IMAGE to run the same tests inside
the test image instead.

    python3 scripts/mutation_harness_engine_failover.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tripwire that guards the failover. Everything this harness needs
#: to see is in here: a mutation that no target can notice is a
#: mutation that proves nothing.
TARGETS = [
    "tests/test_llm_endpoint_failover.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

ENDPOINT = ROOT / "telegram-bot" / "llm_endpoint.py"
TEST_FAILOVER = ROOT / "tests" / "test_llm_endpoint_failover.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the bot never tries the other endpoint", "CAUGHT", [
        (ENDPOINT,
         r"    return \[_ENDPOINTS\[index\]\] \+ \[\n"
         r"        url for i, url in enumerate\(_ENDPOINTS\) if i != index\]\n",
         "    return [_ENDPOINTS[index]]\n", 1),
    ]),
    ("the bot forgets which endpoint it moved to", "CAUGHT", [
        (ENDPOINT,
         r"    if index != _ACTIVE:\n"
         r"        log\.info\(\"\[llm_endpoint\] the engine moved to %s\", url\)\n"
         r"        _ACTIVE = index\n",
         "    if False:\n"
         "        log.info(\"[llm_endpoint] the engine moved to %s\", url)\n"
         "        _ACTIVE = index\n", 1),
    ]),
    ("a request the engine refused is sent to the other endpoint too",
     "CAUGHT", [
        (ENDPOINT,
         r"        return exc\.response\.status_code >= 500\n",
         "        return exc.response.status_code >= 400\n", 1),
    ]),
    ("the bot probes the other endpoint on every request", "CAUGHT", [
        (ENDPOINT,
         r"        _activate\(url\)\n        return response\n",
         "        _activate(url)\n"
         "        if len(_ENDPOINTS) > 1:\n"
         "            others = [u for u in _ENDPOINTS if u != url]\n"
         "            if others:\n"
         "                try:\n"
         "                    await client.get(f\"{others[0]}/models\")\n"
         "                except Exception:\n"
         "                    pass\n"
         "        return response\n", 1),
    ]),
    ("a dead engine is quietly ignored", "CAUGHT", [
        (ENDPOINT,
         r"    raise last_exc\n",
         "    return None\n", 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwire for a refused request is gone, and the request is "
     "sent twice", "SURVIVED", [
        (ENDPOINT,
         r"        return exc\.response\.status_code >= 500\n",
         "        return exc.response.status_code >= 400\n", 1),
        (TEST_FAILOVER,
         r"async def test_a_wrong_request_is_not_sent_twice\(\):\n",
         "async def test_a_wrong_request_is_not_sent_twice():\n"
         "    return\n", 1),
    ]),
    ("the tripwires for the move are gone, and the bot forgets it",
     "SURVIVED", [
        (ENDPOINT,
         r"    if index != _ACTIVE:\n"
         r"        log\.info\(\"\[llm_endpoint\] the engine moved to %s\", url\)\n"
         r"        _ACTIVE = index\n",
         "    if False:\n"
         "        log.info(\"[llm_endpoint] the engine moved to %s\", url)\n"
         "        _ACTIVE = index\n", 1),
        (TEST_FAILOVER,
         r"async def test_a_dead_endpoint_moves_the_bot_to_the_other_one\(\):\n",
         "async def test_a_dead_endpoint_moves_the_bot_to_the_other_one():\n"
         "    return\n", 1),
        (TEST_FAILOVER,
         r"async def test_the_move_is_sticky\(\):\n",
         "async def test_the_move_is_sticky():\n    return\n", 1),
        (TEST_FAILOVER,
         r"async def test_the_bot_goes_back_when_the_new_endpoint_dies\(\):\n",
         "async def test_the_bot_goes_back_when_the_new_endpoint_dies():\n"
         "    return\n", 1),
    ]),
]


def _command():
    if IMAGE:
        return ["docker", "run", "--rm", "-v", f"{ROOT}:/w", "-w", "/w",
                "-e", "CONVERSATIONS_DB=/tmp/mut.db", "-e", "ALLOWED_USER_IDS=1",
                IMAGE, "python", "-m", "pytest", *TARGETS, "-q", "-rf",
                "-p", "no:cacheprovider"]
    return [sys.executable, "-m", "pytest", *TARGETS, "-q", "-rf",
            "-p", "no:cacheprovider"]


def run_tests():
    """('pass' | 'fail' | 'hang', summary, who_failed).

    `who_failed` is the list of the test names that went red. A harness
    that reports only "the suite went red" cannot tell a mutation that
    was caught by the test written for it from one that was caught by an
    import error three files away -- and those are not the same
    evidence.
    """
    try:
        proc = subprocess.run(_command(), cwd=str(ROOT), capture_output=True,
                              text=True, timeout=SUITE_TIMEOUT)
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
    print(f"runner: {'docker ' + IMAGE if IMAGE else sys.executable}")
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
                # The source of an operation is the CURRENT state of the
                # file: several operations on one file have to
                # accumulate, or the last one silently undoes the
                # first and the harness measures a mutation it never
                # applied.
                text = mutated.get(path, path.read_text(encoding="utf-8"))
                found = list(re.finditer(pattern, text, re.M))
                if len(found) != expect:
                    refused += 1
                    refused_here = True
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed -- that is how a harness reports a pass "
                          f"it never measured.", flush=True)
                    break
                total += len(found)
                mutated[path] = re.sub(
                    pattern, replacement.replace("\\", "\\\\"), text, flags=re.M)
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
                    try:
                        compile(src, str(path), "exec")
                    except SyntaxError as e:
                        invalid += 1
                        print(f"  INVALID {name}: {path.name} does not "
                              f"compile -- it caught nothing ({e})", flush=True)
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
