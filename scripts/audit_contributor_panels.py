#!/usr/bin/env python3
"""Audit the contributors panel of a set of repositories.

The panel is the thing the operator actually sees: the avatar strip on
the repository's front page. It is drawn from an endpoint that the
public documentation does not name, so this script reads the panel
itself rather than a proxy:

    GET https://github.com/{owner}/{repo}/_sidebar
        Accept: application/json
        X-Requested-With: XMLHttpRequest

The JSON carries a "contributors" node with "contributorCount" and a
list of entries; each entry has a "login" and an "avatarUrl" of the
form https://avatars.githubusercontent.com/u/{uid}?s=64&v=4. The uid is
what makes the check work: a login can be renamed, a uid cannot. This
script matches on uids, and the uid list it uses was MEASURED, not
guessed -- each address was resolved through the commits API and the
result was confirmed by finding the account on a real panel.

Addresses that do not resolve to an account cannot put a model on a
panel, so they are not in the list. That is the difference between this
check and a keyword search for "claude" in the commit log: a keyword
search flags every human being who ever mentioned the word, and gets
switched off by the first person who writes a document about the
incident.

**The scope is derived, not written by hand.** A hand-written list of
repositories is a list that stops being true the day the next
repository is created -- and a credit nobody looks for is a credit that
hangs. So the script asks GitHub which repositories the owner has
(`GET /users/{owner}/repos`) and audits every one of them, and the
written file becomes the *declaration* of what should be covered: a
repository that is enumerated but not declared is still audited (so
nothing hides), and it is reported as UNDECLARED, because the scope of
the check is the operator's decision and not the script's.

If the enumeration cannot be read, the run fails. An audit that could
not find the repositories is not an audit that found them clean.

The script is stdlib-only and needs no token for public data: every
repository it checks is public, and GitHub serves the sidebar
anonymously. A token is used when one is offered, because it raises the
API rate limit.

Exit status:
    0 -- every panel is clean, or holds only accounts in the baseline
    1 -- a panel holds a denied account, or a panel could not be read
    2 -- the check could not run (network, rate limit, bad input)

Usage:
    python3 scripts/audit_contributor_panels.py --owner vaskes
    python3 scripts/audit_contributor_panels.py --repos owner/name,...
    python3 scripts/audit_contributor_panels.py --repos-file fleet.txt
    python3 scripts/audit_contributor_panels.py --owner vaskes --json
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# --- the denied accounts -------------------------------------------------
#
# Each entry is (uid, login-at-measurement-time). The uid is the
# identity; the login is only there to make the report readable.
#
# Measured 2026-10-31 by resolving each address through the commits API
# and then confirming the account on a real panel:
#
#   noreply@anthropic.com    -> claude        uid 81847
#   noreply@openai.com       -> codex         uid 267193182
#   cursoragent@cursor.com   -> cursoragent   uid 199161495
#
# NOT in the list, because they resolve to nothing and appear on no
# panel: gemini@google.com, mavis@kuduza.ai, noreply@mavis.local. A
# check that forbids them would forbid a word, not an account.

DENIED = {
    81847: "claude",
    267193182: "codex",
    199161495: "cursoragent",
}

# --- the baseline --------------------------------------------------------
#
# A repository that is a copy of someone else's project carries their
# attribution, and that attribution is not ours to erase. Rewriting the
# history of a GPL project to remove a name is destroying the credit the
# licence requires.
#
# Such repositories are listed in a separate file next to this script,
# so that adding one is a data change the operator can make and read
# without touching code. Every entry states the repository, the reason,
# and the date it was recorded -- an exemption without a reason is a
# hole with a comment on it.

BASELINE_FILE = Path(__file__).with_name("contributor-panel-baseline.json")


def load_baseline(path: Path | None = None) -> dict:
    """The recorded exemptions, as {repo: {reason, recorded}}."""
    target = path or BASELINE_FILE
    if not target.is_file():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    entries = data.get("exemptions") or {}
    return {
        repo: {"reason": entry.get("reason", ""),
               "recorded": entry.get("recorded", "")}
        for repo, entry in entries.items()
    }

SIDEBAR_URL = "https://github.com/{owner}/{repo}/_sidebar"
HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "mavis-contributor-panel-audit/1.0",
}


class PanelError(RuntimeError):
    """The panel could not be read."""


def fetch_panel(owner: str, repo: str, timeout: int = 30) -> dict:
    """Read the contributors node of the repository's sidebar."""
    url = SIDEBAR_URL.format(owner=owner, repo=repo)
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # A private or missing repository. GitHub answers the same
            # way for both, so this is not a claim about which it is.
            raise PanelError(f"{owner}/{repo}: HTTP 404 (private or missing)") from exc
        raise PanelError(f"{owner}/{repo}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PanelError(f"{owner}/{repo}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PanelError(f"{owner}/{repo}: the response was not JSON") from exc

    node = payload.get("contributors")
    if not isinstance(node, dict):
        raise PanelError(f"{owner}/{repo}: the sidebar carried no contributors node")
    return node


def panel_entries(node: dict) -> list[tuple[str, int | None]]:
    """The accounts on the panel, as (login, uid) pairs."""
    entries = []
    for entry in node.get("contributors") or []:
        login = entry.get("login") or "?"
        uid = None
        avatar = entry.get("avatarUrl") or ""
        marker = "/u/"
        index = avatar.find(marker)
        if index >= 0:
            tail = avatar[index + len(marker):]
            digits = ""
            for char in tail:
                if char.isdigit():
                    digits += char
                else:
                    break
            if digits:
                uid = int(digits)
        entries.append((login, uid))
    return entries


def denied_on_panel(node: dict) -> list[tuple[str, int | None]]:
    """The denied accounts currently on the panel."""
    hits = []
    for login, uid in panel_entries(node):
        if uid is not None and uid in DENIED:
            hits.append((login, uid))
    return hits


def audit(repos: list[str], baseline: dict | None = None,
          declared: set[str] | None = None) -> dict:
    """Audit every repository, and say what could not be read.

    `declared` is the written scope -- what the operator says should be
    covered. A repository that is in the enumeration but not in it is
    still audited, and is marked UNDECLARED: the scope is the
    operator's decision, and this script does not make it silently.
    """
    baseline = baseline if baseline is not None else load_baseline()
    results = []
    for full in repos:
        if "/" not in full:
            raise ValueError(f"not owner/name: {full!r}")
        owner, repo = full.split("/", 1)
        record = {"repo": full}
        try:
            node = fetch_panel(owner, repo)
        except PanelError as exc:
            record["status"] = "UNREADABLE"
            record["detail"] = str(exc)
            results.append(record)
            continue
        record["contributor_count"] = node.get("contributorCount")
        record["entries"] = panel_entries(node)
        hits = denied_on_panel(node)
        if hits:
            record["denied"] = [
                {"login": login, "uid": uid, "known_as": DENIED.get(uid)}
                for login, uid in hits
            ]
        exemption = baseline.get(full)
        if hits and not exemption:
            record["status"] = "DENIED"
        elif hits and exemption:
            # A recorded exemption is reported, not hidden: the account
            # is named, the reason is printed, and the run still passes.
            # The day the exemption is withdrawn, this row turns red.
            record["status"] = "KNOWN"
            record["reason"] = exemption["reason"]
            record["recorded"] = exemption["recorded"]
        else:
            record["status"] = "CLEAN"
        if declared is not None and full not in declared:
            record["declared"] = False
        results.append(record)
    return {"results": results}


REPOS_API = "https://api.github.com/users/{owner}/repos?per_page=100&type=public"


def fetch_owner_repos(owner: str, token: str | None = None,
                      timeout: int = 30) -> list[str]:
    """Every public repository the owner has.

    This is what makes the audit complete: a repository created after
    the list was written is audited too, without anybody remembering to
    add it.

    A failure here is a failure of the audit, not an empty result: it
    raises, and the caller turns that into a non-zero exit.
    """
    def _get(with_token: bool):
        request = urllib.request.Request(
            REPOS_API.format(owner=owner),
            headers={**HEADERS, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        if with_token and token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        payload = _get(True)
    except urllib.error.HTTPError as exc:
        if token and exc.code in (401, 403):
            # A token that the API will not take is not a reason to
            # shrink the scope. The data is public: fall back to the
            # anonymous call, which has a lower rate limit and sees the
            # same repositories.
            try:
                payload = _get(False)
            except urllib.error.HTTPError as inner:
                raise PanelError(f"the repository list of {owner} could not be "
                                 f"read: HTTP {inner.code}") from inner
        else:
            raise PanelError(f"the repository list of {owner} could not be "
                             f"read: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PanelError(f"the repository list of {owner} could not be read: "
                         f"{exc}") from exc
    except json.JSONDecodeError as exc:
        raise PanelError(f"the repository list of {owner} was not JSON") from exc
    if not isinstance(payload, list):
        raise PanelError(f"the repository list of {owner} was not a list")
    return sorted(item.get("full_name") for item in payload
                  if item.get("full_name"))


def parse_repos(args: argparse.Namespace) -> list[str]:
    """The repositories to check, from --repos and/or --repos-file."""
    repos: list[str] = []
    if args.repos:
        repos.extend(part.strip() for part in args.repos.split(",") if part.strip())
    if args.repos_file:
        with open(args.repos_file, encoding="utf-8") as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if line:
                    repos.append(line)
    seen = set()
    unique = []
    for repo in repos:
        if repo not in seen:
            seen.add(repo)
            unique.append(repo)
    return unique


def resolve_scope(declared: list[str],
                  enumerated: list[str]) -> tuple[list[str], list[str]]:
    """What to audit, and what is not there any more.

    The enumeration is authoritative about what exists. A repository in
    the written scope that it does not list is gone, renamed, or
    private: there is no panel to read, so it is not audited and it does
    not fail the run -- but it IS reported, because a project of the
    operator's that the audit cannot see is worth knowing about and
    must not be passed over in silence.

    A repository in the enumeration and not in the written scope is
    audited anyway. The written scope is a declaration; the audit does
    not narrow itself to what somebody remembered to write down.
    """
    if not enumerated:
        return list(declared), []
    absent = [repo for repo in declared if repo not in enumerated]
    repos = list(declared) + [repo for repo in enumerated if repo not in declared]
    audited = [repo for repo in repos if repo not in absent]
    return audited, absent


def render_text(report: dict) -> str:
    """A report a human can read in a CI log."""
    lines = []
    counts: dict[str, int] = {}
    for record in report["results"]:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    lines.append("## Contributors panel audit")
    lines.append("")
    for record in report["results"]:
        status = record["status"]
        undeclared = "" if record.get("declared", True) else \
            " — **UNDECLARED**: not in the written scope, audited anyway"
        if status == "CLEAN":
            lines.append(f"- **{record['repo']}** — CLEAN "
                         f"({record.get('contributor_count', 0)} contributors)"
                         + undeclared)
        elif status == "KNOWN":
            names = ", ".join(
                f"`{d['login']}` (uid {d['uid']})"
                for d in record.get("denied", []))
            lines.append(f"- **{record['repo']}** — KNOWN, exempt by record "
                         f"({record.get('recorded', 'unrecorded')}): "
                         f"{record.get('reason', '')}"
                         + (f" — on the panel: {names}" if names else "")
                         + undeclared)
        elif status == "DENIED":
            names = ", ".join(
                f"`{d['login']}` (uid {d['uid']}, {d['known_as']})"
                for d in record["denied"])
            lines.append(f"- **{record['repo']}** — **DENIED**: {names}")
        else:
            lines.append(f"- **{record['repo']}** — UNREADABLE: "
                         f"{record.get('detail', '')}")
    for repo in report.get("absent") or []:
        lines.append(f"- **{repo}** — ABSENT: in the written scope, but not "
                     f"among the owner's public repositories (deleted, "
                     f"renamed, or private) — **not audited, and not "
                     f"covered by this check**")
    lines.append("")
    lines.append("Counts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--owner",
                        help="audit every public repository of this owner")
    parser.add_argument("--repos", help="comma-separated owner/name list")
    parser.add_argument("--repos-file", help="file with one owner/name per line")
    parser.add_argument("--json", action="store_true", help="print JSON, not text")
    parser.add_argument("--token", help="a GitHub token, to raise the rate limit")
    parser.add_argument("--allow-unreadable", action="store_true",
                        help="do not fail when a panel could not be read")
    args = parser.parse_args(argv)

    try:
        declared = parse_repos(args)
    except OSError as exc:
        print(f"could not read the repository list: {exc}", file=sys.stderr)
        return 2

    enumerated: list[str] = []
    if args.owner:
        try:
            enumerated = fetch_owner_repos(args.owner, token=args.token)
        except PanelError as exc:
            # An audit that could not find the repositories is not an
            # audit that found them clean.
            print(str(exc), file=sys.stderr)
            return 2

    repos, _ = resolve_scope(declared, enumerated)
    if not repos:
        print("no repositories to check", file=sys.stderr)
        return 2

    audited, absent = resolve_scope(declared, enumerated)

    report = audit(audited, declared=set(declared) if declared else None)
    if absent:
        report["absent"] = absent
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))

    statuses = {record["status"] for record in report["results"]}
    if "DENIED" in statuses:
        print("\nA model-owned account is on a contributors panel. "
              "The panel is drawn from the history, so the credit is in a "
              "commit: an author field or a Co-Authored-By trailer in the "
              "last paragraph of a message.", file=sys.stderr)
        return 1
    if "UNREADABLE" in statuses and not args.allow_unreadable:
        print("\nSome panels could not be read. An unreadable panel is not "
              "a clean one; re-run or pass --allow-unreadable if you know "
              "why.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
