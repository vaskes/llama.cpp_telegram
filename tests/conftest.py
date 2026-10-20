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
import os
import pathlib
import subprocess

import pytest

ENV_FILE = pathlib.Path("/opt/telegram-bot/.env")

# Values the suite itself needs. Deliberately NOT the real production
# ids/users: tests must not depend on who is in the live whitelist.
TEST_ENV_DEFAULTS = {
    "BOT_TOKEN": "123456:test-suite-placeholder",
    "ALLOWED_USER_IDS": "111,222",
    "CONVERSATIONS_DB": "/tmp/pytest_conversations.db",
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


@pytest.fixture
def sandbox_url():
    # Loopback by default: the side-car is published on 127.0.0.1, not
    # on a LAN address. A host that publishes it elsewhere sets the var.
    return os.environ.get("AGENT_STACK_URL", "http://127.0.0.1:8090")