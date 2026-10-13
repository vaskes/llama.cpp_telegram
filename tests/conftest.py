"""Shared pytest fixtures.

Loads AGENT_STACK_TOKEN from /opt/telegram-bot/.env (read
via sudo cat, since the file is mode 0600 owned by root).
Skips sandbox-touching tests if the token cannot be obtained.
"""
import os
import pathlib
import subprocess
import pytest

ENV_FILE = pathlib.Path("/opt/telegram-bot/.env")


def _load_env_via_sudo():
    """Read /opt/telegram-bot/.env via sudo. The file is
    mode 0600 owned by root, and the test process runs as
    git. We need AGENT_STACK_URL and AGENT_STACK_TOKEN to
    reach the real sandbox; the bot is the only source of
    truth for these values, and we must not duplicate them
    in the repo (defence in depth)."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "cat", str(ENV_FILE)],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError):
        return  # sudo not available or file not readable
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_via_sudo()


def pytest_collection_modifyitems(config, items):
    """Skip sandbox tests if AGENT_STACK_TOKEN is not set."""
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
    return os.environ.get("AGENT_STACK_URL", "http://192.168.10.7:8090")
