"""The deploy workflow is a door, and the door's properties are checked.

T-FREEZE-AUTO-3, 2026-10-27. The workflow deploys the bot to the
production host when a tag is pushed, and a workflow is code with
production access -- so the properties that make it safe are pinned
here rather than left to the next person to read:

  * **it deploys on a tag, not on every push.** A deploy on every push
    would put an unreviewed commit on the production host;
  * **a pull request never deploys.** The `pull_request` trigger runs
    with a read-only token and no secrets -- and a workflow that
    deployed from it would be a deploy anyone could trigger;
  * **the token can read the repository and nothing else.** A deploy
    job needs the source, not the ability to push;
  * **the host's key is pinned.** A workflow that accepts whatever
    answers the port is a workflow that deploys to whoever answered;
  * **the key in the secrets is the only credential**, and it is
    named, not inlined -- a key pasted into a workflow file is a key
    in the repository's history;
  * **the topology is not in the repository.** The compose file, the
    `.env` and the volume are the operator's; the workflow says WHEN
    to deploy and the host's script says HOW. A workflow that carried
    its own copy of the topology would be a second topology, and the
    one nobody updates.

The file is checked as text, not as YAML: the test environment has no
YAML parser, and a dependency added for one check is a dependency
everybody has to keep.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


@pytest.fixture(scope="module")
def text() -> str:
    assert WORKFLOW.is_file(), (
        f"{WORKFLOW} does not exist -- the tag push has to deploy the bot, "
        "and the trigger that does it is a file")
    return WORKFLOW.read_text(encoding="utf-8")


def _section(text: str, name: str) -> str:
    """The block under a top-level key, up to the next top-level key."""
    lines = text.splitlines()
    out = []
    inside = False
    for line in lines:
        if not inside:
            if line.rstrip() == f"{name}:" or line.startswith(f"{name}:"):
                inside = True
            continue
        if line and not line[0].isspace():
            break
        out.append(line)
    return "\n".join(out)


def test_it_deploys_on_a_tag_and_not_on_every_push(text):
    triggers = _section(text, "on")
    assert triggers, "the workflow has no 'on:' block -- it never runs"
    assert "tags:" in triggers, (
        "the deploy must hang on a tag: a tag is a release, and a release "
        "is the operator's own action")
    assert "branches:" not in triggers, (
        "a push to a branch must not deploy; the tag is the trigger")
    assert "pull_request" not in triggers, (
        "a pull request must never deploy -- anyone can open one")


def test_the_workflow_can_be_run_by_hand(text):
    """The operator needs a way to deploy a specific commit without
    cutting a release for it."""
    triggers = _section(text, "on")
    assert "workflow_dispatch" in triggers, (
        "there is no manual trigger: without one, deploying a hotfix means "
        "cutting a release")


def test_the_token_can_read_the_repository_and_nothing_else(text):
    permissions = _section(text, "permissions")
    assert permissions, "the workflow has no permissions block"
    assert "contents: read" in permissions, (
        "the deploy job needs to read the source, and nothing else")
    for write in ("contents: write", "packages: write", "id-token: write",
                  "actions: write"):
        assert write not in permissions, (
            f"the deploy workflow asks for {write!r}; a deploy job that can "
            "write to the repository can also push a change nobody reviewed")


def test_the_host_key_is_pinned(text):
    assert "StrictHostKeyChecking=yes" in text, (
        "the host's key is not pinned -- a workflow that accepts whatever "
        "answers the port deploys to whoever answered")
    for loose in ("StrictHostKeyChecking=no", "StrictHostKeyChecking=accept-new",
                  "UserKnownHostsFile=/dev/null"):
        assert loose not in text, f"{loose!r} is not pinning anything"


def test_the_credentials_are_named_not_inlined(text):
    for secret in ("DEPLOY_SSH_KEY", "DEPLOY_HOST", "DEPLOY_PORT",
                   "DEPLOY_KNOWN_HOSTS"):
        assert f"secrets.{secret}" in text, (
            f"the workflow does not use the secret {secret} by name")
    assert "PRIVATE KEY" not in text, (
        "the file carries key material; a key in a workflow file is a key "
        "in the repository's history")
    assert "BEGIN OPENSSH" not in text, "the file carries key material"


def test_the_topology_is_not_in_the_repository(text):
    """The compose file, the .env and the volume are the operator's.
    The host's deploy script holds them; a second copy here is a second
    copy that nobody updates.

    The check reads the commands, not the prose: a comment that NAMES
    the host's script is documentation, and a check that cannot tell
    the two apart is a check that reddens on a good comment.
    """
    commands = "\n".join(line for line in text.splitlines()
                         if not line.strip().startswith("#"))
    for borrowed in ("docker compose", "docker-compose", "telegram-bot/app",
                     "/opt/telegrambot", "conversations.db", "tb-bot"):
        assert borrowed not in commands, (
            f"the workflow carries {borrowed!r} -- the deployment topology "
            "belongs to the host's script, not to the repository")


def test_the_deploy_is_one_ssh_call(text):
    """One command, one host. The forced command on the host decides
    what the sha may do."""
    calls = re.findall(r"^\s*ssh ", text, re.M)
    assert len(calls) == 1, (
        f"the workflow makes {len(calls)} ssh calls; the deploy is one "
        "command on one host")


def test_the_check_can_see_a_wrong_value():
    """Control. A check that cannot fail is decoration."""
    good = "on:\n  push:\n    tags:\n      - 'v*'\n"
    assert "tags:" in good
    assert "branches:" not in good
    bad = "on:\n  push:\n    branches:\n      - main\n"
    assert "branches:" in bad
