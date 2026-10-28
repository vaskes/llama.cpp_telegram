#!/usr/bin/env python3
"""Mutation harness for "a file from an MCP server reaches the model as
its facts" (2026-10-27).

Two rules, both of them about the way a file crosses the boundary
between an MCP server and a chat model.

**Group A -- the behaviour.** Each mutation puts one of the defects
back, and the suite has to notice:

  * a file that is not text is inlined as a base64 blob -- megabytes of
    context for something no text model can read, and a fragment of
    nonsense once the result is truncated;
  * the rendered file carries the bytes anyway, in the one line that
    was supposed to describe it;
  * the token named by ``token_env`` is not read, so the block falls
    back to the copy the naming was meant to remove;
  * a server whose named token is missing is still a server, and the
    model is told "unauthorized" instead of being told that the
    configuration is the problem.

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

    python3 scripts/mutation_harness_mcp_resource_token.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tests that guard this rule, and the neighbours of the path it
#: touches -- a green group B is only a proof if the whole surface is
#: in the run.
TARGETS = [
    "tests/test_mcp_client.py",
    "tests/test_mcp_registry.py",
    "tests/test_mcp_import_wiring.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

BOT = ROOT / "telegram-bot"
CLIENT = BOT / "mcp_client.py"
CONFIG = BOT / "config.py"

# The tripwire files, silenced one by one in group B.
TRIPWIRE_FILES = [
    ROOT / "tests" / "test_mcp_client.py",
    ROOT / "tests" / "test_mcp_registry.py",
]

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("a file that is not text is inlined as a blob", "CAUGHT", [
        (CLIENT,
         r'                elif kind == "resource":\n'
         r'                    parts\.append\(self\._render_resource\(block, structured\)\)\n',
         "", 1),
    ]),
    ("the rendered file carries the bytes anyway", "CAUGHT", [
        (CLIENT,
         r'            return \(f"\[binary file: \{name\} \(\{mime\}\), \{size\} bytes -- "\n'
         r'                    "the content is not inlined\]"\)\n',
         '            return f"[binary file: {name}, {size} bytes] blob={blob}"\n',
         1),
    ]),
    ("the named token is not read", "CAUGHT", [
        (CONFIG,
         r'            token = from_env\n',
         '            token = str(entry.get("token") or "").strip()\n', 1),
    ]),
    ("a server whose named token is missing is still a server", "CAUGHT", [
        (CONFIG,
         r'                log\.warning\("\[mcp\] skipping an MCP server without a token: "\n'
         r'                            "the environment variable %s is empty or unset",\n'
         r'                            token_env\)\n'
         r'                continue\n',
         '                log.warning("[mcp] the named token %s is empty",\n'
         '                            token_env)\n', 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    ("the tripwires are gone, and a file is a blob again", "SURVIVED", [
        (CLIENT,
         r'                elif kind == "resource":\n'
         r'                    parts\.append\(self\._render_resource\(block, structured\)\)\n',
         "", 1),
        *[(path,
           r"(?m)^((?:async )?def test_\w+\([^)]*\):\n)",
           "\\1    return\n", None)
          for path in TRIPWIRE_FILES],
    ]),
    ("the tripwires are gone, and the bytes are in the line", "SURVIVED", [
        (CLIENT,
         r'            return \(f"\[binary file: \{name\} \(\{mime\}\), \{size\} bytes -- "\n'
         r'                    "the content is not inlined\]"\)\n',
         '            return f"[binary file: {name}, {size} bytes] blob={blob}"\n',
         1),
        *[(path,
           r"(?m)^((?:async )?def test_\w+\([^)]*\):\n)",
           "\\1    return\n", None)
          for path in TRIPWIRE_FILES],
    ]),
    ("the tripwires are gone, and the named token is ignored", "SURVIVED", [
        (CONFIG,
         r'            token = from_env\n',
         '            token = str(entry.get("token") or "").strip()\n', 1),
        *[(path,
           r"(?m)^((?:async )?def test_\w+\([^)]*\):\n)",
           "\\1    return\n", None)
          for path in TRIPWIRE_FILES],
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
    was caught by the test that was written for it from one that was
    caught by an import error three files away.
    """
    try:
        proc = subprocess.run(_command(), cwd=str(ROOT), capture_output=True,
                              text=True, timeout=SUITE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "hang", "the suite did not finish", []
    out = proc.stdout + proc.stderr
    tail = [line for line in out.splitlines()
            if " passed" in line or " failed" in line]
    if not tail:
        tail = [line for line in out.splitlines() if "error" in line.lower()]
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
                # The source of an op is the CURRENT state of the file,
                # including the ops that already ran on it. Re-reading
                # the file from disk each time would undo the previous
                # op, and the harness would score a mutation it never
                # actually applied.
                text = mutated.get(path, path.read_text(encoding="utf-8"))
                found = list(re.finditer(pattern, text, re.M))
                if expect is None:
                    # "every site": the tripwire files are silenced in
                    # full, and the count is whatever the file has.
                    if not found:
                        refused += 1
                        refused_here = True
                        print(f"  REFUSED {name}: no test found in "
                              f"{path.name} to silence", flush=True)
                        break
                elif len(found) != expect:
                    refused += 1
                    refused_here = True
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed.", flush=True)
                    break
                total += len(found)
                # m.expand() is the substitution: the template may
                # carry a group reference (\1) and the backslashes in
                # it are the template's own, not something to be
                # escaped twice.
                mutated[path] = re.sub(pattern,
                                       lambda m: m.expand(replacement),
                                       text, flags=re.M)
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
                    if path.suffix == ".py":
                        try:
                            compile(src, str(path), "exec")
                        except SyntaxError as e:
                            invalid += 1
                            print(f"  INVALID {name}: {path.name} does not "
                                  f"compile -- it caught nothing ({e})",
                                  flush=True)
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
    if dirty.stdout.strip():
        print("THE TREE IS DIRTY:\n" + dirty.stdout, flush=True)
        return 1
    return 1 if (wrong or refused or invalid) else 0


if __name__ == "__main__":
    raise SystemExit(main())
