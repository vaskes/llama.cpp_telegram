"""T38: bash_command tool for personal profile.

Wire format
-----------
The tool definition is added to llama-server's `tools` list when
LLAMABOT_DEPLOYMENT_PROFILE=personal. The handler in this module
forwards each call to the vaskes/OpenWebUI-bash-sandbox openapi-proxy
side-car over HTTP with a bearer token.

We do NOT execute commands locally. All shell execution is delegated
to the sandbox side-car which runs as a non-root user with read-only
fs, cap_drop ALL, no-new-privileges, pids_limit, mem_limit, rate
limit, and persistent tmux sessions per session_id.

Profile gate
------------
`_is_enabled_for_active_profile()` returns True only when
LLAMABOT_DEPLOYMENT_PROFILE=personal AND the bearer token is set.
This module is imported by call_llama; the public profile never
constructs the tool definition, so the LLM in a public chat cannot
even see that this tool exists.

Failure modes
-------------
* AGENT_STACK_TOKEN not set -> import is fine, but `enabled()` returns
  False (so the tool is not advertised). A misconfigured personal
  bot just runs without bash, never crashes the bot on startup.
* sandbox side-car unreachable -> call returns an error JSON that
  the LLM sees, with no exception leaking into the bot's flow.
* Timeout in sandbox-agent -> 60s, after which the side-car kills the
  tmux command and returns `{"timed_out": true}`.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx


# Tool definition that goes into llama-server's request body. The model
# sees this and decides whether to call it. Note: we do NOT advertise
# any "destructive" verb in the schema. The sandbox itself enforces
# filesystem and process limits; the tool just forwards.
BASH_COMMAND_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "bash_command",
        "description": (
            "Run a shell command in a persistent, sandboxed Linux "
            "environment. State (cwd, env vars, history, running "
            "processes) is preserved across calls in the same chat. "
            "The sandbox is read-only outside its workspace, runs as "
            "non-root with no capabilities, has a 60s per-command "
            "timeout, a 2GB memory cap, a 256-PID cap, and a 60 rpm "
            "rate limit. Use it for: running scripts, inspecting files "
            "in /workspace, git operations, building projects, "
            "fetching public web pages with curl/wget. Do NOT use it "
            "for: anything that would need sudo, anything that needs "
            "to write outside /workspace, or anything that needs to "
            "run for more than a minute."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The shell command to run. Standard bash. "
                        "Multi-line via embedded \\n is fine. No need "
                        "to wrap in quotes."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Optional. Stable id for a persistent shell "
                        "session (cwd, env, history survive across "
                        "calls). If omitted, a per-chat session id is "
                        "used automatically."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}


# Network: openapi-proxy listens on 192.168.10.7:8090 on the host LAN
# (the agent-net docker network is internal; only openapi-proxy is
# published). Inside the telegram-bot container (which uses a
# macvlan on the same LAN), 192.168.10.7:8090 is reachable.
AGENT_STACK_URL = os.environ.get(
    "AGENT_STACK_URL", "http://192.168.10.7:8090"
)
AGENT_STACK_TOKEN = os.environ.get("AGENT_STACK_TOKEN", "")


def _active_profile() -> str:
    """Read the active deployment profile without importing limits
    at module load time (limits reads env at import). Returns the
    lowercase profile name.
    """
    return os.environ.get("LLAMABOT_DEPLOYMENT_PROFILE", "public").lower()


def is_enabled_for_active_profile() -> bool:
    """The single check the bot uses to decide whether to advertise
    this tool. Returns True ONLY when:
      - LLAMABOT_DEPLOYMENT_PROFILE=personal
      - AGENT_STACK_TOKEN is set (a non-empty string)

    The public profile is structurally incapable of receiving this
    tool, regardless of what the operator does in .env.
    """
    if _active_profile() != "personal":
        return False
    if not AGENT_STACK_TOKEN:
        return False
    return True


async def call_bash_command(
    command: str,
    *,
    session_id: str | None = None,
    timeout: float = 70.0,
) -> dict[str, Any]:
    """Forward a bash call to the sandbox side-car and return the
    parsed JSON response.

    Returns a dict with at least `{"ok": bool}`. On success also has
    `stdout`, `stderr`, `exit_code`, `duration_ms`. On failure has
    `error` (a human-readable string) and the relevant HTTP code if
    the request reached the side-car.

    The timeout is slightly longer than the side-car's per-command
    timeout (60s) so we get the proper `{"timed_out": true}` payload
    back rather than our own httpx timeout.
    """
    if not is_enabled_for_active_profile():
        # Defence in depth: even if the caller forgot to gate,
        # refuse to call.
        return {
            "ok": False,
            "error": (
                "bash_command is only enabled for LLAMABOT_DEPLOYMENT_PROFILE=personal"
            ),
        }
    payload: dict[str, Any] = {"command": command, "timeout": 60}
    if session_id:
        payload["session_id"] = session_id
    headers = {
        "Authorization": f"Bearer {AGENT_STACK_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{AGENT_STACK_URL}/bash",
                json=payload,
                headers=headers,
            )
    except httpx.TimeoutException:
        return {"ok": False, "error": "sandbox side-car timeout (network)"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"sandbox side-car unreachable: {e!r}"}
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "error": (
                f"AGENT_STACK_TOKEN rejected by sandbox side-car "
                f"(HTTP {r.status_code}); check /etc/agent-stack.env"
            ),
            "http_status": r.status_code,
        }
    if r.status_code != 200:
        return {
            "ok": False,
            "error": f"sandbox side-car HTTP {r.status_code}: {r.text[:500]}",
            "http_status": r.status_code,
        }
    try:
        data = r.json()
    except json.JSONDecodeError:
        return {"ok": False, "error": f"sandbox returned non-JSON: {r.text[:500]}"}
    return {
        "ok": True,
        "stdout": data.get("stdout", ""),
        "stderr": data.get("stderr", ""),
        "exit_code": data.get("exit_code"),
        "duration_ms": data.get("duration_ms"),
        "timed_out": data.get("timed_out", False),
    }


def tool_result_to_text(result: dict[str, Any]) -> str:
    """Render the result dict as a string suitable for the tool
    message we send back to the LLM. Truncate stdout/stderr to
    keep tool message size manageable for the model.
    """
    if not result.get("ok"):
        return f"[bash error] {result.get('error', 'unknown error')}"
    out = result.get("stdout", "")
    err = result.get("stderr", "")
    rc = result.get("exit_code", "?")
    dur = result.get("duration_ms", "?")
    parts = [f"exit_code={rc} duration_ms={dur}"]
    if result.get("timed_out"):
        parts.append("TIMED_OUT")
    if err:
        parts.append(f"stderr:\n{err[-2000:]}")
    if out:
        parts.append(f"stdout:\n{out[-8000:]}")
    return "\n".join(parts)
