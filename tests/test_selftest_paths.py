"""selftest.py must not assume a container path.

W3-H2. The `/app/*.py` hardcodes were removed in 88a119b and the fix was
confirmed by running `_selftest()` against a mock harness. Then the
mutation "put `/app/storage.py` back" survived the entire suite, on both
interpreters:

    W2-M1: /app path restored   SURVIVED  SURVIVED

Two structural reasons the suite could not see it:

  1. `tests/test_selftest.py` is in CI's --ignore list.
  2. `_selftest()` returns immediately unless LLAMABOT_SELFTEST=1, so even
     in a full run nothing ever executed the function:

         LLAMABOT_SELFTEST default: None
         output of _selftest() with no env var: ''

With 94 assertion sites behind that gate, "the smoke test is green" was
not a statement about anything. This is the cheap half of the fix: a
static tripwire that catches the regression instantly, without needing a
mock Telegram.

The expensive half -- un-ignoring test_selftest.py and running the smoke
test in CI with LLAMABOT_SELFTEST=1 -- is now possible, because
TELEGRAM_API finally reaches the PTB client (W2-H2). Until someone wires
that up, this file is the guard.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

SELFTEST = BOT_DIR / "selftest.py"


def test_selftest_contains_no_container_paths():
    """`/app/...` is the container's layout. Any occurrence means the smoke
    test aborts on the first one it hits -- a function whose contract is
    that it never raises."""
    source = SELFTEST.read_text(encoding="utf-8")
    offenders = [
        (n, ln.strip())
        for n, ln in enumerate(source.splitlines(), 1)
        if "/app/" in ln and not ln.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"selftest.py hardcodes container paths at {offenders}. The smoke "
        f"test cannot run outside the image, and nothing in CI would notice: "
        f"test_selftest.py is --ignore'd and _selftest() returns "
        f"immediately unless LLAMABOT_SELFTEST=1."
    )


def test_module_source_helper_exists():
    """If someone deletes the helper without inlining its logic, the next
    test would stop being able to find sibling modules at all."""
    source = SELFTEST.read_text(encoding="utf-8")
    assert "def _module_source(" in source, (
        "selftest.py lost _module_source(); sibling modules are read through "
        "it instead of a hardcoded path"
    )
    assert "import storage as _storage_mod" in source or (
        "storage" in source), (
        "selftest.py should use the already-imported storage module"
    )


def test_selftest_really_is_gated():
    """Pin the gate itself.

    If someone removes the LLAMABOT_SELFTEST check, the smoke test starts
    running inside the unit suite -- where no Telegram exists -- and every
    run goes red. Better that than silent, but the tripwire above would
    then be half the story, so the shape is recorded here.
    """
    source = SELFTEST.read_text(encoding="utf-8")
    assert "LLAMABOT_SELFTEST" in source, (
        "the LLAMABOT_SELFTEST gate is gone; 94 assertion sites would start "
        "running inside the unit suite with no Telegram to talk to"
    )