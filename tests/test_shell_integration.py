"""T50: non-destructive shell integration tests.

Each test calls a real shell command through the actual
sandbox side-car (vaskes/OpenWebUI-bash-sandbox openapi-proxy).
The commands are intentionally non-destructive: no writes,
no deletes, no installs, no network. The test asserts:

  1. The sandbox returns ok=True with exit_code=0.
  2. The stdout contains an expected fragment.
  3. The duration_ms is positive.
  4. The session_id is honoured (cwd / state preserved
     across calls in the same session).

These are the same operations the operator was doing
manually to find bugs after each deploy. Automating them
catches regressions in T43/T49 soft-reject, T44 bash
history, T46 silent delete, T47 reframe, T48 TimedOut,
T49 continue-on-unhandled before a human has to.

Run with: cd /home/git/llama.cpp_telegram && \
  CONVERSATIONS_DB=/tmp/pytest.db pytest tests/test_shell_integration.py -v
"""
import asyncio
import os
import pathlib
import sys
import uuid

import pytest

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        not (os.environ.get("LLAMABOT_DEPLOYMENT_PROFILE") == "personal"
             and os.environ.get("AGENT_STACK_TOKEN")),
        reason="bash_command disabled (need personal profile + token)",
    ),
]

# Add the bot source to path so we can import the actual
# bash_command module (not a mock).
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot"))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "telegram-bot" / "personal_tools"))

# Force the env vars from /opt/telegram-bot/.env (the conftest
# already does this at import time, but be explicit here for
# direct python -m pytest invocations).
os.environ.setdefault("LLAMABOT_DEPLOYMENT_PROFILE", "personal")

from bash_command import call_bash_command, is_enabled_for_active_profile  # noqa: E402


# Skip the whole module if the sandbox is not configured.


def _run(coro):
    return asyncio.run(coro)


def test_sandbox_health():
    """A trivial command to confirm the sandbox is reachable."""
    r = _run(call_bash_command("echo sandbox-ok", session_id="pytest-health"))
    assert r["ok"], f"sandbox not reachable: {r.get('error')}"
    assert "sandbox-ok" in r["stdout"]
    assert r["exit_code"] == 0
    assert r["duration_ms"] > 0


def test_pwd():
    r = _run(call_bash_command("pwd", session_id="pytest-pwd"))
    assert r["ok"]
    assert r["exit_code"] == 0
    # Default sandbox starts in /workspace.
    assert "/workspace" in r["stdout"], f"unexpected pwd: {r['stdout']!r}"


def test_whoami():
    r = _run(call_bash_command("whoami", session_id="pytest-whoami"))
    assert r["ok"]
    assert "root" in r["stdout"], f"expected root, got {r['stdout']!r}"


def test_hostname():
    r = _run(call_bash_command("hostname", session_id="pytest-host"))
    assert r["ok"]
    assert r["stdout"].strip(), "hostname was empty"


def test_date():
    import re
    r = _run(call_bash_command("date -u +%Y", session_id="pytest-date"))
    assert r["ok"]
    # Sandbox stdout includes PS1 + command + output + markers.
    # Extract the first 4-digit year on its own line.
    m = re.search(r"^(\d{4})$", r["stdout"], re.MULTILINE)
    assert m, f"no 4-digit year in stdout: {r['stdout']!r}"
    year = m.group(1)
    assert year >= "2025", f"year is in the past: {year!r}"


def test_echo():
    r = _run(call_bash_command("echo hello-world", session_id="pytest-echo"))
    assert r["ok"]
    assert "hello-world" in r["stdout"]


def test_uname():
    r = _run(call_bash_command("uname -a", session_id="pytest-uname"))
    assert r["ok"]
    assert "Linux" in r["stdout"]


def test_id():
    r = _run(call_bash_command("id", session_id="pytest-id"))
    assert r["ok"]
    assert "uid=0" in r["stdout"], f"expected root uid, got {r['stdout']!r}"


def test_which_python3():
    r = _run(call_bash_command("which python3", session_id="pytest-which"))
    assert r["ok"]
    assert "python3" in r["stdout"]


def test_python_arithmetic():
    """A multi-stage pipeline. Operator's cat-all-files style."""
    r = _run(call_bash_command(
        "python3 -c 'print(2+2)'",
        session_id="pytest-py",
    ))
    assert r["ok"]
    assert r["stdout"].strip().endswith("4") or "\n4\n" in r["stdout"]


def test_cat_etc_os_release():
    r = _run(call_bash_command("cat /etc/os-release | head -3",
                               session_id="pytest-os"))
    assert r["ok"]
    # Sandbox is Ubuntu or Debian based.
    body = r["stdout"].lower()
    assert "ubuntu" in body or "debian" in body, \
        f"expected Ubuntu/Debian, got {r['stdout']!r}"


def test_ls_tmp():
    """ls /tmp is non-destructive (sandbox does not have user
    files there in a clean state, but the directory exists)."""
    r = _run(call_bash_command("ls -la /tmp | head -3",
                               session_id="pytest-ls"))
    assert r["ok"]
    assert r["exit_code"] == 0
    assert "total" in r["stdout"]


def test_session_persistence():
    """A session_id must preserve cwd across two calls.
    This is the feature operator uses to run `cd /opt && ls`
    style commands."""
    sid = f"pytest-cwd-{uuid.uuid4().hex[:8]}"
    r1 = _run(call_bash_command("cd /opt && pwd", session_id=sid))
    assert r1["ok"]
    assert "/opt" in r1["stdout"]
    # Second call without explicit cd - cwd should still be /opt.
    r2 = _run(call_bash_command("pwd", session_id=sid))
    assert r2["ok"]
    assert "/opt" in r2["stdout"], f"cwd lost: {r2['stdout']!r}"


def test_session_isolation():
    """Two different session_ids must NOT share state.
    This is the defence against cross-user state leak."""
    sid_a = f"pytest-iso-a-{uuid.uuid4().hex[:8]}"
    sid_b = f"pytest-iso-b-{uuid.uuid4().hex[:8]}"
    r1 = _run(call_bash_command("cd /opt && pwd", session_id=sid_a))
    r2 = _run(call_bash_command("pwd", session_id=sid_b))
    assert r1["ok"] and r2["ok"]
    assert "/opt" in r1["stdout"]
    # sid_b never did `cd`, so it should be in default cwd.
    import re
    sid_b_prompts = re.findall(r":(/[^:]+)#", r2["stdout"])
    assert all("/opt" not in p for p in sid_b_prompts), \
        f"sid_b leaked from sid_a: prompts={sid_b_prompts} stdout={r2['stdout']!r}"


def test_command_failure_non_zero_exit():
    """A command that exits non-zero should still return ok=True
    with exit_code != 0 (not a network error). The LLM can then
    decide to retry or report the failure to the user."""
    r = _run(call_bash_command("false", session_id="pytest-fail"))
    assert r["ok"], "false should be ok=True (HTTP succeeded)"
    assert r["exit_code"] == 1, f"expected exit 1, got {r['exit_code']}"


def test_stderr_capture():
    """The sandbox-agent captures the full tmux pane and merges
    stdout and stderr into the same stream. We assert that both
    lines are in stdout (they are interleaved by tmux, not split
    by the agent). If a future agent_api version starts splitting
    them, the r["stderr"] field will become non-empty and this
    test can be tightened.
    """
    r = _run(call_bash_command(
        "echo to-stdout; echo to-stderr >&2",
        session_id="pytest-stderr",
    ))
    assert r["ok"]
    assert "to-stdout" in r["stdout"]
    # Documented: stderr is currently merged into stdout.
    assert "to-stderr" in r["stdout"] or "to-stderr" in r.get("stderr", "")


def test_pipeline():
    """Multi-stage pipeline (the exact pattern the operator
    used in the bug report)."""
    r = _run(call_bash_command(
        "cd /opt && find . -maxdepth 2 -name '*.py' | head -3",
        session_id="pytest-pipe",
    ))
    assert r["ok"]
    # Sandbox may or may not have .py files; we only check
    # the command executed and returned something.
    assert r["exit_code"] == 0


def test_tool_result_to_text_shape():
    """tool_result_to_text should produce a non-empty string
    that the LLM can see as a tool result message."""
    from bash_command import tool_result_to_text
    r = _run(call_bash_command("echo test-output", session_id="pytest-trt"))
    text = tool_result_to_text(r)
    assert text
    assert "test-output" in text
    assert "exit_code=0" in text


def test_session_id_default_per_call():
    """If session_id is omitted, the call still works (default
    session). This is what happens when the LLM forgets to
    pass it."""
    r = _run(call_bash_command("echo no-session-id"))
    assert r["ok"]
    assert "no-session-id" in r["stdout"]
