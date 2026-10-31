"""No commit may credit a model account with work it did not do.

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

**Which slots can put a model in the panel.** Measured on 2026-10-31 by
reading the panel itself (`GET /{owner}/{repo}/_sidebar`, the endpoint
that feeds the contributors tile) for every repository where a model
account appears:

| repository | author | committer | trailer | model on the panel |
|---|---|---|---|---|
| `vaskes/greenboost` | 0 | 0 | 1 | `claude` |
| `hongik2023graduationproject/hongik-new-web` | 0 | 18 | 37 | `claude` |
| `lima1217/wellread` | 26 | 158 | 109 | `claude`, `cursoragent` |
| `sebyx07/git-history` | 10052 | 10052 | 3 | `claude` |
| `vaskes/rtx-5090-strata-nvfp4` | 12 | 12 | 1840 | `claude`, `codex` |

Two slots are doors, and the third one is not:

* the **author** field -- `sebyx07/git-history` has 10 052 commits
  authored by `noreply@anthropic.com` and the model is on the panel;
* the **last paragraph** of the commit message, where a
  `Co-Authored-By:` trailer lives -- `vaskes/greenboost` has exactly
  ONE such commit, no model-authored commit at all, and the model is on
  the panel.

The **committer** field is not a door. In
`hongik2023graduationproject/hongik-new-web` the model carried the
committer on 18 commits, and the panel's own count for it
(`graphs/contributors-data`, field `total`) is 37 -- the number of
trailers, exactly. Had the committer slot counted, that number would
have been 38. The committer field is what a `git rebase` or a
`git commit --amend` leaves behind; it is not a credit GitHub counts.

What GitHub's `total` counts beyond that (it is lower than the raw
number of credited commits on two of the five repositories, and the
arithmetic behind it was not reproduced here) does not change the rule.
The rule is about **membership**: the model is on the panel or it is
not, and these are the slots that put it there.

**Which address the check reads, and why it is the raw one.** The
author field is read **as the commit carries it** (`%ae`), not as
`.mailmap` rewrites it (`%aE`). GitHub's own criterion for a
contribution is that "the email address used to make or co-author the
commits is associated with your account on GitHub" -- the address in the
commit. Whether the contributors tile honours `.mailmap` is not
documented, and this repository's `.mailmap` maps
`noreply@anthropic.com` to the operator's own address, so a check that
trusted the mapping would be trusting something unmeasured.

The two mistakes are not the same size. A check that trusts the
mapping and GitHub does not honour it puts the model back on the panel
-- the exact failure this rule exists to prevent. A check that ignores
the mapping when GitHub does honour it asks the operator to amend one
commit. So the check reads the raw address, and a commit authored with
a model address is flagged even though the repository's `.mailmap` would
redirect it.

**Which addresses.** A credit only reaches the panel if GitHub can
resolve the address to an account. Measured on 2026-10-31, by asking
GitHub which account a commit's address belongs to:

| address | resolves to | reaches a panel |
|---|---|---|
| `noreply@anthropic.com` | `claude` (uid 81847) | **yes** |
| `noreply@openai.com` | `codex` (uid 267193182) | **yes** |
| `cursoragent@cursor.com` | `cursoragent` (uid 199161495) | **yes** |
| `gemini@google.com` | nothing | no |
| `mavis@kuduza.ai`, `noreply@mavis.local`, ... | nothing | no |

The last row is the reason the credits in this repository's README are
not a problem: `Mavis` and `Qwen` are credited there, and neither
address resolves to an account, so neither can put a link on a
contributors panel. The rule is not "the word Claude is forbidden" -- it
is "no model-owned account may be credited here".

`gemini@google.com` is the counter-example that keeps the list honest:
it is a model credit, it does not resolve to anything, and it does not
appear in a panel. A rule that forbade it would be forbidding a word,
not a link.

**What counts as a trailer.** By git's own convention a trailer is a
`Key: value` line in the LAST paragraph of a commit message. The first
version of this test searched the whole message, and the first thing it
caught was this repository's own CHANGELOG quoting the trailer in prose.
A check that fires on a document describing the problem is a check that
will be disabled by whoever writes the next document about it -- so the
boundary is explicit here, and pinned by the controls at the bottom.

**What it reads.** The commit messages and the author field of whatever
history the checkout can see. In CI that is the whole history, and it
has to be: a depth-1 pull-request checkout is the merge ref alone, so
`git log --all` sees one commit and this test passes on a branch that
carries the trailer. `tests/test_ci_history_depth.py` is what keeps
that from happening again.
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: The model-owned addresses that GitHub resolves to an account, and so
#: that put a link on a contributors panel. Measured 2026-10-31 -- see
#: the table in the module docstring.
#:
#: The model name in a trailer changes with the model; the address does
#: not. That is the whole reason this list is a list of addresses.
MODEL_ADDRESSES = (
    "noreply@anthropic.com",   # -> claude, uid 81847
    "noreply@openai.com",       # -> codex, uid 267193182
    "cursoragent@cursor.com",   # -> cursoragent, uid 199161495
)

TRAILER_KEY = "Co-Authored-By:"


def _history():
    """Every commit this checkout can see: author email and message.

    Or skip. The `%x00` separators mean a message containing a blank
    line cannot be confused with the next commit.
    """
    try:
        proc = subprocess.run(
            # safe.directory: the checkout is often owned by another uid
            # than the one running the suite -- a containerised run against
            # a host-mounted clone, for instance. Without it git refuses
            # to read the repository and the test skips itself, which is
            # the one outcome that must not be silent.
            ["git", "-c", "safe.directory=*", "log",
             "--format=%ae%x00%B%x00", "--all"],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git is not usable here: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"git history is not readable here: {proc.stderr.strip()}")
    return proc.stdout


def _records():
    """(author email, message) for every commit the checkout can see."""
    fields = _history().split("\x00")
    # The format emits two fields per commit, so the tail is empty.
    return [(fields[i], fields[i + 1])
            for i in range(0, len(fields) - 1, 2)]


def _trailer_lines(message):
    """The trailer block of one commit message: its last paragraph."""
    blocks = [b for b in message.strip().split("\n\n") if b.strip()]
    if not blocks:
        return []
    return blocks[-1].split("\n")


def _credits_model(text):
    """The detector, separated so that the controls can call it."""
    low = (text or "").lower()
    return [addr for addr in MODEL_ADDRESSES if addr in low]


def _credits_model_in_trailer(message):
    """The detector for the trailer slot: the LAST paragraph only.

    This is the same detector as `_credits_model`, applied to a narrower
    slice of the message -- and the narrowing is the whole point. A
    detector that searched the whole message would fire on this
    repository's own CHANGELOG, which quotes the old trailer while
    describing the incident; a check that fires on the document
    explaining the problem is a check the next person disables.
    """
    out = []
    for line in _trailer_lines(message):
        out.extend(_credits_model(line))
    return out


def _offenders():
    """Every credit of a model account, wherever it is.

    Two slots, both measured to reach the panel: the author field, and
    the trailer in the last paragraph.
    """
    hits = []
    for author, message in _records():
        for addr in _credits_model(author):
            hits.append(f"author: {addr}")
        for addr in _credits_model_in_trailer(message):
            hits.append(f"trailer: {addr}")
    return hits


def test_no_commit_credits_a_model_account():
    """The property: no commit credits a model-owned account.

    Not "no commit mentions Anthropic" -- the prose of this repository
    discusses the incident, and the discussion is not a credit. The two
    slots GitHub actually reads are the author field and the trailer in
    the last paragraph.
    """
    hits = _offenders()
    assert not hits, (
        f"{len(hits)} credit(s) of a model-owned account:\n  "
        + "\n  ".join(hits[:10])
        + "\n\nGitHub counts the author field and a co-author trailer as "
        "contributions, so a model credited here appears on the "
        "contributors list with nothing behind it -- no key, no pull "
        "request, no line of code. Remove the credit from the commit.")


def test_the_history_is_read_at_all():
    """Control: the check above must be looking at something."""
    records = _records()
    assert records, (
        "git log returned nothing -- the check above is vacuous, and it "
        "would pass on a repository with every credit still in place")


def test_the_detector_can_see_a_trailer():
    """Control: the detector must be able to fire."""
    sample = (
        "fix(x): something\n\n"
        "The body says why.\n\n"
        "Co-Authored-By: Claude Opus 4.8 (1M context) "
        "<noreply@anthropic.com>\n")
    assert _credits_model(sample), (
        "the detector cannot see the thing it was written for, so every "
        "green run above means nothing")


def test_the_detector_can_see_an_author_field():
    """Control: the second door.

    The author field is a separate slot from the trailer, and it is
    counted separately by GitHub -- so the detector has to reach it
    without the trailer's machinery.
    """
    assert _credits_model("noreply@anthropic.com"), (
        "the detector cannot see the author field, so a commit authored "
        "by a model account would pass")


def test_the_author_field_is_read_as_the_commit_carries_it(tmp_path):
    """The boundary on the author slot: `.mailmap` is not a defence.

    This repository's `.mailmap` maps `noreply@anthropic.com` to the
    operator's own address, so a commit authored with that address is
    claimed by the operator there. The check reads the address **as the
    commit carries it** and does not take the mapping as an alibi.

    The reason is the size of the two mistakes. GitHub's own criterion
    for a contribution names "the email address used to make or
    co-author the commits"; whether the contributors tile honours
    `.mailmap` is not documented. A check that trusted the mapping and
    GitHub did not honour it would put the model back on the panel --
    the exact failure this rule exists to prevent. The opposite mistake
    costs one amended commit.

    The test pins the choice in both directions: with the mapping, the
    address is still flagged; without the mapping it is flagged too.
    """
    def _repo(with_mailmap: bool) -> Path:
        path = tmp_path / ("mapped" if with_mailmap else "bare")
        path.mkdir()
        run = lambda *args: subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True,
            check=True)
        run("init", "-q")
        run("config", "user.name", "dev")
        run("config", "user.email", "dev@example.com")
        if with_mailmap:
            (path / ".mailmap").write_text(
                "<vaskes@kuduza.com> <noreply@anthropic.com>\n",
                encoding="utf-8")
        (path / "f.txt").write_text("x\n", encoding="utf-8")
        run("add", "-A")
        env = dict(os.environ,
                   GIT_AUTHOR_EMAIL="noreply@anthropic.com",
                   GIT_COMMITTER_EMAIL="noreply@anthropic.com")
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", "chore: x"],
            capture_output=True, text=True, check=True, env=env)
        return path

    for with_mailmap in (True, False):
        repo = _repo(with_mailmap)
        raw = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%ae"],
            capture_output=True, text=True, check=True).stdout
        assert "noreply@anthropic.com" in raw, (
            "the fixture does not carry the address it is supposed to")
        assert _credits_model(raw), (
            "the detector cannot see the address the commit carries")


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
    assert not _credits_model_in_trailer(sample), (
        "a quoted trailer in the body was read as a real one -- the "
        "check would fire on any document describing the problem")


def test_our_own_credits_are_not_a_violation():
    """Control: the boundary on the other side.

    `Mavis` and `Qwen` are credited in this repository's README and in
    its commit trailers, and that is not what this rule is about. Their
    addresses resolve to no account, so they cannot put a link on a
    contributors panel -- measured 2026-10-31, see the table in the
    module docstring.

    This test pins that. Without it, the obvious "fix" for a red run is
    to widen the ban until it covers the credits the operator asked to
    keep, and the rule would end up being "the word Claude is banned"
    rather than "no model account is credited here".
    """
    allowed = ("noreply@mavis.local", "mavis@kuduza.ai",
               "mavis-agent@kuduza.ai", "mavis@kuduza.com")
    for address in allowed:
        assert not _credits_model(address), (
            f"{address} is now treated as a model credit -- but it "
            "resolves to no GitHub account, so it cannot put a link on a "
            "contributors panel. The credits the operator asked to keep "
            "must stay allowed.")
