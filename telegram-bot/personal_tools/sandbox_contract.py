"""What the bot believes about the sandbox it talks to. One place.

The bot says two kinds of thing about the sandbox: what it *does* (HTTP
routes, a bearer token, a file door) and what it *is* (a hardened
container, a non-root user, a wall the model cannot cross). The second
kind used to live in a hand-written sentence in a docstring, and the
sentence outlived the sandbox.

On 2026-10-25 the operator turned the sandbox's privilege mode on
(sandbox v2.8.0): a command inside the sandbox's container can become
root there. The bot's docstring still described the container as
non-root with an empty capability set and no-new-privileges -- all
three false from that moment -- and nothing in the tree noticed. A
claim in a docstring that nobody checks is not a claim; it is a finding
nobody wrote down.

On 2026-10-26 the operator gave the production deployment a writable
rootfs as well (sandbox v2.10.0, `AGENT_READ_ONLY=false`): a
privileged root that cannot write a byte of the filesystem cannot
install a package, and apt-get is exactly what a privileged root is
for. The container stays the perimeter -- the writable layer dies with
the container.

THE MODEL'S READ BUDGET (v2.13.0, 2026-10-27)
The bot imports the side-car's MCP door, and one of the tools on that
door reads a whole file into the model's context. The side-car's
binary limit (50 MiB) is the right limit for a download, and the wrong
one for a prompt: one MCP read could cost the machine about 117 MiB of
RAM for an answer a text model has no use for. So the side-car gave
that door a budget of its own -- `AGENT_MCP_MAX_READ_BYTES`, 1 MiB by
default -- and a refusal that names the file's size and the limit
instead of a truncated file. The HTTP door kept its own limit: the
bytes land in a file there.

The bot's claim about the side-car moves with the side-car, and the
tripwire checks it against the pinned source -- which is why the pin
and this number cannot drift apart: the pin is v2.13.0, and v2.13.0
is where the budget exists.

So the posture lives here, once:

* `SANDBOX_PINNED_VERSION` -- the tag this repo pins as a submodule.
  `tests/test_sandbox_contract.py` reads the pin out of the tree and
  the version out of the submodule's own source, and compares them with
  this number, so one of the three cannot move without the others.
* `PRIVILEGE_MODE` -- whether a command may become root inside the
  sandbox's container. The sandbox has supported it since v2.8.0; the
  operator turned it on for the production deployment on 2026-10-25.
* `SANDBOX_POSTURE` -- the paragraph the operator-facing documents
  quote verbatim, so the prose cannot say something else than the code
  believes.
* `SUDO_POLICY` -- the sentence the *model* reads, inside the
  `bash_command` tool description.
* `EXEC_TIMEOUT_SECONDS` and the two numbers derived from it -- the
  command's time budget, which the side-car's proxy owns as a knob and
  the bot has to stay strictly above.

THE STDIN TRAP IS GONE (v2.10.2, 2026-10-26)
The side-car runs a command in a tmux pane and knows the command is
over because a marker it printed shows up in the pane. A command that
reads standard input used to read that marker instead: apt-get does it
through debconf, and the answer was lost even though the command itself
finished in a second. The side-car fixed it -- the command now runs
inside a brace group whose stdin is /dev/null, so it reads EOF and the
protocol stays intact.

So the bot's tool description no longer carries the warning, and no
sentence here tells the model to write `< /dev/null`: a warning for a
bug that no longer exists is a stale claim about the side-car, and it
teaches the model a workaround it does not need. The history stays in
this docstring, where it belongs.

The model-facing sentence is the interesting one. The tool description
used to say "Do NOT use it for: anything that would need sudo", and the
model read that as a wall. It is not a wall any more: with the privilege
mode on, sudo works inside the sandbox. A policy the model believes is a
wall stops being a policy the moment the model tries it and succeeds --
so the description now says what is true and keeps the rule: sudo is
available in there, and the bot does not want it used.

Everything else that talks about the posture -- the docstrings, the
documents -- points here instead of restating it. Restating is how the
sentence went stale in the first place.
"""
from __future__ import annotations

__all__ = [
    "SANDBOX_REPO",
    "SANDBOX_PINNED_VERSION",
    "PRIVILEGE_MODE_SUPPORTED",
    "PRIVILEGE_MODE",
    "WRITABLE_ROOTFS",
    "SANDBOX_POSTURE",
    "SANDBOX_POSTURE_LINES",
    "SUDO_POLICY",
    "EXEC_TIMEOUT_SECONDS",
    "PROXY_WAIT_GRACE_SECONDS",
    "CLIENT_TIMEOUT_SECONDS",
    "MCP_MAX_READ_BYTES",
]

SANDBOX_REPO = "vaskes/OpenWebUI-bash-sandbox"

# The tag this repo pins as a submodule. Keep it equal to the pin in the
# tree and to the version the submodule's own source declares: a bump of
# one without the others is a red test, not a quiet drift.
#
# v2.13.0 (2026-10-27) is the version the production side-car runs: the
# MCP read got a budget of its own (see MCP_MAX_READ_BYTES below), and
# the search engine's read-only mount is gone from the deployment.
SANDBOX_PINNED_VERSION = "2.13.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    """``"2.9.1"`` -> ``(2, 9, 1)``; anything unparseable -> ``()``."""
    parts: list[int] = []
    for chunk in value.strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


# The privilege mode exists since sandbox v2.8.0: the image ships sudo
# and a NOPASSWD rule, so a command can become root inside the
# container. Before that, no sandbox could, and the bot's prose said so.
PRIVILEGE_MODE_SUPPORTED = _version_tuple(SANDBOX_PINNED_VERSION) >= (2, 8, 0)

# Whether it is ON in the deployment this bot talks to. It is a
# deployment fact, not a version fact: the sandbox ships with the mode
# off (the image build argument defaults to 0), and the operator turned
# it on for the production deployment on 2026-10-25 --
# AGENT_ALLOW_SUDO=1, AGENT_NO_NEW_PRIVILEGES=false,
# AGENT_CAP_DROP=SYS_ADMIN in the side-car's env file.
#
# A deployment that leaves the mode off gets a bot whose description says
# sudo works there: the model tries, gets "not allowed", and moves on.
# The sentence is about the sandbox's capability, which the pinned
# version has; the deployment decides whether it is switched on.
PRIVILEGE_MODE = True

# Whether the container's root filesystem is writable in the deployment
# this bot talks to. A deployment fact too, and a knob the sandbox
# added in v2.10.0: `read_only: "${AGENT_READ_ONLY:-true}"`. The stock
# posture is a read-only rootfs; the production deployment sets
# AGENT_READ_ONLY=false, because a privileged root that cannot write a
# byte of the filesystem cannot install a package (apt-get writes to
# /var/lib/apt/lists, /var/lib/dpkg and /usr). The container is still
# the perimeter: the writable layer dies with the container.
WRITABLE_ROOTFS = True

# The operator-facing paragraph, one sentence per line. The documents
# quote these lines verbatim -- a tripwire checks that they still carry
# them -- so the prose cannot drift away from what the code believes.
# A paraphrase would be a second copy, and a second copy is how the
# sentence went stale in the first place.
SANDBOX_POSTURE_LINES = (
    "The side-car runs in a container whose rootfs is writable in this "
    "deployment (AGENT_READ_ONLY=false), so a root inside it can install "
    "packages; /workspace is the workspace mount, and the host's "
    "ssh-keys are mounted read-only.",
    "The agent's own process runs as uid 1000 and the container drops "
    "SYS_ADMIN, so there are no mounts and no namespaces to be had from "
    "inside.",
    "The privilege mode is ON: a command may become root inside the "
    "container, so the container is the perimeter and what is inside it "
    "is the blast radius.",
)
SANDBOX_POSTURE = " ".join(SANDBOX_POSTURE_LINES)


def _sudo_policy() -> str:
    """The sentence the model reads, inside the tool description.

    It is a policy, and it says so. The sandbox can do it; the bot does
    not want it done.
    """
    if PRIVILEGE_MODE:
        return (
            "Do NOT use it for: anything that would need sudo -- the "
            "sandbox's container can run a command as root (its privilege "
            "mode is on), so that is a policy and not a wall: the "
            "container is the boundary, and the work this tool is for "
            "does not need root. Nor for anything that needs to write "
            "outside /workspace, or anything that needs to run for more "
            "than a minute."
        )
    return (
        "Do NOT use it for: anything that would need sudo (the sandbox "
        "runs without it), anything that needs to write outside "
        "/workspace, or anything that needs to run for more than a minute."
    )


SUDO_POLICY = _sudo_policy()


# === the command's time budget ===

#: The longest a single command may run in the sandbox. Since v2.10.2
#: this is a knob on the side-car's proxy: `AGENT_EXEC_TIMEOUT`
#: (5..600, default 60). The proxy asks the agent for this limit, and
#: the agent answers `timed_out: true` with the output it managed to
#: produce when the limit is up.
EXEC_TIMEOUT_SECONDS = 60

#: How much longer the proxy waits for the agent than the limit it
#: asked for. Before v2.10.2 both numbers were 60: the agent answered
#: at the very moment the proxy stopped listening, so the answer that
#: said "this timed out" was thrown away and the caller saw a transport
#: error with an empty reason -- which sent the operator to the network
#: instead of to the clock.
PROXY_WAIT_GRACE_SECONDS = 15

#: How many bytes one MCP `read_file` may put into a model's context.
#: Since v2.13.0 the side-car owns that budget: `AGENT_MCP_MAX_READ_BYTES`
#: (0 .. 64 MiB, 1 MiB by default). Before it, the effective ceiling was
#: the deployment's binary limit -- 50 MiB -- and one call read that much
#: into memory and base64-encoded it, about 117 MiB of RAM for an answer
#: a text model cannot use.
#:
#: A deployment fact, like the other two above: the side-car ships the
#: knob with that default, and the production deployment does not
#: override it. The HTTP door's own limit is a different axis -- there
#: the bytes land in a file on the caller's disk, not in a prompt.
MCP_MAX_READ_BYTES = 1024 * 1024

#: The bot's own HTTP timeout. It has to be strictly larger than the
#: proxy's wait (the limit plus the grace), or the bot gives up before
#: the proxy does and the answer is lost again -- one layer up. The
#: margin on top is for the trip between the two.
CLIENT_TIMEOUT_SECONDS = (
    EXEC_TIMEOUT_SECONDS + PROXY_WAIT_GRACE_SECONDS + 15
)
