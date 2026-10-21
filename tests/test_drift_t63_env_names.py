"""T63: env-var-name drift between tests and code.

The wave-6 regression class in this repo is "the claim says X, the
code does Y" — a guard that is present, green, and does not guard.

This file is the guard against the same thing happening to *test
fixtures*. `tests/test_dispatch_start.py` set
`LLAMABOT_ALLOWED_USER_IDS`, a variable that does not exist anywhere in
the codebase. The whitelist therefore stayed empty, the bot rejected
the fake user at the security gate, and the /start body where the
wave-6 #S40 NameError lives was never executed. The test still went
green — because tests/conftest.py used to dump the live
/opt/telegram-bot/.env into os.environ, so the real ids leaked in.

Tests that set env vars must set names the code actually reads. That is
checkable: collect the set of env vars the application reads, and fail
on any test that monkeypatches/``os.environ``-sets a name outside it.

Complements `test_drift_r10_env_vars.py`, which walks the other
direction (code vars must be documented in .env.example).
"""
import ast
import pathlib
import re
from pathlib import Path

import pytest

ROOT = pathlib.Path(__file__).parent.parent
BOT = ROOT / "telegram-bot"
TESTS = ROOT / "tests"


def _iter_python_files(directory: pathlib.Path):
    for p in sorted(directory.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


# --- 1. every env var the application reads -------------------------------

# Every idiom this codebase uses to read the environment. Keep this
# list in sync with the code: if a new helper starts resolving env
# vars, add it here or the check below develops blind spots. A check
# that cries wolf gets switched off, and a switched-off check is worse
# than no check at all.
_APP_ENV_PATTERNS = (
    # os.environ.get("X", ...)  — the dominant form
    re.compile(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
    # os.environ["X"]
    re.compile(r"""os\.environ\s*\[\s*["']([A-Z][A-Z0-9_]*)["']"""),
    # os.getenv("X", ...)
    re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
    re.compile(r"""os\.environ\.setdefault\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
    # limits.py: _env_or("LLAMABOT_X", profile_default)
    re.compile(r"""_env_or\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
)

# config.py declares its knobs as module-level constants; names can also
# arrive through `from config import (...)`, so collect the public
# uppercase names defined there as a proxy for the documented surface.
_CONFIG_CONST = re.compile(
    r"""^([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=""", re.MULTILINE
)


def _app_env_vars() -> set[str]:
    found: set[str] = set()
    for p in _iter_python_files(BOT):
        text = p.read_text(encoding="utf-8", errors="replace")
        for pat in _APP_ENV_PATTERNS:
            found.update(pat.findall(text))
    config = BOT / "config.py"
    if config.exists():
        found.update(_CONFIG_CONST.findall(config.read_text(
            encoding="utf-8", errors="replace")))
    return found


# --- 2. every env var a test sets ----------------------------------------

_TEST_SET_PATTERNS = (
    re.compile(r"""monkeypatch\.setenv\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
    re.compile(r"""monkeypatch\.delenv\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
    re.compile(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]\s*=""") ,
    re.compile(r"""os\.environ\.setdefault\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
)


def _test_env_vars() -> dict[str, list[str]]:
    """name -> [files that set it]"""
    out: dict[str, list[str]] = {}
    for p in _iter_python_files(TESTS):
        if p.name == Path(__file__).name:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for pat in _TEST_SET_PATTERNS:
            for name in pat.findall(text):
                out.setdefault(name, [])
                rel = str(p.relative_to(ROOT))
                if rel not in out[name]:
                    out[name].append(rel)
    return out


APP_VARS = _app_env_vars()
TEST_VARS = _test_env_vars()


def test_env_var_extraction_sanity():
    """Guard the guard: if the extraction itself returns nothing the
    tests below would pass vacuously — exactly the failure mode this
    file exists to prevent."""
    assert len(APP_VARS) > 10, (
        f"only found {len(APP_VARS)} env vars in the application — the "
        f"extraction is broken, so every other test here is vacuous"
    )
    # spot-check names we know exist
    for expected in ("ALLOWED_USER_IDS", "BOT_TOKEN", "AGENT_STACK_TOKEN",
                     "LLAMABOT_DEPLOYMENT_PROFILE", "WEBHOOK_MODE",
                     "LLAMABOT_LOOP_BUDGET_SEC", "LLAMABOT_DEV_ALLOWED_CHAT_IDS",
                     "AGENT_STACK_URL" if False else "WHISPER_URL"):
        assert expected in APP_VARS, (
            f"{expected} not detected in the application sources — an "
            f"env-reading idiom is probably missing from "
            f"_APP_ENV_PATTERNS"
        )
    assert len(APP_VARS) >= 30, (
        f"only {len(APP_VARS)} env vars detected; expected 30+"
    )


#: Env vars that are real and effective, but read by the standard library or the
#: OS rather than by the application. Listing them keeps the tripwire honest
#: instead of quietly nudging a future author into deleting a working test.
#:
#: TZ is the only one today: test_t68_reanchor.py sets it and calls
#: time.tzset() to check the daily re-anchor timer across DST boundaries. It is
#: consumed by time.localtime(), which seconds_until_next() genuinely calls --
#: so the value does reach the code under test. It simply is not an app config
#: var, and that is exactly the distinction this tripwire draws.
NON_APP_VARS = {"TZ"}


def test_tests_do_not_invent_env_var_names():
    unknown = {n: files for n, files in TEST_VARS.items()
               if n not in APP_VARS and n not in NON_APP_VARS}
    if not unknown:
        pytest.skip("no test sets an env var outside the application surface")
    detail = "\n".join(
        f"  {name}\n      set in: {', '.join(files)}"
        for name, files in sorted(unknown.items())
    )
    pytest.fail(
        "Tests set env vars that the application never reads:\n"
        f"{detail}\n\n"
        "Such a test passes or fails for the wrong reason: the value has\n"
        "no effect on the code under test. Either the name is a typo "
        "(rename\nit to what config.py actually reads), or the test needs\n"
        "an application change instead of an env change.\n\n"
        "Regression: LLAMABOT_ALLOWED_USER_IDS in test_dispatch_start.py\n"
        "(wave-6 #S40 tripwire) was green for weeks while never reaching\n"
        "the code it guards."
    )


def test_whitelist_env_var_is_referenced_exactly_as_config_reads_it():
    """Named regression, so the failure explains itself in history."""
    src = (TESTS / "test_dispatch_start.py").read_text(encoding="utf-8")
    # Match the *call*, not the bare name: this file's own comments
    # quote the wrong name to explain the regression, and a bare
    # substring check would trip on the explanation.
    # Not a bare substring: this file's own comments quote the wrong
    # name to explain the regression.
    assert not re.search(
        r"""setenv\(\s*["']LLAMABOT_ALLOWED_USER_IDS["']""", src
    ), "the wrong env var name is back in test_dispatch_start.py"

    # The /start smoke test must bypass the whitelist explicitly rather
    # than arrange it through the environment: config.py snapshots
    # ALLOWED_USER_IDS at import time, so a monkeypatch there is a no-op
    # once anything has imported the module.
    # TECH-DEBT #2 moved is_authorized from dispatch.py to access.py, so the
    # stub has to target the module that actually holds the function. Patching
    # `dispatch_mod.is_authorized` now rebinds only a re-exported name and the
    # whitelist check keeps running for real — the test would silently stop
    # bypassing authorization while still appearing to pass.
    assert re.search(
        r"""setattr\(\s*access_mod\s*,\s*["']is_authorized["']""", src
    ), (
        "test_dispatch_start.py must stub is_authorized on the module that "
        "defines it (access) — an env monkeypatch cannot reach a value "
        "config.py already snapshotted, and patching the re-export in "
        "dispatch rebinds nothing"
    )


from pathlib import Path  # noqa: E402  (used by _test_env_vars via __file__)