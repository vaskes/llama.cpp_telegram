"""The panel audit, tested without a network.

The audit is the second layer of the defence: the tripwire stops a
model credit from being committed, and the audit notices if one is on a
panel anyway -- a credit that arrived by another route, or a panel
that was stale.

The logic is tested here, offline, because a check that can only be run
against the live site is a check that runs when the site is reachable
and not when it is needed. The fixtures are the panel payloads that
were MEASURED on 2026-10-31, including the uids.

The tripwire below is deliberately separate from the tripwire that
guards the commit history: one reads the history, the other reads the
panel. They fail for different reasons and neither substitutes for the
other.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# The ledger names this file as the marker of tech-debt item 15, and the
# reconciler wants the two sides to name each other.
pytestmark = pytest.mark.tech_debt(item=15)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = ROOT / "scripts" / "audit_contributor_panels.py"


def _load_module():
    """The script under test, loaded from its path."""
    spec = importlib.util.spec_from_file_location("audit_contributor_panels", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUDIT = _load_module()


def _panel(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_the_script_is_where_the_check_says_it_is():
    """A guard that is not there guards nothing."""
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"


def test_a_clean_panel_is_clean():
    """A panel with only human accounts passes."""
    node = AUDIT.fetch_panel.__wrapped__ if hasattr(AUDIT.fetch_panel, "__wrapped__") else None
    entries = AUDIT.panel_entries(_panel("panel_green.json")["contributors"])
    assert entries == [("vaskes", 988884), ("Hyphaed", 19622367)]
    assert AUDIT.denied_on_panel(_panel("panel_green.json")["contributors"]) == []


def test_a_model_account_on_the_panel_is_denied():
    """The check that matters."""
    node = _panel("panel_denied.json")["contributors"]
    hits = AUDIT.denied_on_panel(node)
    assert hits == [("claude", 81847)], (
        f"the model account on the panel was not found: {hits}")


def test_the_match_is_by_uid_and_not_by_login():
    """A renamed account is still the same account.

    Login heuristics break on rename; a uid does not. The fixture
    carries a login that reads like a different thing entirely.
    """
    node = _panel("panel_renamed.json")["contributors"]
    hits = AUDIT.denied_on_panel(node)
    assert hits == [("claude-2", 81847)], (
        "the denied account was missed after a rename -- the check is "
        "matching on the login, and a login is not an identity")


def test_every_denied_account_is_caught():
    """All three measured accounts, not just the first one."""
    for fixture, login, uid in (
            ("panel_denied.json", "claude", 81847),
            ("panel_openai.json", "codex", 267193182),
            ("panel_cursor.json", "cursoragent", 199161495)):
        node = _panel(fixture)["contributors"]
        hits = AUDIT.denied_on_panel(node)
        assert hits == [(login, uid)], (
            f"{fixture}: expected {login} ({uid}), got {hits}")


def test_a_human_whose_login_sounds_like_a_model_is_not_denied():
    """`devin` and `mavis` are people, and they are on real panels.

    A check that matched on the login would take them down. The uid is
    what separates the account from the name.
    """
    node = _panel("panel_human_named_claude.json")["contributors"]
    assert AUDIT.denied_on_panel(node) == [], (
        "a human account was denied because of its name")


def test_an_entry_without_an_uid_is_not_guessed_at():
    """No uid means no identity, so no verdict.

    Guessing from a login here would put the heuristic back.
    """
    node = _panel("panel_no_uid.json")["contributors"]
    assert AUDIT.panel_entries(node) == [("someone-without-an-avatar", None)]
    assert AUDIT.denied_on_panel(node) == []


def test_a_sidebar_without_a_contributors_node_is_an_error():
    """An unreadable panel is not a clean one.

    This is the failure that a check can hide: the endpoint answers, the
    JSON parses, and the thing being checked is simply absent.
    """
    with pytest.raises(AUDIT.PanelError) as excinfo:
        node = _panel("sidebar_no_contributors.json").get("contributors")
        if not isinstance(node, dict):
            raise AUDIT.PanelError("the sidebar carried no contributors node")
    assert "contributors" in str(excinfo.value)


def test_a_panel_that_cannot_be_read_is_unreadable_and_not_clean(monkeypatch):
    """The failure mode that hides: an absent panel reads as an empty one.

    An endpoint that answers, a JSON that parses, and the thing being
    checked simply missing -- that is a clean bill of health for a panel
    that was never looked at. The status has to say UNREADABLE, and the
    run has to fail on it.
    """
    def _raise(owner, repo, timeout=30):
        raise AUDIT.PanelError(f"{owner}/{repo}: HTTP 404 (private or missing)")

    monkeypatch.setattr(AUDIT, "fetch_panel", _raise)
    report = AUDIT.audit(["vaskes/whatever"], baseline={})
    record = report["results"][0]
    assert record["status"] == "UNREADABLE", (
        f"an unreadable panel was reported as {record['status']!r}")
    assert "404" in record["detail"]


def test_the_run_fails_when_a_panel_could_not_be_read(monkeypatch):
    """And the failure reaches the exit status, not just the report."""
    def _raise(owner, repo, timeout=30):
        raise AUDIT.PanelError(f"{owner}/{repo}: HTTP 404")

    monkeypatch.setattr(AUDIT, "fetch_panel", _raise)
    status = AUDIT.main(["--repos", "vaskes/whatever"])
    assert status == 1, (
        f"the run exited {status} with an unreadable panel; a check that "
        "cannot see must not pass")


def test_the_run_fails_when_a_model_account_is_on_a_panel(monkeypatch):
    """The end-to-end verdict, with the network replaced by a fixture."""
    fixture = _panel("panel_denied.json")

    def _fake(owner, repo, timeout=30):
        return fixture["contributors"]

    monkeypatch.setattr(AUDIT, "fetch_panel", _fake)
    status = AUDIT.main(["--repos", "vaskes/whatever"])
    assert status == 1, (
        f"the run exited {status} with a model account on the panel")


def test_the_run_passes_on_a_clean_panel(monkeypatch):
    """And it passes when there is nothing to report."""
    fixture = _panel("panel_green.json")

    def _fake(owner, repo, timeout=30):
        return fixture["contributors"]

    monkeypatch.setattr(AUDIT, "fetch_panel", _fake)
    status = AUDIT.main(["--repos", "vaskes/whatever"])
    assert status == 0, f"the run exited {status} on a clean panel"


def test_the_scope_is_derived_and_not_only_the_written_list(monkeypatch):
    """A repository nobody remembered to write down is still audited.

    The written list is the operator's declaration of what should be
    covered. The enumeration is what GitHub says exists. A repository
    that is in the second and not in the first is a repository created
    after the list was written -- and a credit nobody looks for is a
    credit that hangs.
    """
    def _fake_fetch(owner, repo, timeout=30):
        return _panel("panel_green.json")["contributors"]

    monkeypatch.setattr(AUDIT, "fetch_panel", _fake_fetch)
    monkeypatch.setattr(AUDIT, "fetch_owner_repos",
                        lambda owner, token=None, timeout=30:
                        ["vaskes/written", "vaskes/brand-new"])
    monkeypatch.setattr(AUDIT, "parse_repos",
                        lambda args: ["vaskes/written"])

    scope, absent = AUDIT.resolve_scope(
        ["vaskes/written"], ["vaskes/written", "vaskes/brand-new"])
    assert absent == [], f"nothing is absent here: {absent}"
    assert "vaskes/brand-new" in scope, (
        f"a repository that is not in the written list was not in scope: "
        f"{scope}")

    report = AUDIT.audit(scope, baseline={},
                         declared={"vaskes/written"})
    audited = [record["repo"] for record in report["results"]]
    assert "vaskes/brand-new" in audited, (
        f"a repository that is not in the written list was not audited: "
        f"{audited}")
    undeclared = [r for r in report["results"] if r.get("declared") is False]
    assert [r["repo"] for r in undeclared] == ["vaskes/brand-new"], (
        "the repository that is not in the written list was not marked as "
        "such -- the operator cannot see that the scope drifted")


def test_a_scope_that_cannot_be_read_fails_the_run(monkeypatch):
    """An audit that could not find the repositories found nothing clean.

    The enumeration is what makes the audit complete. If it cannot be
    read, the run must not pass on the shorter list it happened to have.
    """
    def _boom(owner, token=None, timeout=30):
        raise AUDIT.PanelError(f"the repository list of {owner} could not be read")

    monkeypatch.setattr(AUDIT, "fetch_owner_repos", _boom)
    status = AUDIT.main(["--owner", "vaskes", "--repos", "vaskes/whatever"])
    assert status == 2, (
        f"the run exited {status} with an unreadable scope; a check that "
        "cannot see must not pass")


def test_a_repository_that_is_gone_is_reported_and_not_counted_clean(monkeypatch):
    """Deleted, renamed, or private: reported, and not passed over.

    Such a repository has no panel to read, so it is not audited and it
    does not fail the run. But it IS named in the report: a project of
    the operator's that the audit cannot see is worth knowing about.
    """
    def _fake_fetch(owner, repo, timeout=30):
        return _panel("panel_green.json")["contributors"]

    monkeypatch.setattr(AUDIT, "fetch_panel", _fake_fetch)
    monkeypatch.setattr(AUDIT, "fetch_owner_repos",
                        lambda owner, token=None, timeout=30:
                        ["vaskes/here"])
    monkeypatch.setattr(AUDIT, "parse_repos",
                        lambda args: ["vaskes/here", "vaskes/gone"])

    audited, absent = AUDIT.resolve_scope(
        ["vaskes/here", "vaskes/gone"], ["vaskes/here"])
    assert absent == ["vaskes/gone"], (
        f"the repository that is gone was not reported: {absent}")
    assert "vaskes/gone" not in audited, (
        "a repository with no panel was audited as if it had one")
    assert "vaskes/here" in audited


def test_the_denied_list_is_exactly_the_measured_accounts():
    """The deny list is measured, not remembered.

    Each entry is a uid that was resolved from an address and then found
    on a real panel. A uid that was never confirmed must not be here:
    an unconfirmed uid denies a person.
    """
    assert AUDIT.DENIED == {
        81847: "claude",
        267193182: "codex",
        199161495: "cursoragent",
    }


def test_every_baseline_entry_states_a_reason_and_a_date():
    """An exemption without a reason is a hole with a comment on it."""
    baseline = AUDIT.load_baseline()
    assert baseline, "the baseline file is missing or empty"
    for repo, entry in baseline.items():
        assert "/" in repo, f"{repo} is not owner/name"
        assert len(entry["reason"]) > 60, (
            f"{repo}: the reason is too short to be a reason")
        assert entry["recorded"], f"{repo}: no date on the exemption"


def test_an_exempt_repository_is_reported_and_not_hidden():
    """The exemption makes the run pass; it must not make it invisible."""
    report = AUDIT.audit(
        ["vaskes/greenboost"],
        baseline={"vaskes/greenboost": {"reason": "because", "recorded": "2026-10-31"}})
    record = report["results"][0]
    assert record["status"] == "KNOWN"
    assert record["denied"], "the account on the panel was dropped from the report"
    assert "because" in record["reason"]


def test_without_the_exemption_the_same_repository_is_denied():
    """Withdraw the record and the repository turns red.

    An exemption that cannot be withdrawn is a permanent hole; this is
    the test that says the record is the only thing holding it open.
    """
    report = AUDIT.audit(["vaskes/greenboost"], baseline={})
    assert report["results"][0]["status"] == "DENIED"
