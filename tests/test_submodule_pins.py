"""The submodule pins are commits, not branches.

`.gitmodules` used to carry `branch = main` (sandbox) and
`branch = master` (donsetch). The commit each submodule points at is
recorded in the index, so a clone gets exactly that tree -- but
`git submodule update --remote` follows the *branch*, and would move
the working tree to whatever the branch tip is. The README calls the
submodules pinned; a branch key says otherwise.

The branch key has no legitimate use here: the release ships an image
built from the recorded commits, and nothing in the release flow runs
`--remote`.

From the external review of 2026-10-28 (finding #2).
"""
import configparser
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _gitmodules() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False)
    parser.read(ROOT / ".gitmodules", encoding="utf-8")
    return parser


def test_no_submodule_points_at_a_branch():
    """The pin is the recorded commit. A branch key is a promise of a
    moving target."""
    parser = _gitmodules()
    sections = parser.sections()
    assert sections, ".gitmodules has no submodule sections -- the file the tripwire reads is not the one the repo has"
    for section in sections:
        assert not parser.has_option(section, "branch"), (
            f"{section} names a branch. The commit in the index is the pin; "
            f"a branch key makes `git submodule update --remote` move it")


def test_every_submodule_is_recorded_as_a_commit():
    """Each path in .gitmodules is recorded in the index as a gitlink --
    a commit, with a mode that says 'not a file'."""
    parser = _gitmodules()
    for section in parser.sections():
        path = parser.get(section, "path")
        out = subprocess.run(
            ["git", "ls-tree", "HEAD", path],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert out, f"{path} is in .gitmodules but the index does not record it"
        mode, kind, sha = out.split()[0], out.split()[1], out.split()[2]
        assert mode == "160000", f"{path} is recorded as {mode}, not a gitlink"
        assert kind == "commit", f"{path} is recorded as a {kind}"
        assert re.fullmatch(r"[0-9a-f]{40}", sha), f"{path} is pinned at {sha}, not a commit sha"


def test_the_urls_are_the_ones_the_contract_names():
    """Both submodules point at the repositories the tree documents."""
    parser = _gitmodules()
    urls = {parser.get(s, "url") for s in parser.sections()}
    assert "https://github.com/vaskes/OpenWebUI-bash-sandbox.git" in urls
    assert "https://github.com/dondai44423/donsetch.git" in urls
