"""Shared pytest fixtures.

The test suite must produce the same result on any machine, with or
without a production deployment on the host. Historically this module
dumped *every* key from /opt/telegram-bot/.env into os.environ, which
made the suite depend on live secrets:

  * on a host with the prod .env, `ALLOWED_USER_IDS` leaked in and tests
    that monkeypatch the whitelist passed by accident;
  * on a host without it, those same tests failed — the suite was not
    a property of the code, it was a property of the machine.

So the split is now explicit:

  * `TEST_ENV_DEFAULTS` — values the suite needs regardless of host.
    Applied with setdefault, so an explicit env still wins.
  * `_load_sandbox_credentials()` — the ONLY thing read from the prod
    .env, and only because those two values cannot live in the repo
    (the sandbox token is a secret, and duplicating it would defeat
    the single-source-of-truth rule in docs/SECURITY.md). Absent
    credentials only skip the `@pytest.mark.sandbox` tests; nothing
    else changes.
"""
import atexit
import os
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ENV_FILE = pathlib.Path("/opt/telegram-bot/.env")

# Values the suite itself needs. Deliberately NOT the real production
# ids/users: tests must not depend on who is in the live whitelist.
# A per-session tmp dir for CONVERSATIONS_DB, registered for cleanup.
#
# Earlier this value was the fixed path /tmp/pytest_conversations.db,
# shared by every pytest run on the host. Two consequences bit the
# operator more than once during the W6 review:
#
#   1. Two pytest runs in parallel collided on the same SQLite file
#      and one of them died with "sqlite3.OperationalError: disk I/O
#      error" mid-test.
#   2. A pytest run that was killed by timeout left the file behind;
#      the next run inherited a corrupted DB and 6 selftest assertions
#      failed before any test code even started.
#
# tempfile.mkdtemp() gives a fresh dir per pytest process. atexit
# hooks clean it up at interpreter shutdown. CI still gets its own
# value via the workflow env override (os.environ.setdefault below).
_SESSION_DB_DIR = tempfile.mkdtemp(prefix="pytest-conversations-")
_SESSION_DB_PATH = os.path.join(_SESSION_DB_DIR, "conversations.db")
atexit.register(shutil.rmtree, _SESSION_DB_DIR, ignore_errors=True)

TEST_ENV_DEFAULTS = {
    "BOT_TOKEN": "123456:test-suite-placeholder",
    "ALLOWED_USER_IDS": "111,222",
    # Per-session unique; the workflow env override still wins.
    "CONVERSATIONS_DB": _SESSION_DB_PATH,
}


def _apply_test_defaults():
    for key, val in TEST_ENV_DEFAULTS.items():
        os.environ.setdefault(key, val)


def _load_sandbox_credentials():
    """Pull only AGENT_STACK_TOKEN / AGENT_STACK_URL from the prod .env.

    Read via sudo: the file is mode 0600 root-owned and the test process
    is not root. Returns quietly when unavailable — the sandbox tests
    skip, everything else still runs.
    """
    wanted = {"AGENT_STACK_TOKEN", "AGENT_STACK_URL"}
    try:
        out = subprocess.run(
            ["sudo", "-n", "cat", str(ENV_FILE)],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError):
        return
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in wanted and key not in os.environ:
            os.environ[key] = val.strip().strip('"').strip("'")


_apply_test_defaults()
_load_sandbox_credentials()


def pytest_collection_modifyitems(config, items):
    """Skip @sandbox tests when no live side-car credentials exist."""
    if not os.environ.get("AGENT_STACK_TOKEN"):
        skip_sandbox = pytest.mark.skip(
            reason="AGENT_STACK_TOKEN not set; sandbox tests skipped"
        )
        for item in items:
            if "sandbox" in item.keywords:
                item.add_marker(skip_sandbox)


@pytest.fixture
def sandbox_token():
    return os.environ.get("AGENT_STACK_TOKEN", "")


@pytest.fixture(autouse=True)
def _the_sandbox_module_follows_the_env():
    """After every test, the sandbox module's copy of the token must
    match the env again.

    ``personal_tools.bash_command`` reads ``AGENT_STACK_TOKEN`` at
    IMPORT time. Several tests re-import or reload it to make it read
    a different env -- and the copy they leave behind says "the
    sandbox is enabled" for the rest of the session, because the env
    they set is gone by then.

    Measured on 2026-11-01: that leak is what made
    ``test_blockers.py::test_handler_survives_reply_returning_none``
    red in a full suite run and green in isolation -- the handler took
    the upload path (the sandbox "was" enabled) with a fake document
    that has no name, raised, and swallowed it.

    One guard for the whole class, in the one place every test passes
    through. The fixtures that re-import the module also put the
    session's copy back (see tests/test_bash_command.py); this guard
    covers the reloads, which mutate the module in place and cannot
    be undone by handing back an old object.
    """
    yield
    try:
        import personal_tools.bash_command as bc
    except ImportError:
        # A materialised tree may carry no personal_tools at all (the
        # deploy-artifact backtests copy part of the tree and run pytest
        # inside it). There is nothing to sync there.
        return
    bc.AGENT_STACK_TOKEN = os.environ.get("AGENT_STACK_TOKEN", "")


@pytest.fixture
def sandbox_url():
    # Loopback by default: the side-car is published on 127.0.0.1, not
    # on a LAN address. A host that publishes it elsewhere sets the var.
    return os.environ.get("AGENT_STACK_URL", "http://127.0.0.1:8090")