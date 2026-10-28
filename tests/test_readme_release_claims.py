"""The README's header claims are claims about this repository.

Two of them are numbers that rot silently, because nothing recomputes
them and a reader has no way to tell:

  * the badge that says how many tests there are -- the number is
    written by hand into two places (the alt text and the image URL),
    and a half-updated badge renders one number and says another;
  * the tag the clone command pins -- four copies of it (the README's
    header, the README's quick start, `docs/SETUP.md`,
    `docs/BASH-SANDBOX.md`) and the CHANGELOG's header, and they were
    all a release behind while the README looked perfect.

The tripwire recomputes both:

  * the test count is **collected from the tree** (`pytest --collect-only`),
    not remembered;
  * the current stable tag is read from the tags -- locally, and from
    the remote in CI, where a depth-1 checkout has none at all (a
    tripwire that skips in CI is not a tripwire).

The rule it protects: **a number in a badge is a claim, and somebody
has to recompute it.** The badge is the first thing a visitor reads;
a stale one is worse than no badge, because it looks verified.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"
CHANGELOG = ROOT / "CHANGELOG.md"
DOCKERFILE = ROOT / "telegram-bot" / "Dockerfile"

#: The badge's alt text and the number inside the image URL. Two places,
#: because the image is what a reader sees and the alt text is what a
#: screen reader says -- and they must not disagree.
BADGE_ALT = re.compile(r"\[!\[Tests:\s*(\d+)\]")
BADGE_IMAGE = re.compile(r"img\.shields\.io/badge/tests-(\d+)")

#: The version badge and the license badge.
PYTHON_BADGE = re.compile(r"\[!\[Python:\s*([\d.]+)\+?\]")
LICENSE_BADGE = re.compile(r"\[!\[License:\s*([A-Za-z0-9 .+-]+)\]")

#: The header's claim about the current release.
CURRENT_STABLE = re.compile(r"\*\*Current stable:\s*`(v[0-9A-Za-z._+-]+)`\*\*")

#: A clone command that pins a branch. The branch must be the current
#: stable release -- pinning "main" in a clone command is exactly the
#: mistake the README warns about three paragraphs above.
CLONE_PIN = re.compile(r"git clone (?:-[a-z-]+ )*--branch (v[0-9A-Za-z._+-]+)")

#: Only the CHANGELOG's header is a live claim; its sections are
#: history and may quote old pins on purpose.
CHANGELOG_HEADER_LINES = 40

COLLECTED = re.compile(r"(\d+) tests collected")


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _current_stable_tag() -> str | None:
    """The newest stable tag, locally or on the remote.

    `actions/checkout` fetches a depth-1 history and **no tags**, so in
    CI the local tag list is empty and a check that reads it would skip
    -- which is how a tripwire quietly stops being one. The remote is
    asked as well: `git ls-remote --tags origin` works in CI, where the
    checkout has already been authenticated.
    """
    names: set[str] = set()
    local = _git(["git", "-c", "safe.directory=*", "tag", "--list"])
    if local:
        names |= {t for t in local.split() if t}
    else:
        remote = _git(["git", "-c", "safe.directory=*", "ls-remote",
                       "--tags", "origin"])
        for line in remote.splitlines():
            ref = line.split()[-1] if line.split() else ""
            if not ref.startswith("refs/tags/"):
                continue
            name = ref[len("refs/tags/"):]
            if name.endswith("^{}"):
                name = name[:-3]
            names.add(name)

    def _key(tag: str) -> tuple[int, ...]:
        digits = re.findall(r"\d+", tag)
        return tuple(int(d) for d in digits)

    stable = [t for t in names if t.startswith("v") and t.endswith("-stable")]
    if not stable:
        return None
    return max(stable, key=_key)


def _git(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout


def _collected_tests() -> int | None:
    """How many tests the tree actually has.

    Collected, not remembered, and not remembered in the README either:
    the count comes out of pytest's own collector.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout + proc.stderr
    m = COLLECTED.search(out)
    if not m:
        return None
    return int(m.group(1))


def _clone_pins() -> list[tuple[str, str]]:
    """Every clone command that pins a branch, with the file it is in."""
    found: list[tuple[str, str]] = []
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for m in CLONE_PIN.finditer(text):
            found.append((str(path.relative_to(ROOT)), m.group(1)))
    readme = _readme()
    for m in CLONE_PIN.finditer(readme):
        found.append(("README.md", m.group(1)))
    header = "\n".join(CHANGELOG.read_text(encoding="utf-8")
                       .splitlines()[:CHANGELOG_HEADER_LINES])
    for m in CLONE_PIN.finditer(header):
        found.append(("CHANGELOG.md", m.group(1)))
    return found


def test_the_claims_were_read_at_all():
    """A guard on the guard.

    Every check below looks for a claim and compares it with a fact. If
    the pattern stops matching, they all pass by finding nothing -- so
    the patterns themselves are asserted here.
    """
    readme = _readme()
    assert BADGE_ALT.search(readme), (
        "the README has no `Tests: N` badge -- the pattern stopped "
        "matching, or the badge was removed")
    assert BADGE_IMAGE.search(readme), (
        "the README's test badge has no number in its image URL")
    assert PYTHON_BADGE.search(readme), (
        "the README has no Python version badge")
    assert LICENSE_BADGE.search(readme), (
        "the README has no license badge")
    assert CURRENT_STABLE.search(readme), (
        "the README's header does not name the current stable release")
    pins = _clone_pins()
    assert len(pins) >= 4, (
        f"only {len(pins)} clone commands pin a branch (found {pins}); "
        f"the pin was expected in the README, SETUP, BASH-SANDBOX and the "
        f"CHANGELOG's header")


def test_the_badge_counts_the_tests_that_are_there():
    """The badge's number is the tree's number.

    Both copies of the number are checked, because a badge whose alt
    text says one thing and whose image says another looks fine in the
    source and lies in the rendering.
    """
    readme = _readme()
    claimed = int(BADGE_ALT.search(readme).group(1))
    image = int(BADGE_IMAGE.search(readme).group(1))
    assert claimed == image, (
        f"the badge's alt text says {claimed} and its image says {image}. "
        f"The image is what the reader sees; the alt text is what the "
        f"screen reader says. They are the same number.")
    actual = _collected_tests()
    assert actual is not None, (
        "pytest could not collect the tests -- the badge's number cannot "
        "be checked against a tree that does not answer")
    assert claimed == actual, (
        f"the README's badge claims {claimed} tests, the tree has "
        f"{actual}. The badge rides in the same commit as the tests it "
        f"counts.")


def test_the_badge_agrees_with_the_dockerfile():
    """The version badge names the interpreter the image pins.

    The Dockerfile is the source of truth for what runs in production;
    the badge is what a visitor reads about it.
    """
    badge = PYTHON_BADGE.search(_readme()).group(1)
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    m = re.search(r"FROM python:([\d.]+)", dockerfile)
    assert m, "the Dockerfile does not pin a python image -- the badge's "
    pinned = m.group(1)
    assert badge == pinned, (
        f"the README's badge says Python {badge}, the image pins "
        f"{pinned}")


def test_the_license_badge_agrees_with_the_license_file():
    """The license badge names the license in the repository."""
    badge = LICENSE_BADGE.search(_readme()).group(1).strip()
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert badge.lower() in license_text.lower(), (
        f"the README's badge says {badge!r} and the LICENSE file does not "
        f"contain that name")


def test_the_readme_names_the_current_stable_release():
    """The header's claim is the release that is actually current.

    The claim was a release behind while the README looked perfect: it
    named v0.14.0 while v0.15.0 was the stable one.
    """
    claimed = CURRENT_STABLE.search(_readme()).group(1)
    current = _current_stable_tag()
    assert current is not None, (
        "no stable tag could be found, locally or on the remote -- the "
        "claim cannot be checked against a list that does not answer")
    assert claimed == current, (
        f"the README says the current stable is {claimed}; the newest "
        f"stable tag is {current}. The header is the first thing a reader "
        f"reads.")


def test_every_clone_command_pins_the_current_stable():
    """Every clone command pins the release, not `main`.

    Four copies of the pin, and all of them were a release behind at
    the same time -- the README's header, the README's quick start,
    SETUP and BASH-SANDBOX (plus the CHANGELOG's header). A pin that
    names an old tag is a reader who installs yesterday's release and
    files today's bug against it.
    """
    current = _current_stable_tag()
    assert current is not None, (
        "no stable tag could be found, locally or on the remote")
    wrong = [(where, tag) for where, tag in _clone_pins() if tag != current]
    assert not wrong, (
        f"these clone commands pin something other than {current}: {wrong}. "
        f"The pin is the current stable release -- the README warns "
        f"against cloning `main` two paragraphs above.")


#: The live documents that describe the harness in prose. The CHANGELOG
#: and the CODEBOOK are history and may quote old counts on purpose;
#: these three are read as the current state, so a number in them is a
#: claim about today.
LIVE_DOCS = ("README.md", "docs/HARNESS.md", "docs/TODO.md")

#: A test count written out in prose. The badge is the one place the
#: number lives -- see `test_the_badge_counts_the_tests_that_are_there`.
PROSE_COUNT = re.compile(r"\b\d{3,5} (tests|cases)\b")


def test_the_test_count_lives_in_the_badge_and_nowhere_else():
    """One number, one place.

    The README's component table used to say "2909 tests" and the
    harness document used to say "the 2909-case suite" -- both written
    by hand, both a release behind, and neither recomputed by
    anything. The badge is the place that has a tripwire behind it;
    every other copy is a number that rots silently, in the paragraph
    a reader trusts most.

    The CHANGELOG and the CODEBOOK are history, and history may quote
    the count of its day.
    """
    offenders = []
    for rel in LIVE_DOCS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for m in PROSE_COUNT.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}: {m.group(0)}")
    assert not offenders, (
        "a test count written in prose, outside the badge: "
        f"{offenders}. The badge is the one place the number lives -- "
        "point at the badge instead of copying it.")
