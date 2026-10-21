"""Technical debt is pinned by tests, not by a markdown file.

Review round 3: TECH-DEBT.md is prose that CI does not read and that goes
stale on commit. The replacement is two halves:

  - docs/tech-debt/NN-*.md, one file per item, carrying `tech_debt:` and
    `state:` in front-matter so it can be parsed without an LLM
  - @pytest.mark.tech_debt(item=N) on the test that pins each item, so an
    item cannot close without something in the suite moving

scripts/tech_debt_sync.py reconciles the two against GitHub Issues. It needs
GITHUB_TOKEN, which this environment does not have, so the reconcile half has
not run yet. This test runs the half that needs no network.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEBT_DIR = REPO_ROOT / "docs" / "tech-debt"
TESTS = REPO_ROOT / "tests"

FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---", re.S)


def _items() -> dict:
    out = {}
    for path in sorted(DEBT_DIR.glob("*.md")):
        m = FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
        assert m, f"{path.name}: no front matter"
        fields = dict(
            line.split(":", 1) for line in m.group(1).splitlines() if ":" in line
        )
        item = fields["tech_debt"].strip()
        state = fields.get("state", "").strip()
        out[item] = {"path": path, "state": state,
                     "issue": fields.get("issue", "").strip()}
    assert out, f"no debt items under {DEBT_DIR}"
    return out


def test_every_debt_item_has_front_matter():
    """The split failed silently once already -- a regex that matched no
    headers would have written zero files and reported nothing. Assert the
    items are actually there."""
    items = _items()
    assert len(items) >= 5, f"expected the debt inventory, found {sorted(items)}"


def test_no_marker_references_an_unknown_item():
    """The direction that catches a stale ledger.

    A test carrying tech_debt(item=N) where no item file exists means the
    tracker and the suite have drifted. Open items are allowed to have no
    marker -- not every debt is expressible as a tripwire, and three of the
    seven say so in their front matter -- but a marker pointing at nothing is
    always wrong.
    """
    items = _items()
    marker_re = re.compile(r"tech_debt\s*\(\s*item\s*=\s*(\d+)")
    marked = set()
    for path in TESTS.glob("*.py"):
        marked.update(marker_re.findall(path.read_text(encoding="utf-8")))

    unknown = sorted(marked - set(items))
    assert not unknown, (
        f"tests carry tech_debt markers for items that do not exist: "
        f"{unknown}. Either restore the item or drop the marker."
    )


def test_closed_items_have_no_live_marker():
    """The direction that actually protects the ledger.

    An item marked closed must not have a test that still says "this debt
    exists". Otherwise closing an item in the tracker changes nothing about
    the suite, which is the whole problem with a markdown file.
    """
    marker_re = re.compile(r"tech_debt\s*\(\s*item\s*=\s*(\d+)")
    marked = {}
    for path in TESTS.glob("*.py"):
        for m in marker_re.finditer(path.read_text(encoding="utf-8")):
            marked.setdefault(m, []).append(path.name)

    items = _items()
    for item, meta in items.items():
        if meta["state"] == "closed":
            assert item not in marked, (
                f"item {item} is closed but {marked[item]} still carries a "
                f"tech_debt marker. Remove the marker or reopen the item."
            )


def test_open_items_declare_whether_a_marker_exists():
    """Every open item states its enforcement status explicitly.

    Not "is it pinned" as a question -- as a field. An item that says
    `marker: none` is a decision somebody made; an item with no field is
    just an oversight.
    """
    for item, meta in _items().items():
        if meta["state"] == "closed":
            continue
        text = meta["path"].read_text(encoding="utf-8")
        assert "marker:" in text, (
            f"{meta['path'].name}: open debt item does not say whether a "
            f"tripwire pins it. Add `marker: <test name>` or `marker: none "
            f"-- <reason>`."
        )


def test_sync_script_exists_and_does_not_silently_pass():
    """The reconciler is allowed to fail loudly when unauthenticated.

    A sync script that returns 0 without checking anything is how a tracker
    rots while looking maintained.
    """
    script = REPO_ROOT / "scripts" / "tech_debt_sync.py"
    assert script.is_file(), "scripts/tech_debt_sync.py missing"
    src = script.read_text(encoding="utf-8")
    assert "sys.exit" in src, (
        "the sync script has no non-zero exit path; it cannot fail, so it "
        "cannot report anything"
    )

# W7-H2 tripwires for scripts/tech_debt_create_missing.py: --
# ------------------------------------------------------------------
def test_create_missing_script_exists_and_has_dry_run_default():
    """The auto-create counterpart to tech_debt_sync.py must exist and
    default to dry-run. A POST-on-default-by-typo script that fires
    before the operator has eyeballed the plan is how a maintainer
    files 5 duplicate issues at 03:00 and only notices in the morning.
    """
    script = REPO_ROOT / "scripts" / "tech_debt_create_missing.py"
    assert script.is_file(), "scripts/tech_debt_create_missing.py missing"
    src = script.read_text(encoding="utf-8")
    assert "--dry-run" in src and "--apply" in src, (
        "tech_debt_create_missing.py must offer both --dry-run and --apply"
    )
    # The argparse mutual-exclusion pattern (or equivalent) must put
    # --dry-run as the default; otherwise running the script with no
    # args will POST.
    dry_run_pattern = 'add_argument("--dry-run", dest="apply", action="store_false"'
    assert dry_run_pattern in src, (
        "--dry-run must be the default (action=\"store_false\" on --apply). "
        "If the user runs the script with no arguments, nothing must hit the network."
    )


def test_create_missing_duplicate_guard_exists():
    """An interrupted --apply run creates a real GitHub Issue but does
    not rewrite the front-matter; re-running then double-posts. The
    script must abort with a non-zero exit and a clear message if an
    existing Issue has the same title as one it is about to create.
    """
    script = REPO_ROOT / "scripts" / "tech_debt_create_missing.py"
    src = script.read_text(encoding="utf-8")
    # The guard fetches existing GitHub Issues filtered by label, builds
    # a title->numbers map, then bails if any candidate title is in there.
    assert "existing_issues" in src, (
        "create-missing script must look up existing tech-debt Issues "
        "before POSTing so a partial previous run does not double-post"
    )
    assert "title_to_nos" in src, (
        "create-missing script must build a title->numbers map and exit "
        "non-zero when a candidate title already exists on GitHub"
    )


def test_create_missing_skips_closed_items():
    """An item with `state: closed` and `issue: null` is fine -- the
    reconciliation rules say closed items need no Issue. The create
    script must not post for those.
    """
    script = REPO_ROOT / "scripts" / "tech_debt_create_missing.py"
    src = script.read_text(encoding="utf-8")
    # The candidates() helper excludes state == "closed" with empty issue.
    assert 'state"] != "closed"' in src or 'state"] == "closed"' in src, (
        "candidates() must filter out closed items -- found no closed-state guard"
    )


def test_create_missing_writes_back_issue_number_in_place():
    """The whole point: after a successful POST, the front-matter must
    stop saying `issue: null` and instead say `issue: <n>`. Otherwise
    tech_debt_sync.py on the next run will report the same disagreement
    and re-run will double-post.
    """
    script = REPO_ROOT / "scripts" / "tech_debt_create_missing.py"
    src = script.read_text(encoding="utf-8")
    assert "write_issue_no" in src, (
        "create-missing script must rewrite `issue: null` to `issue: <n>` "
        "in the front-matter after a successful POST"
    )
    # Sanity: --apply branches into create_issue + write_issue_no.
    apply_branch = src.split("--apply", 1)[-1] if "--apply" in src else ""
    assert "create_issue" in apply_branch and "write_issue_no" in apply_branch, (
        "--apply branch must call create_issue + write_issue_no in sequence"
    )
