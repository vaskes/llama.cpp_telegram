"""One `[Unreleased]` section, and no release section without a tag.

`CHANGELOG.md` had **two** sections headed `## [Unreleased]`: the live
one at the top, and one written on 2026-10-10 that stayed in that
name through fourteen releases. A reader who wants to know what has
changed since the last tag lands on a file with two answers, and the
older one is longer.

Nothing caught it, because nothing looked. The debt register has a
tripwire, the tech-debt documents have one, the sizes have one -- and
the changelog, which is the document a user actually reads when
deciding what to run, had none.

So this file is the tripwire, and it is deliberately narrow: it does
not require the changelog to be complete or tidy. It requires that it
does not become *ambiguous*, and that the two ways it can go wrong --
a section with no tag, a tag with no section -- do not grow.

The known mismatches are listed below rather than fixed here. Fixing
the changelog's history is a job of its own; what this file does is
make sure that job does not get bigger while nobody is looking.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\]", re.MULTILINE)

# A section that describes a release is named after one: `v0.13.0-stable`.
# Anything else in a level-2 heading is a note -- "Earlier versions", or
# the group-mode section that was renamed out of `[Unreleased]` -- and is
# not expected to have a tag.
RELEASE_NAME = re.compile(r"^v\d")

# Release headings with no matching tag, and why.
#
# A tag may be named differently from the section that describes it --
# `v0.7.0` was tagged `v0.7` -- and a section may explicitly point at
# the release that absorbed it. Both are facts about the past, and both
# are recorded here so that a *new* mismatch is a red test.
SECTIONS_WITHOUT_A_TAG = {
    "v0.7.0": "the release was tagged `v0.7`, not `v0.7.0`",
    "v0.12.1-pre": "absorbed into v0.13.0-stable; no such tag was ever pushed",
}

# Tags with no section of their own. Some of these are pre-releases
# that were folded into a later release, some are patch tags whose
# changes are described inside a neighbouring section.
TAGS_WITHOUT_A_SECTION = {
    "v0.6.3": "hotfix, described inside the v0.6.2 section",
    "v0.7": "the tag the v0.7.0 section refers to",
    "v0.7.1": "chain tag, described in the v0.7.0 section",
    "v0.7.2": "chain tag, described in the v0.7.0 section",
    "v0.7.3": "chain tag, described in the v0.7.0 section",
    "v0.7.4": "chain tag, described in the v0.7.0 section",
    "v0.7.5": "chain tag, described in the v0.7.0 section",
    "v0.7.6": "chain tag, described in the v0.7.0 section",
    "v0.7.7": "chain tag, described in the v0.7.0 section",
    "v0.7.8": "chain tag, described in the v0.7.0 section",
    "v0.7.9": "chain tag, described in the v0.7.0 section",
    "v0.7.9b": "chain tag, described in the v0.7.0 section",
    "v0.8.1": "chain tag, described in the v0.8.0 section",
    "v0.8.2": "chain tag, described in the v0.8.0 section",
    "v0.9.7": "patch tag, described in the v0.9.6 section",
    "v0.10.0": "pre-release, folded into v0.10.1",
    "v0.11.0-pre": "pre-release, folded into v0.11.0-stable",
    "v0.12.0-pre": "pre-release, folded into v0.12.0-stable",
}

TEXT = CHANGELOG.read_text(encoding="utf-8")
SECTIONS = HEADING.findall(TEXT)
RELEASES = [s for s in SECTIONS if RELEASE_NAME.match(s)]


def _tags():
    """Tags in this checkout.

    `actions/checkout` fetches a depth-1 history and no tags, so in CI
    the local tag list is empty and a check that reads it would skip --
    which is how a tripwire quietly stops being one. The remote is
    asked as well: `git ls-remote --tags origin` works in CI, where
    the checkout has already been authenticated.

    Falls back to an empty list when neither works -- a tarball has no
    git at all, and a test that cannot run is not a test.
    """
    local = _run_git(["git", "-c", "safe.directory=*", "tag", "--list"])
    if local is not None and local.strip():
        return [t for t in local.split() if t]
    remote = _run_git(["git", "-c", "safe.directory=*", "ls-remote",
                       "--tags", "origin"])
    if remote is None:
        return []
    names = set()
    for line in remote.splitlines():
        ref = line.split()[-1] if line.split() else ""
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            name = name[:-3]
        names.add(name)
    return sorted(names)


def _run_git(cmd):
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


TAGS = set(_tags())


def test_the_changelog_was_read_at_all():
    """A guard on the guard: if the heading pattern stops matching, every
    check below would pass by finding nothing."""
    assert len(SECTIONS) > 15, (
        f"only found {len(SECTIONS)} sections in CHANGELOG.md -- the "
        f"heading pattern stopped matching")
    assert len(RELEASES) > 15, (
        f"only found {len(RELEASES)} release sections -- the release-name "
        f"pattern stopped matching")


def test_there_is_exactly_one_unreleased_section():
    """The defect this file exists for. Two sections called
    `[Unreleased]` means a reader cannot tell which one is current."""
    unreleased = [s for s in SECTIONS if s.lower().startswith("unreleased")]
    assert len(unreleased) == 1, (
        f"found {len(unreleased)} [Unreleased] sections: {unreleased}. "
        f"Exactly one, and it is the live one at the top of the file.")


def test_the_unreleased_section_is_the_first_one():
    """A live `[Unreleased]` below a released section reads as if that
    release were not released yet."""
    live = [s for s in SECTIONS if s.lower().startswith("unreleased")]
    assert live, "no [Unreleased] section at all"
    first_release = next(
        (i for i, s in enumerate(SECTIONS) if not s.lower().startswith("unreleased")),
        len(SECTIONS))
    unreleased_at = SECTIONS.index(live[0])
    assert unreleased_at < first_release, (
        f"the [Unreleased] section is at position {unreleased_at}, after a "
        f"released section at {first_release}")


def test_release_sections_are_unique():
    """Two sections with the same name means one of them is a copy."""
    seen = {}
    for name in RELEASES:
        seen[name] = seen.get(name, 0) + 1
    dupes = {n: c for n, c in seen.items() if c > 1}
    assert not dupes, f"duplicate section headings: {dupes}"


def test_every_release_section_has_a_tag():
    """A section that describes a release nobody can clone is a section
    about a release that does not exist."""
    if not TAGS:
        pytest.skip("no git tags in this checkout -- see _tags()")
    missing = []
    for name in RELEASES:
        if name in TAGS:
            continue
        if name in SECTIONS_WITHOUT_A_TAG:
            continue
        missing.append(name)
    assert not missing, (
        f"these sections describe a release with no tag: {missing}. Either "
        f"push the tag, or record the mismatch in "
        f"SECTIONS_WITHOUT_A_TAG with why.")


def test_every_tag_has_a_section():
    """A tag that nobody wrote about is a release whose notes exist
    nowhere except the tag itself."""
    if not TAGS:
        pytest.skip("no git tags in this checkout -- see _tags()")
    described = set(RELEASES)
    missing = sorted(
        t for t in TAGS
        if t not in described and t not in TAGS_WITHOUT_A_SECTION)
    assert not missing, (
        f"these tags have no section in CHANGELOG.md: {missing}. Either "
        f"write the section, or record the tag in TAGS_WITHOUT_A_SECTION "
        f"with why.")


def test_the_two_registries_have_not_rotted():
    """An entry that no longer describes a mismatch is one more thing to
    believe without checking."""
    described = set(RELEASES)
    stale_sections = sorted(
        n for n in SECTIONS_WITHOUT_A_TAG
        if n in described and n in TAGS)
    assert not stale_sections, (
        f"{stale_sections} are listed as having no tag, but they do have "
        f"one -- drop the entry")

    stale_tags = sorted(t for t in TAGS_WITHOUT_A_SECTION if t in described)
    assert not stale_tags, (
        f"{stale_tags} are listed as having no section, but they do have "
        f"one -- drop the entry")
