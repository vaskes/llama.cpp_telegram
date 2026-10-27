"""The release workflow is a door, and the door's properties are checked.

T-FREEZE-AUTO-3, 2026-10-27. The workflow builds the image and publishes
it on the release when a tag is pushed. It used to deploy the bot to the
production host over ssh -- and that was the wrong shape, for a reason
the operator said out loud: the runners' addresses are not on the
operator's router, and opening the router to all of GitHub's runners
(7081 CIDR, ~28.6 million addresses) to earn a deploy nobody asked for
is not a trade worth making. The deploy is the agent's: the agent
already has the access, and the host's own script already is the door.

So the workflow's job ended where the image is built, and the properties
that make it safe are pinned here rather than left to the next person to
read:

  * **it releases on a tag, not on every push.** A release on every push
    would put an unreviewed commit under a release name;
  * **a pull request never releases.** The `pull_request` trigger runs
    with a read-only token and no secrets -- and a workflow that
    released from it would be a release anyone could trigger;
  * **the workflow cannot reach the production host.** No ssh, no
    scp, no host address, no port, no key. The worst a compromised
    workflow can do is publish a release;
  * **the workflow holds no secret at all.** A secret in a workflow is
    a secret the workflow can leak;
  * **the token may write the release and nothing else.** Publishing the
    release needs `contents: write`; a deploy job used to need the
    repository to be read-only, and a job that can write to the
    repository can also push a change nobody reviewed;
  * **the image is built from the repository,** so the image on the
    release is the image that was reviewed;
  * **the image is attached to the release for that tag,** so the tag,
    the image and the running container are one fact;
  * **the tag is validated before it reaches a command.** A dispatch
    input is text somebody typed;
  * **the topology is not in the repository.** The compose file, the
    `.env` and the volume are the operator's; the host's script holds
    them. A workflow that carried its own copy of the topology would be
    a second topology, and the one nobody updates.

The file is checked as text, not as YAML: the test environment has no
YAML parser, and a dependency added for one check is a dependency
everybody has to keep.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def text() -> str:
    assert WORKFLOW.is_file(), (
        f"{WORKFLOW} does not exist -- the tag push has to build the image, "
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


def _commands(text: str) -> str:
    """The file without its comments.

    A comment that NAMES a thing is documentation; a check that cannot
    tell a comment from a command reddens on a good sentence.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))


def test_it_releases_on_a_tag_and_not_on_every_push(text):
    triggers = _section(text, "on")
    assert triggers, "the workflow has no 'on:' block -- it never runs"
    assert "tags:" in triggers, (
        "the release must hang on a tag: a tag is a release, and a release "
        "is the operator's own action")
    assert "branches:" not in triggers, (
        "a push to a branch must not release; the tag is the trigger")
    assert "pull_request" not in triggers, (
        "a pull request must never release -- anyone can open one")


def test_the_workflow_can_be_run_by_hand(text):
    """A release can be rebuilt by hand: a hotfix needs a new image
    under the same tag without cutting a second tag."""
    triggers = _section(text, "on")
    assert "workflow_dispatch" in triggers, (
        "there is no manual trigger: without one, rebuilding a release "
        "means cutting a new tag")
    assert "tag:" in triggers, (
        "the manual trigger takes no tag: without one there is nothing "
        "to rebuild")


def test_the_workflow_cannot_reach_the_production_host(text):
    """The CI's share of the work ends at the image.

    A workflow that can ssh to the host is a workflow whose host
    credentials are one leaked secret away from being used. The deploy
    door is the agent's access and the host's own script.
    """
    commands = _commands(text)
    for door in (r"\bssh\b", r"\bscp\b", r"\brsync\b", r"/dev/tcp",
                 r"\bnc\b", r"\bcurl\b", r"\bwget\b"):
        assert not re.search(door, commands), (
            f"the workflow reaches out with {door!r} -- the production host "
            "is not reachable from a CI runner, and the door into it is the "
            "agent's access, not the workflow's")
    for borrowed in ("DEPLOY_HOST", "DEPLOY_PORT", "DEPLOY_SSH_KEY",
                     "DEPLOY_KNOWN_HOSTS", "telegram-bot/app",
                     "/opt/telegrambot"):
        assert borrowed not in commands, (
            f"the workflow carries {borrowed!r} -- the production host is "
            "not the workflow's business")


def test_the_workflow_holds_no_secret(text):
    """No secret, and no key material. The workflow publishes a file
    anybody with read access can see."""
    commands = _commands(text)
    assert "secrets." not in commands, (
        "the workflow reads a secret -- a release build needs no "
        "credential of the operator's")
    for material in ("PRIVATE KEY", "BEGIN OPENSSH"):
        assert material not in commands, (
            f"the file carries {material!r}; key material in a workflow "
            "file is key material in the repository's history")
    for line in commands.splitlines():
        if "GH_TOKEN" in line or "GITHUB_TOKEN" in line:
            assert "github.token" in line, (
                "the workflow authenticates with something that is not the "
                "token GitHub mints for this run -- that would be a "
                "credential of the operator's in the repository")


def test_the_token_may_write_the_release_and_nothing_else(text):
    permissions = _section(text, "permissions")
    assert permissions, "the workflow has no permissions block"
    assert "contents: write" in permissions, (
        "publishing the release needs to write the release; the token "
        "that does it is the workflow's own")
    for write in ("packages: write", "id-token: write", "actions: write",
                  "deployments: write", "security-events: write"):
        assert write not in permissions, (
            f"the release workflow asks for {write!r}; a workflow that can "
            "do more than publish a release can do more than publish a "
            "release")


def test_the_image_is_built_from_the_repository(text):
    """The image on the release is built from the reviewed source, in
    the repository's own Dockerfile."""
    commands = _commands(text)
    assert "docker build" in commands, (
        "the release carries no image: the workflow's job is to build the "
        "image that will run in production")
    assert "telegram-bot/" in commands, (
        "the image is built from somewhere else than the bot's own "
        "directory")
    assert "docker compose" not in commands, (
        "the workflow runs a compose stack; the stack on the host is the "
        "operator's")


def test_the_image_is_attached_to_the_release_for_that_tag(text):
    """The release carries the image, under the tag that names it.

    The check reads the commands that talk to the release, not the file
    as a whole: the image's name also appears in the step that builds
    the tarball, and a file-wide search for it says nothing about
    whether the image was attached.
    """
    commands = _commands(text)
    release = [line for line in commands.splitlines() if "gh release" in line]
    assert release, (
        "the image is not attached to the release -- the release is the "
        "deployable unit, and without the image it is a name")
    assert any("gh release create" in line for line in release), (
        "the release is never created -- a tag with no release has no "
        "image to deploy")
    assert any("gh release upload" in line for line in release), (
        "a rebuilt release never replaces the image it already carries")
    assert all("$TAG" in line for line in release), (
        "a release command does not name the tag: the image would land "
        "on somebody else's release")
    assert any("bot-image.tar.gz" in line for line in release), (
        "the release carries something else than the image")


def test_the_tag_is_validated_before_it_reaches_a_command(text):
    """A dispatch input is text somebody typed, and a value that
    reaches a shell unvalidated is a value that can become a command.

    The check reads the pattern itself and asks it what it accepts: a
    check that only looks for the presence of a validation can be
    satisfied by a validation that accepts everything.
    """
    commands = _commands(text)
    match = re.search(r"grep -Eq '([^']+)'", commands)
    assert match, (
        "the tag is never validated; it reaches the build and the release "
        "as whatever somebody typed")
    pattern = match.group(1)
    build = commands.find("docker build")
    assert build != -1 and commands.find("grep -Eq") < build, (
        "the tag is validated after it has already been used")
    for good in ("v0.14.0", "v0.13.0-stable"):
        assert re.fullmatch(pattern, good), (
            f"the tag check refuses a real release tag: {good!r}")
    for bad in ("main", "v0.14.0; rm -rf /", "", "v0.14.0`id`",
                "v0.14.0 && curl example.com"):
        assert not re.fullmatch(pattern, bad), (
            f"the tag check accepts {bad!r} -- the tag reaches a shell")


def test_the_topology_is_not_in_the_workflow(text):
    """The compose file, the .env and the volume are the operator's.
    The host's deploy script holds them; a second copy here is a second
    copy that nobody updates.

    The release may CARRY the compose file as the reference the host
    starts from -- what it may not do is RUN a stack: the stack that
    runs in production is the operator's, and the deploy script owns
    it.
    """
    commands = _commands(text)
    for borrowed in ("docker compose", "telegram-bot/app",
                     "/opt/telegrambot", "conversations.db", "tb-bot"):
        assert borrowed not in commands, (
            f"the workflow carries {borrowed!r} -- the deployment topology "
            "belongs to the host's script, not to the repository")
    # The operator's env file, as a path. `os.environ` is not that.
    assert not re.search(r"(?<![\w.])\.env\b", commands), (
        "the workflow refers to the operator's .env -- the secrets and the "
        "endpoints are the host's business")


def test_the_check_can_see_a_wrong_value():
    """Control. A check that cannot fail is decoration."""
    good = "on:\n  push:\n    tags:\n      - 'v*'\n"
    assert "tags:" in good
    assert "branches:" not in good
    bad = "on:\n  push:\n    branches:\n      - main\n"
    assert "branches:" in bad
