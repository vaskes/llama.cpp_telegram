"""No commit may credit a model that wrote none of it.

On 2026-10-23 this repository listed two contributors on its GitHub
page: `vaskes`, and `claude`. Claude Opus 4.8 was there because of
exactly one thing -- a

    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

trailer on 21 commits that Mavis had written. GitHub counts a co-author
trailer as a contribution, so the model appeared as a contributor having
written nothing, holding no key and opening no pull request.

`.mailmap` cannot prevent this. Git applies the file to the AUTHOR
field, GitHub applies it to the author field, and neither one applies it
to a co-author trailer -- that line is parsed as written. The trailers
had to leave the history; `.mailmap` records what that cost.

This test keeps them out. Claude Code is not used to work on this
repository, so a trailer from it is always a false attribution. If that
ever stops being true, this file is the thing to delete, in the same
commit that starts using it.

**What counts as a trailer.** By git's own convention a trailer is a
`Key: value` line in the LAST paragraph of a commit message. The first
version of this test searched the whole message, and the first thing it
caught was this repository's own CHANGELOG quoting the trailer in prose.
A check that fires on a document describing the problem is a check that
will be disabled by whoever writes the next document about it -- so the
boundary is explicit here, and pinned by the last two tests.

What it reads: the commit messages of whatever history the checkout can
see. In CI the checkout is shallow, so that is the HEAD commit alone --
which is the case that matters, because a new commit is where a trailer
would reappear. Locally it is the whole history.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Claude Code's address. The model name in the trailer changes with the
# model; the address does not.
FORBIDDEN = "noreply@anthropic.com"

TRAILER_KEY = "Co-Authored-By:"


def _messages():
    """Every commit message this checkout can see, or skip."""
    try:
        proc = subprocess.run(
            # safe.directory: the checkout is often owned by another uid
            # than the one running the suite -- a containerised run against
            # a host-mounted clone, for instance. Without it git refuses
            # to read the repository and the test skips itself, which is
            # the one outcome that must not be silent.
            # %x00 separates the messages, so a message that contains a
            # blank line cannot be confused with the next message.
            ["git", "-c", "safe.directory=*", "log",
             "--format=%B%x00", "--all"],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git is not usable here: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"git history is not readable here: {proc.stderr.strip()}")
    return proc.stdout


def _trailer_lines(message):
    """The trailer block of one commit message: its last paragraph."""
    blocks = [b for b in message.strip().split("\n\n") if b.strip()]
    if not blocks:
        return []
    return blocks[-1].split("\n")


def _credits_claude(message):
    """The detector, separated so that the controls can call it."""
    return [line for line in _trailer_lines(message)
            if line.startswith(TRAILER_KEY) and FORBIDDEN in line]


def test_no_commit_credits_claude_code():
    """The property: no commit message carries Claude Code's trailer."""
    hits = []
    for message in _messages().split("\x00"):
        hits.extend(_credits_claude(message))
    assert not hits, (
        f"{len(hits)} commit message(s) credit Claude Code with "
        f"{FORBIDDEN}. GitHub counts a co-author trailer as a "
        f"contribution, so the model shows up on the contributors list "
        f"without having written a line of this repository. Remove the "
        f"trailer from the commit.\n  " + "\n  ".join(hits[:5]))


def test_the_history_is_read_at_all():
    """Control: the check above must be looking at something."""
    messages = _messages()
    assert messages.strip(), (
        "git log returned nothing -- the check above is vacuous, and it "
        "would pass on a repository with every trailer still in place")


def test_the_detector_can_see_a_trailer():
    """Control: the detector must be able to fire."""
    sample = (
        "fix(x): something\n\n"
        "The body says why.\n\n"
        "Co-Authored-By: Claude Opus 4.8 (1M context) "
        "<noreply@anthropic.com>\n")
    assert _credits_claude(sample), (
        "the detector cannot see the thing it was written for, so every "
        "green run above means nothing")


def test_a_mention_in_the_body_is_not_a_trailer():
    """Control: the boundary. A document quoting the trailer is not a
    commit crediting Claude -- and the first version of this test
    could not tell the two apart, which is how it ended up failing on
    this repository's own CHANGELOG."""
    sample = (
        "fix(x): something\n\n"
        "The body quotes the old trailer:\n\n"
        "    Co-Authored-By: Claude Opus 4.8 (1M context) "
        "<noreply@anthropic.com>\n\n"
        "and says it was removed.\n")
    assert not _credits_claude(sample), (
        "a quoted trailer in the body was read as a real one -- the "
        "check would fire on any document describing the problem")
