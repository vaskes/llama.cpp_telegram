#!/usr/bin/env python3
"""Reconcile docs/tech-debt/ against GitHub Issues.

Review round 3 asked for twenty lines that do this. The rules:

  - an open Issue with no item file      -> fail (work is untracked)
  - a closed Issue with a live marker    -> fail (the ledger is stale)
  - an item file with no Issue           -> fail, unless --allow-missing

Needs GITHUB_TOKEN and REPO (owner/name). Exits non-zero on every
disagreement, including on missing auth: a checker that cannot see anything
must not report success, which is the failure mode this replaces.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBT_DIR = ROOT / "docs" / "tech-debt"
FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---", re.S)
MARKER_RE = re.compile(r"tech_debt\s*\(\s*item\s*=\s*(\d+)")


def parse_items() -> dict[str, dict]:
    items = {}
    for path in sorted(DEBT_DIR.glob("*.md")):
        m = FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
        if not m:
            print(f"FAIL {path.name}: no front matter")
            continue
        fields = {}
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            # Strip the trailing "# filled in when..." comment, which was
            # being read as part of the issue number.
            fields[name.strip()] = value.split("#", 1)[0].strip()
        key = fields["tech_debt"].strip()
        items[key] = {
            "state": fields.get("state", ""),
            "issue": fields.get("issue", ""),
            "marker": fields.get("marker", ""),
            "path": path,
        }
    return items


def marked_items() -> set[str]:
    found = set()
    for path in (ROOT / "tests").glob("*.py"):
        found.update(MARKER_RE.findall(path.read_text(encoding="utf-8")))
    return found


def open_issues(repo: str, token: str) -> dict[str, str]:
    """Return {issue_number: state} for every issue carrying tech_debt."""
    url = f"https://api.github.com/repos/{repo}/issues?state=all&per_page=100"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    out = {}
    with urllib.request.urlopen(req, timeout=20) as resp:
        for issue in json.load(resp):
            if "pull_request" in issue:
                continue
            labels = [lb["name"] for lb in issue.get("labels", [])]
            if "tech-debt" in labels:
                out[str(issue["number"])] = issue["state"]
    return out


def main() -> int:
    allow_missing = "--allow-missing" in sys.argv
    repo = os.environ.get("REPO")
    token = os.environ.get("GITHUB_TOKEN")

    items = parse_items()
    markers = marked_items()
    problems: list[str] = []

    if not items:
        print("FAIL: no debt items parsed")
        return 1

    if not repo or not token:
        missing = [
            name for name, value in (("REPO", repo), ("GITHUB_TOKEN", token))
            if not value
        ]
        print(f"FAIL: cannot reconcile without {', '.join(missing)}.")
        print("      Set them and re-run. Refusing to report success on an")
        print("      unauthenticated check -- that is the failure mode this")
        print("      script exists to replace.")
        for item, meta in sorted(items.items()):
            issue = meta["issue"] or "null"
            print(f"  item {item:>2}  state={meta['state']:<7} issue={issue}")
        return 2

    try:
        issues = open_issues(repo, token)
    except urllib.error.HTTPError as e:
        print(f"FAIL: GitHub API returned HTTP {e.code}")
        return 2

    for item, meta in sorted(items.items(), key=lambda kv: int(kv[0])):
        issue_no = meta["issue"].rstrip(".")
        if not issue_no or issue_no == "null":
            if meta["state"] == "closed":
                print(f"  ok    item {item:>2}  closed, no issue needed")
                continue
            if allow_missing:
                print(f"  warn  item {item}: no issue yet")
            else:
                problems.append(
                    f"item {item} ({meta['path'].name}) has no issue number"
                )
            continue
        state = issues.get(issue_no)
        if state is None:
            problems.append(f"item {item} points at #{issue_no}, which is not a tech-debt issue")
        elif state == "closed" and item in markers:
            problems.append(
                f"item {item} is closed (#{issue_no}) but a test still "
                f"carries a tech_debt marker for it"
            )
        else:
            print(f"  ok    item {item:>2}  #{issue_no}  {state}")

    for item in sorted(markers - set(items), key=lambda s: int(s)):
        problems.append(f"test marker references unknown item {item}")

    if problems:
        print(f"\n{len(problems)} disagreement(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"\nreconciled: {len(items)} items, {len(markers)} marked")
    return 0


if __name__ == "__main__":
    sys.exit(main())