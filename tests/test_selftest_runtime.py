"""`_selftest()` runs, and its result is pinned.

`selftest._selftest` is 1940 lines in one function and, until this file,
had no runtime coverage at all. `test_in_tree_selftest` needs a real
Telegram token and a live llama-server, so it has never passed; every
other test mentioning selftest reads its source and asserts on the AST.

Extracting 1940 lines with no runtime signal is how the `call_llama`
blocks went wrong: a guard removed by the first slice, 805 tests green,
nobody notices until a user gets a truncated stream. This file is the
signal.

What it asserts, in order of how much it is worth:

  1. the harness runs and exits 0 -- the 28 blocks execute;
  2. no block reports `[FAIL]` -- each block's own checks pass;
  3. the marker sequence matches `selftest_golden.log` -- every block
     still reports, in the same order, saying the same thing.

(3) is the one that catches an extraction. A block that stops running
emits no marker; a block that starts lying emits a different one; a
block that swaps places emits the same set in a different order. None
of those is visible to a structural test, and all three are what
cutting a 1940-line function actually produces.

Regenerating the golden is deliberately not automatic. If this test
fails because the selftest legitimately changed, that is the signal
worth reading, and a `--update` flag would train both of us to run it
without looking. Set `SELFTEST_UPDATE_GOLDEN=1` only when you have read
the diff and agree the new behaviour is intended.
"""
import difflib
import os
import pathlib
import sys

import pytest

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from selftest_harness import run_selftest  # noqa: E402

GOLDEN = TESTS / "selftest_golden.log"

# Measured, not estimated. The selftest has exactly 95 assertion
# sites -- 95 unique [OK]/[FAIL] markers.
#
# It was 190 until this was counted properly. Every line the selftest
# logs is written twice by the logging stack, and the first version of
# the harness grepped the raw stream, so 190 was the doubled count of
# 95. That number reached a commit message as a fact. It is corrected
# here rather than quietly replaced, because "the count is 95, and
# here is why the earlier number was wrong" is the useful form.
#
# Bump this deliberately when a block is added, never as a side effect
# of something failing.
EXPECTED_VERDICTS = 95

# Run once per session. It starts a stub server and spawns a
# subprocess, and the whole point is that it is cheap enough to run on
# every change -- 147 markers in well under a second.
@pytest.fixture(scope="session")
def selftest_run():
    lines, _trace, rc, raw, raw_n = run_selftest()
    if rc == "timeout":
        pytest.fail(f"_selftest did not finish inside the harness timeout.\n"
                    f"tail:\n{raw[-3000:]}")
    return lines, rc, raw, raw_n


def test_the_harness_actually_runs_the_selftest(selftest_run):
    lines, rc, raw, raw_n = selftest_run

    assert rc == 0, f"the harness subprocess exited {rc}\ntail:\n{raw[-3000:]}"
    assert len(lines) > 100, (
        f"only {len(lines)} markers came out of 1940 lines of selftest. "
        f"Either the gate is closed or most blocks died early -- neither "
        f"is a pass.\ntail:\n{raw[-3000:]}")


def test_every_assertion_site_fires_exactly_once(selftest_run):
    """The count, which the deduplicated golden cannot see.

    The harness collapses adjacent duplicate lines because the logging
    stack emits some of them twice. That collapse would hide a block
    that genuinely started running twice -- a real failure mode when a
    1940-line function is cut into modules and a driver is written by
    hand. So the raw count is asserted separately: 190 assertion
    sites, and a doubled block makes this fail even though its line
    looks unchanged.
    """
    lines, _rc, _raw, raw_n = selftest_run
    verdicts = [ln for ln in lines if "[OK]" in ln or "[FAIL]" in ln]
    assert len(verdicts) == EXPECTED_VERDICTS, (
        f"{len(verdicts)} [OK]/[FAIL] markers, expected {EXPECTED_VERDICTS}. "
        f"A block that started running twice looks identical in the "
        f"deduplicated golden, so this is where that shows up. The raw "
        f"stream carried {raw_n} matching lines.")


def test_no_block_reports_a_failure(selftest_run):
    lines, _rc, _raw, _raw_n = selftest_run
    fails = [ln for ln in lines if "[FAIL]" in ln]

    assert not fails, (
        f"{len(fails)} selftest block(s) reported [FAIL] under the local "
        f"stubs:\n" + "\n".join(fails))


def test_the_marker_sequence_is_unchanged(selftest_run):
    lines, _rc, _raw, _raw_n = selftest_run
    got = [_normalise(ln) for ln in lines]

    if os.environ.get("SELFTEST_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text("\n".join(got) + "\n", encoding="utf-8")
        pytest.skip("golden rewritten -- read the diff before committing it")

    if not GOLDEN.exists():
        pytest.fail(
            f"{GOLDEN.name} is missing. Run with SELFTEST_UPDATE_GOLDEN=1 to "
            f"create it, then read what it says before committing it.")

    want = GOLDEN.read_text(encoding="utf-8").splitlines()
    assert got == want, _diff(want, got)


def _normalise(line):
    """Strip the parts of a marker that vary between runs.

    Elapsed time is the only volatile field in practice, and leaving it
    in would make this test flaky rather than strict -- which is worse
    than not having it, because a flaky signal gets ignored.
    """
    out = []
    for token in line.split():
        out.append("<elapsed>" if token.endswith("s") and token[:-1].replace(
            ".", "").isdigit() else token)
    return " ".join(out)


def _diff(want, got):
    d = difflib.unified_diff(want, got, "golden", "now", lineterm="", n=1)
    body = "\n".join(list(d)[:80])
    return (
        "the selftest marker sequence changed. Every line that disappeared "
        "is a block that stopped running, and every line that moved is a "
        "block that changed order -- both are what cutting a 1940-line "
        f"function produces.\n\n{body}")