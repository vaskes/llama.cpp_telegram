"""The bot's claims about the sandbox must match the sandbox we run.

Added 2026-10-25, right after the operator turned the sandbox's privilege
mode on (sandbox v2.8.0). The bot's docstring went on describing the
side-car's container as non-root with an empty capability set and
no-new-privileges; the tool description the model reads went on telling
it that sudo was off the table. Nothing in the tree noticed, because
nothing compared the claim with the thing the claim was about.

So the claim lives in one place -- `telegram-bot/personal_tools/sandbox_contract.py` --
and this file is the check:

  * the version the contract names is the version the pinned submodule's
    own source declares, and the submodule is the one the tree pins;
  * the privilege mode is only claimed by a sandbox that has it;
  * the sentence the model reads says what is true about sudo, and
    keeps the policy;
  * the operator-facing prose carries the contract's posture, and states
    the sandbox's defaults through the knobs the sandbox defines instead
    of asserting a posture in words;
  * a 403 from the door means a permission and a 401 means the token.

The tests read the sandbox's own compose file for the defaults, so the
document cannot drift away from the sandbox it describes without one of
them going red.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"
SANDBOX = ROOT / "sandbox"

if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from personal_tools.sandbox_contract import (  # noqa: E402
    PRIVILEGE_MODE,
    PRIVILEGE_MODE_SUPPORTED,
    SANDBOX_PINNED_VERSION,
    SANDBOX_POSTURE_LINES,
    STDIN_POLICY,
    SUDO_POLICY,
    WRITABLE_ROOTFS,
)
import personal_tools.file_transport as ft  # noqa: E402
import personal_tools.bash_command as bc  # noqa: E402

# The operator-facing places that talk about the sandbox's posture.
DOCS = (ROOT / "docs" / "BASH-SANDBOX.md", ROOT / "docs" / "ARCHITECTURE.md")
PROSE = DOCS + (BOT / "personal_tools" / "bash_command.py",)

# The claim as it was written before the privilege mode. A phrase ban is
# a blunt instrument, and it is aimed at one thing: the old sentence
# coming back. It is not a way of saying "the document may not mention
# capabilities" -- the document may, as long as it says which knob sets
# them (the next test).
STALE_CLAIMS = (
    "non-root, cap_drop ALL, no-new-privileges",
    "cap_drop: ALL` and `no-new-privileges",
    "`cap_drop: ALL` and `no-new-privileges`",
)

# The sandbox's own compose file, read for the values the document has
# to quote. The keys are the ones the bot's prose talks about; the
# patterns are deliberately narrow -- a document that quotes a value the
# compose does not have is not describing this sandbox.
COMPOSE_VALUES = (
    ("mem_limit", re.compile(r"^\s*mem_limit:\s*(\S+)", re.MULTILINE)),
    ("pids_limit", re.compile(r"^\s*pids_limit:\s*(\S+)", re.MULTILINE)),
    ("read_only", re.compile(r"^\s*read_only:\s*(\S+)", re.MULTILINE)),
)
COMPOSE_KNOBS = (
    ("AGENT_ALLOW_SUDO",
     re.compile(r"\$\{AGENT_ALLOW_SUDO:-([^}]+)\}")),
    ("AGENT_CAP_DROP",
     re.compile(r"\$\{AGENT_CAP_DROP:-([^}]+)\}")),
    ("AGENT_NO_NEW_PRIVILEGES",
     re.compile(r"\$\{AGENT_NO_NEW_PRIVILEGES:-([^}]+)\}")),
    ("AGENT_READ_ONLY",
     re.compile(r"\$\{AGENT_READ_ONLY:-([^}]+)\}")),
)


def _compose_text() -> str:
    path = SANDBOX / "docker-compose.production.yml"
    assert path.is_file(), (
        "the sandbox submodule is not checked out -- run "
        "`git submodule update --init --recursive`. A tripwire that "
        "cannot see the sandbox cannot check the claims about it.")
    return path.read_text(encoding="utf-8")


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=True).stdout.strip()


def _description() -> str:
    return bc.BASH_COMMAND_TOOL_DEF["function"]["description"]


def test_the_contract_names_the_version_the_pinned_sandbox_declares():
    """The number in the contract is the number in the submodule."""
    entry = _git("ls-tree", "HEAD", "sandbox")
    assert entry, (
        "the tree has no sandbox submodule -- there is nothing to pin the "
        "contract to")
    mode, kind, sha, path = entry.split()
    assert kind == "commit", f"the sandbox is not a gitlink: {entry!r}"
    assert path == "sandbox"

    version_file = SANDBOX / "agent_core.py"
    assert version_file.is_file(), (
        "the sandbox submodule is not checked out -- run `git submodule "
        "update --init --recursive`. A tripwire that cannot see the "
        "sandbox cannot check the claims about it.")
    declared = re.search(r'^__version__ = "([^"]+)"',
                         version_file.read_text(encoding="utf-8"),
                         re.MULTILINE)
    assert declared, "the sandbox does not declare a version"
    assert declared.group(1) == SANDBOX_PINNED_VERSION, (
        f"the contract says {SANDBOX_PINNED_VERSION}, the pinned sandbox "
        f"declares {declared.group(1)}. One of them was moved and the "
        "other was not.")

    checked_out = _git("rev-parse", "HEAD", cwd=SANDBOX)
    assert checked_out == sha, (
        f"the sandbox submodule is checked out at {checked_out}, the tree "
        f"pins {sha}. The tripwire reads the checkout, so the checkout "
        "has to be the pin.")


def test_the_privilege_mode_is_only_claimed_by_a_sandbox_that_has_one():
    """The mode landed in v2.8.0. A sandbox older than that cannot."""
    if PRIVILEGE_MODE:
        assert PRIVILEGE_MODE_SUPPORTED, (
            f"the contract claims the privilege mode, but the pinned "
            f"sandbox ({SANDBOX_PINNED_VERSION}) is older than the one "
            "that introduced it")


def test_the_tool_description_says_what_is_true_about_sudo():
    """The model reads the description, not the contract.

    The old description said "Do NOT use it for: anything that would
    need sudo", and the model read that as a wall. With the privilege
    mode on, sudo works in there -- so a description that leaves the
    model believing otherwise is a policy that stops being a policy the
    moment the model tries it and succeeds.
    """
    desc = _description()
    assert SUDO_POLICY in desc, (
        "the tool description no longer carries the contract's sentence -- "
        "the model reads the description, not the contract")
    if PRIVILEGE_MODE:
        assert "policy" in desc and "not a wall" in desc, (
            "the privilege mode is on, so the description has to say that "
            "the ban on sudo is a policy and not a wall")
        for banned in ("no sudo", "sudo is not available",
                       "cannot use sudo", "without sudo", "runs without it"):
            assert banned not in desc.lower(), (
                f"the description says {banned!r}, which is not true while "
                "the privilege mode is on")
    else:
        assert "runs without it" in desc, (
            "without the privilege mode the description has to say that "
            "the sandbox cannot do it at all")


def test_the_tool_description_carries_the_stdin_policy():
    """The model reads the description, and the stdin trap is in it.

    A command that reads standard input swallows the side-car's end
    marker, and its answer is lost. The description is the only place
    the model can learn that before it waits out a minute.
    """
    desc = _description()
    assert STDIN_POLICY in desc, (
        "the tool description no longer carries the contract's stdin "
        "sentence -- the model reads the description, not the contract")
    assert "/dev/null" in desc, (
        "the description has to offer the way out, not just the trap")


def test_the_operator_facing_prose_carries_the_contract_posture():
    """The documents quote the contract, they do not paraphrase it.

    A paraphrase is a second copy, and a second copy is what went stale
    in the first place.
    """
    for doc in DOCS:
        lines = [line.strip() for line in
                 doc.read_text(encoding="utf-8").splitlines()]
        for sentence in SANDBOX_POSTURE_LINES:
            assert sentence in lines, (
                f"{doc.name} does not carry this sentence from the "
                f"contract: {sentence!r}. It has to quote it verbatim.")


def test_a_posture_is_stated_through_the_sandbox_knobs_and_not_in_prose():
    """Capabilities are named by the knob that sets them.

    The sandbox decides what it drops and whether new privileges are
    allowed; the operator decides by setting three variables. A document
    that asserts a posture in words has a second source of truth, and
    that is the failure this whole file is about.
    """
    for path in PROSE:
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            low = line.lower()
            if "cap_drop" in low or "no-new-privileges" in low:
                assert ("AGENT_CAP_DROP" in line
                        or "AGENT_NO_NEW_PRIVILEGES" in line), (
                    f"{path.name} states the sandbox's posture in prose on "
                    f"line {number}: {line.strip()!r}. Name the knob that "
                    "sets it instead.")


def test_a_writable_rootfs_is_stated_through_its_knob():
    """The rootfs is a knob, and the prose has to name it.

    v2.10.0 turned the root filesystem into a switch:
    `read_only: "${AGENT_READ_ONLY:-true}"`. A document that says
    "read-only rootfs" in words carries a second copy of the truth --
    and in the production deployment that copy is false, because the
    rootfs is writable there so a privileged root can install
    packages.
    """
    joined = " ".join(SANDBOX_POSTURE_LINES)
    if WRITABLE_ROOTFS:
        assert "AGENT_READ_ONLY" in joined, (
            "the deployment runs with a writable rootfs, so the posture "
            "has to name the knob that says so")
        assert "read-only rootfs" not in joined, (
            "the posture still calls the rootfs read-only, and in this "
            "deployment it is not")
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        assert "read-only rootfs" not in text, (
            f"{doc.name} still calls the rootfs read-only; the rootfs is "
            "a knob now (AGENT_READ_ONLY), and the deployment sets it")


def test_the_old_posture_claim_does_not_come_back():
    """A blunt instrument, aimed at one sentence."""
    for path in PROSE:
        text = path.read_text(encoding="utf-8")
        for claim in STALE_CLAIMS:
            assert claim not in text, (
                f"{path.name} still carries the pre-privilege-mode claim "
                f"{claim!r}")


def test_the_documents_quote_the_sandbox_defaults_as_the_sandbox_states_them():
    """The numbers come from the sandbox's compose file, not from memory."""
    compose = _compose_text()
    doc = (ROOT / "docs" / "BASH-SANDBOX.md").read_text(encoding="utf-8")
    for key, pattern in COMPOSE_VALUES:
        found = pattern.search(compose)
        assert found, (
            f"the sandbox's compose file has no {key} the way this test "
            "expects -- the sandbox changed, and this test has to follow")
        quoted = f"{key}: {found.group(1)}"
        assert quoted in doc, (
            f"docs/BASH-SANDBOX.md does not quote the sandbox's own "
            f"{quoted}. A number about the sandbox has to come from the "
            "sandbox.")
    for knob, pattern in COMPOSE_KNOBS:
        found = pattern.search(compose)
        assert found, (
            f"the sandbox's compose file does not define {knob} the way "
            "this test expects -- the sandbox changed, and this test has "
            "to follow")
        fragment = f"{knob}:-{found.group(1)}"
        assert fragment in doc, (
            f"docs/BASH-SANDBOX.md does not quote the sandbox's own "
            f"{fragment}. The knob and its default are how the posture is "
            "stated.")


def test_a_403_from_the_door_is_a_permission_and_a_401_is_the_token():
    """The two statuses mean different things, and the bot says which.

    The proxy answers 401 for a token it does not accept. The agent
    answers 403 for a file it cannot read. Before the sandbox v2.9.1 the
    second case arrived as a truncated body, and the bot called it an
    unreachable side-car -- the wrong component, and the operator loses
    an afternoon to the network instead of to the file's mode.
    """
    denied = ft._error_for_status(403, '{"detail": "permission denied: '
                                    'the sandbox agent cannot read '
                                    'root-only.bin. The file\'s owner or '
                                    'mode does not allow it; make it '
                                    'readable, e.g. chmod 644."}')
    assert "cannot read" in denied, denied
    assert "token" not in denied.lower(), (
        f"a 403 is a permission, not a token problem: {denied!r}")
    assert "unreachable" not in denied.lower(), (
        f"a 403 is not a transport failure: {denied!r}")

    refused = ft._error_for_status(401, '{"detail": "invalid token"}')
    assert "token" in refused.lower(), refused


@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_is_never_reported_as_a_transport_failure(status):
    """The diagnosis has to name the component that is at fault."""
    message = ft._error_for_status(status, '{"detail": "nope"}')
    assert "unreachable" not in message.lower(), (
        f"HTTP {status} was reported as an unreachable side-car: "
        f"{message!r}")
