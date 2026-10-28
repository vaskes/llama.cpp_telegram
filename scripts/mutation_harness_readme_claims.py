#!/usr/bin/env python3
"""Mutation harness for "a badge is a claim" (2026-10-27; rewritten 2026-10-28).

The README's header carries the same badge block as the side-car's:
license, interpreter, compose, release, and the test count. The number
in the badge is a claim about the tree, and a claim that nobody
recomputes rots silently -- which is what happened: the clone pins in
the README's header, the README's quick start, SETUP, BASH-SANDBOX and
the CHANGELOG's header were all a release behind at the same time,
while the README looked perfect.

**Why this file was rewritten (2026-10-28).** The harness used to name
the claims in its own source: `Tests: 2806`, `v0.15.0-stable`,
`--branch v0.15.0-stable`. Every one of those was a *copy* of the
tree, and a copy rots. When the badge's number moved from 2806 to 2891,
six of the fifteen operations were REFUSED -- the pattern no longer
matched, the harness said so, and it reported a clean run while
measuring nothing. A harness that cannot apply its mutations has not
proved anything, so:

  * **the claims are read out of the tree**, not written here -- there
    is no number and no tag in this file any more, only the shape of
    the claim, and even the shape is imported from the tripwire itself,
    so the harness and the tripwire cannot disagree about what a badge
    looks like;
  * **a refused or invalid mutation fails the run** -- it is not a
    detail of one mutation, it is the end of the evidence.

**Group A -- the behaviour.** Each mutation makes the README lie, and
the suite has to notice:

  * the badge's number stops matching the tree;
  * the badge's two copies of the number disagree (the alt text says
    one thing, the image another -- the image is what a reader sees);
  * the header names a release that is not the current one;
  * a clone command pins a release that is not the current one;
  * the version badge stops agreeing with the Dockerfile, and the
    license badge with the LICENSE file;
  * the badge is gone entirely.

**Group B -- the tripwires.** The mutation removes the tripwires *and*
the claims they are supposed to catch, and the suite has to go
**green**. A green suite with the tripwire gone is the proof that the
tripwire was the thing doing the work.

The rules this harness holds itself to:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * a REFUSED or an INVALID mutation fails the run;
  * `compile()` validates, not `ast.parse()`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the name of the test that went red is printed;
  * the silenced tripwires are checked to exist by name before the run
    -- a harness that silences a test that is no longer there proves
    nothing;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte;
  * the tripwires are silenced with a `return` at the top of the test
    body, not by renaming the function: a renamed test leaves the file,
    and the CODEBOOK's table quotes how many tests it has.

    python3 scripts/mutation_harness_readme_claims.py [ROOT]
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The tests that guard this rule, and the neighbours of the documents
#: it touches -- a green group B is only a proof if the whole surface
#: is in the run.
TARGETS = [
    "tests/test_readme_release_claims.py",
    "tests/test_changelog_structure.py",
    "tests/test_codebook_test_table.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 600

README = ROOT / "README.md"
CLAIMS_TEST = ROOT / "tests" / "test_readme_release_claims.py"

#: The tripwires group B silences: the ones that are supposed to catch
#: the lies the mutation tells. Checked against the file before the run
#: -- if one of them was renamed, the harness stops with a message
#: instead of "proving" something with a test that is not there.
SILENCED = (
    "test_the_badge_counts_the_tests_that_are_there",
    "test_the_readme_names_the_current_stable_release",
    "test_every_clone_command_pins_the_current_stable",
)

#: A tag that is not the current one, and a license that is not the
#: repository's. Both are deliberately wrong values, not copies of
#: anything in the tree -- a copy would rot the way this file's old
#: literals did.
WRONG_TAG = "v0.0.0-rot"
WRONG_LICENSE = "Nonexistent-1.0"

#: The shape of a claim comes from the tripwire itself. One definition:
#: the harness and the test cannot drift apart about what a badge is.
sys.path.insert(0, str(ROOT / "tests"))
try:
    import test_readme_release_claims as claims  # noqa: E402
except ImportError as exc:  # pragma: no cover -- the harness stops here
    print(f"cannot import the tripwire: {exc}")
    raise SystemExit(2)


def _claims() -> dict:
    """Read the claims out of the tree, and the tree's answer to them.

    The preflight has already proved that the README agrees with the
    tree; this reads what it says, so that the mutations can be built
    from the live values instead of from literals that rot.
    """
    readme = README.read_text(encoding="utf-8")
    found = {}
    for key, pattern in (("badge_alt", claims.BADGE_ALT),
                         ("badge_image", claims.BADGE_IMAGE),
                         ("python", claims.PYTHON_BADGE),
                         ("license", claims.LICENSE_BADGE),
                         ("stable", claims.CURRENT_STABLE)):
        m = pattern.search(readme)
        if not m:
            raise SystemExit(
                f"the README has no {key} claim -- the tripwire's pattern "
                "stopped matching, or the badge was removed. The harness "
                "would measure nothing.")
        found[key] = m.group(1)
    if found["badge_alt"] != found["badge_image"]:
        raise SystemExit(
            f"the badge's two copies already disagree ({found['badge_alt']} "
            f"and {found['badge_image']}) -- the tree is broken before the "
            "harness starts")
    found["readme"] = readme
    found["wrong_number"] = str(int(found["badge_alt"]) + 1)
    found["wrong_python"] = found["python"][:-1] + "9"
    return found


def _clone_ops(readme: str, tag: str) -> list:
    """The clone commands in the README that pin the current tag.

    The pattern is the command itself and not the whole line: the
    header's command is quoted with a "> " in front of it, and a
    pattern that carried the quote would overlap the one without it --
    the second operation would then find one site where it was told to
    find two, and the mutation would be refused for a reason that has
    nothing to do with the tree.
    """
    ops = []
    seen = set()
    for m in claims.CLONE_PIN.finditer(readme):
        pinned, command = m.group(1), m.group(0)
        if pinned != tag or command in seen:
            continue
        seen.add(command)
        sites = len(re.findall(re.escape(command), readme))
        ops.append((re.escape(command), command.replace(pinned, WRONG_TAG),
                    sites))
    return ops


def _mutations(c: dict) -> list:
    """The mutations, built from the claims that were just read."""
    readme = c["readme"]
    tag = c["stable"]
    number = c["badge_alt"]
    wrong = c["wrong_number"]

    alt = re.escape(f"Tests: {number}")
    image = re.escape(f"tests-{number}-brightgreen")
    header = re.escape(f"**Current stable: `{tag}`**")
    python = re.escape(f"[![Python: {c['python']}+]")
    license_ = re.escape(f"[![License: {c['license']}]")
    badge_line = next((ln.strip() for ln in readme.splitlines()
                       if claims.BADGE_ALT.search(ln)), None)
    if badge_line is None:
        raise SystemExit("the README has no badge line to remove")
    badge_whole = re.escape(badge_line)

    # The clone commands, grouped by the command text: two identical
    # commands are one operation with two sites, two different ones are
    # two operations. The count is measured, not remembered.
    clone_ops = _clone_ops(readme, tag)

    return [
        # --- group A: the behaviour -------------------------------------
        ("the badge's number stops matching the tree", "CAUGHT", [
            (README, alt, f"Tests: {wrong}", 1),
            (README, image, f"tests-{wrong}-brightgreen", 1),
        ]),
        ("the badge's two copies of the number disagree", "CAUGHT", [
            (README, alt, f"Tests: {wrong}", 1),
        ]),
        ("the header names a release that is not the current one", "CAUGHT", [
            (README, header, f"**Current stable: `{WRONG_TAG}`**", 1),
        ]),
        ("a clone command pins a release that is not the current one", "CAUGHT",
         [(README, pattern, repl, sites) for pattern, repl, sites in clone_ops]),
        ("the version badge stops agreeing with the Dockerfile", "CAUGHT", [
            (README, python, f"[![Python: {c['wrong_python']}+]", 1),
        ]),
        ("the license badge stops agreeing with the LICENSE file", "CAUGHT", [
            (README, license_, f"[![License: {WRONG_LICENSE}]", 1),
        ]),
        ("the badge is gone", "CAUGHT", [
            (README, badge_whole, "", 1),
        ]),

        # --- group B: the tripwires ------------------------------------
        ("the tripwires are gone, and the README lies",
         "SURVIVED", [
             (CLAIMS_TEST,
              rf"(?m)^def {name}\(\):\n",
              f"def {name}():\n    return\n", 1)
             for name in SILENCED
         ] + [
             (README, alt, f"Tests: {wrong}", 1),
             (README, image, f"tests-{wrong}-brightgreen", 1),
             (README, header, f"**Current stable: `{WRONG_TAG}`**", 1),
         ] + [
             (README, pattern, repl, sites)
             for pattern, repl, sites in clone_ops
         ]),
    ]


def _check_silenced() -> None:
    """The tripwires group B silences have to be there.

    A harness that silences a test that no longer exists proves
    nothing at all, and reports a green run while doing it.
    """
    try:
        tree = ast.parse(CLAIMS_TEST.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        print(f"cannot read {CLAIMS_TEST}: {exc}")
        raise SystemExit(2)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [name for name in SILENCED if name not in defined]
    if missing:
        print("these tripwires are not in "
              f"{CLAIMS_TEST.name} any more: {missing}. The harness would "
              "silence nothing and report a green run.")
        raise SystemExit(2)


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
    _check_silenced()
    claims_read = _claims()
    print(f"claims read from the tree: tests={claims_read['badge_alt']}, "
          f"stable={claims_read['stable']}, python={claims_read['python']}, "
          f"license={claims_read['license']}")
    MUTATIONS = _mutations(claims_read)

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
                if len(found) != expect:
                    refused += 1
                    refused_here = True
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed.", flush=True)
                    break
                total += len(found)
                mutated[path] = re.sub(pattern, replacement.replace("\\", "\\\\"),
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
    print("tree after the run:", dirty.stdout.strip() or "clean")
    if wrong:
        return 1
    if refused or invalid:
        # A mutation that could not be applied is not a mutation that
        # proved anything. The run is a failure, not a detail.
        print("THE RUN MEASURED NOTHING IN FULL -- a refused or invalid "
              "mutation is a hole in the evidence.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
