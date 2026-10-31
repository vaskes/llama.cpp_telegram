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

The script is stdlib-only and needs no token: every repository it
checks is public, and GitHub serves the sidebar anonymously.

Exit status:
    0 -- every panel is clean, or holds only accounts in the baseline
    1 -- a panel holds a denied account
    2 -- the check could not run (network, rate limit, bad input)

Usage:
    python3 scripts/audit_contributor_panels.py --repos owner/name,...
    python3 scripts/audit_contributor_panels.py --repos-file fleet.txt
    python3 scripts/audit_contributor_panels.py --repos ... --json
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


def audit(repos: list[str], baseline: dict | None = None) -> dict:
    """Audit every repository, and say what could not be read."""
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
        results.append(record)
    return {"results": results}


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
        if status == "CLEAN":
            lines.append(f"- **{record['repo']}** — CLEAN "
                         f"({record.get('contributor_count', 0)} contributors)")
        elif status == "KNOWN":
            names = ", ".join(
                f"`{d['login']}` (uid {d['uid']})"
                for d in record.get("denied", []))
            lines.append(f"- **{record['repo']}** — KNOWN, exempt by record "
                         f"({record.get('recorded', 'unrecorded')}): "
                         f"{record.get('reason', '')}"
                         + (f" — on the panel: {names}" if names else ""))
        elif status == "DENIED":
            names = ", ".join(
                f"`{d['login']}` (uid {d['uid']}, {d['known_as']})"
                for d in record["denied"])
            lines.append(f"- **{record['repo']}** — **DENIED**: {names}")
        else:
            lines.append(f"- **{record['repo']}** — UNREADABLE: "
                         f"{record.get('detail', '')}")
    lines.append("")
    lines.append("Counts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repos", help="comma-separated owner/name list")
    parser.add_argument("--repos-file", help="file with one owner/name per line")
    parser.add_argument("--json", action="store_true", help="print JSON, not text")
    parser.add_argument("--allow-unreadable", action="store_true",
                        help="do not fail when a panel could not be read")
    args = parser.parse_args(argv)

    try:
        repos = parse_repos(args)
    except OSError as exc:
        print(f"could not read the repository list: {exc}", file=sys.stderr)
        return 2
    if not repos:
        print("no repositories to check", file=sys.stderr)
        return 2

    report = audit(repos)
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
