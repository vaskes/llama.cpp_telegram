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


# The marker is resolved against docs/tech-debt/NN-*.md, i.e. the LEDGER
# item number. The GitHub Issue number is a different namespace and is not
# what item=N means -- two tests carried each other's numbers until a test
# forced the two sides to agree.
_MARKER_RE = re.compile(r"tech_debt\s*\(\s*item\s*=\s*(\d+)")


def _markers_in(text: str) -> list[str]:
    """tech_debt(item=N) markers in `text`, comments removed first.

    Comments are stripped per line rather than by regex across the file so
    that a marker stays findable when someone explains the namespace
    directly above `item=`. The first version of this did not, and a
    three-line comment inside pytestmark made every marker in that file
    vanish without a single assertion noticing -- "no marker here" is not a
    failure to any test written against it.
    """
    stripped = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    return _MARKER_RE.findall(stripped)


def _marker_owners() -> dict:
    """item number -> the test files that carry its marker."""
    owners: dict = {}
    for path in sorted(TESTS.glob("*.py")):
        for item in _markers_in(path.read_text(encoding="utf-8")):
            owners.setdefault(item, set()).add(path)
    return owners


def _items() -> dict:
    out = {}
    for path in sorted(DEBT_DIR.glob("*.md")):
        m = FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
        assert m, f"{path.name}: no front matter"
        fields = {}
        for line in m.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep:
                # Strip the trailing "# ..." comment. sync.py has always
                # done this; this parser did not, which is why `issue` read
                # as "1          # filled in when the Issue is created" and
                # nobody noticed -- nothing here reads issue. marker_kind
                # is not so forgiving.
                fields[key.strip()] = value.split("#", 1)[0].strip()
        item = fields["tech_debt"].strip()
        state = fields.get("state", "").strip()
        out[item] = {"path": path, "state": state,
                     "issue": fields.get("issue", "").strip(),
                     "marker_kind": fields.get("marker_kind", "guard").strip()
                                    or "guard",
                     "marker_field": fields.get("marker", "").strip()}
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
    marked = set(_marker_owners())

    unknown = sorted(marked - set(items))
    assert not unknown, (
        f"tests carry tech_debt markers for items that do not exist: "
        f"{unknown}. Either restore the item or drop the marker."
    )


def test_closed_items_have_no_live_guard_marker():
    """The direction that actually protects the ledger.

    An item marked closed must not carry a test that still asserts "this
    debt exists". Otherwise closing an item in the tracker changes nothing
    about the suite, which is the whole problem with a markdown file.

    A marker the item declares as `marker_kind: tripwire` is the opposite:
    it pins the fix so the item cannot come back. Those are expected on
    closed items, and only the declaration distinguishes them -- inferring
    it from the state would pass a stale guard and fail a legitimate
    tripwire.
    """
    marked = {}
    for item, paths in _marker_owners().items():
        marked[item] = sorted(p.name for p in paths)

    items = _items()
    for item, meta in items.items():
        if meta["state"] == "closed" and meta["marker_kind"] != "tripwire":
            assert item not in marked, (
                f"item {item} is closed but {marked[item]} still carries a "
                f"tech_debt guard marker. Remove the marker, or declare "
                f"`marker_kind: tripwire` if that test now pins the fix."
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


def test_marker_and_ledger_field_name_each_other():
    """The two sides must agree, in both directions.

    `marker:` in the ledger names the test that pins the item, and
    `tech_debt(item=N)` in the test names the item. Either alone can drift
    and nothing notices: today two tests carried each other's item number,
    and because both wrong numbers landed on a real, closed, tripwire-
    marked item, the reconciler reported every one as correctly pinned.

    This is the check that was missing. It is the reason the namespace is
    now stated in both places rather than inferred from whichever number
    was typed.
    """
    owners = _marker_owners()

    for item, meta in _items().items():
        declared = meta.get("marker_field", "")
        # "none", "none -- <reason>": the parser strips "#" comments but the
        # ledger writes the none-form reason with "--", so the value arrives
        # as "none -- a 1177-line function cannot be pinned...".
        if not declared or declared.startswith("none"):
            continue
        named = {n.strip() for n in declared.split(",") if n.strip()}
        for entry in named:
            filename = entry.rsplit("/", 1)[-1]
            assert (TESTS / filename).is_file(), (
                f"item {item} names {filename} as its marker and that file "
                f"does not exist"
            )
            assert item in owners and (TESTS / filename) in owners[item], (
                f"item {item} names {filename} as its marker, but that file "
                f"carries tech_debt(item={sorted(owners.get(item, {'?'}))}). "
                f"The ledger and the test must name the same item -- the "
                f"GitHub Issue number is a different namespace and is not "
                f"what the marker resolves against."
            )


def test_reconciler_and_this_file_see_the_same_markers():
    """Two implementations of one rule is the defect; the tripwire is that
    they agree, not either answer.

    They did not. The reconciler matched the raw source and the test file
    matched with comments stripped, so three of six markers were invisible
    to the reconciler and the count changed with no error anywhere. Both
    numbers were internally consistent, which is what made it survivable.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import tech_debt_sync
    finally:
        sys.path.pop(0)

    theirs = tech_debt_sync.marked_items()
    ours = set(_marker_owners())
    assert theirs == ours, (
        f"the reconciler sees {sorted(theirs, key=int)} but this file sees "
        f"{sorted(ours, key=int)}. One of the two marker readers is stale; "
        f"a marker counted by one and not the other makes every reconcile "
        f"result meaningless."
    )
