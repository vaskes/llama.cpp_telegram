"""Create GitHub Issues for docs/tech-debt/*.md that lack an issue number.

Counterpart to tech_debt_sync.py. That script reads GitHub and reports
disagreements; this one writes the missing rows back.

Reads the same front-matter. For every item with `issue: null` and
`state != closed`, POSTs a GitHub Issue with title from the file's first
h2 ("## N. <title>"), body = everything after the closing ---,
labels = ["tech-debt"]. Then rewrites `issue: null` -> `issue: <n>`
in place.

Two modes:
  --dry-run  (default) print what would be created and where it would
              land, do not POST and do not touch the file. The exit
              code is 0 if all candidates parse cleanly, 1 otherwise.
  --apply                actually POST and rewrite.

Environment:
  GITHUB_TOKEN  Bearer token. Must have Issues: Read+Write on REPO.
  REPO          'owner/name' pair, e.g. vaskes/llama.cpp_telegram.

Refuses to run if there are no candidates, and refuses to commit
duplicates by checking the existing GitHub Issues for the `tech-debt`
label so the script does not double-post if it is interrupted and
re-run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBT_DIR = ROOT / "docs" / "tech-debt"
FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)
HEADING_RE = re.compile(r"^##\s*\d+\.\s*(.+?)\s*$", re.M)
LABEL = "tech-debt"


def parse_items() -> dict[str, dict]:
    items: dict[str, dict] = {}
    for path in sorted(DEBT_DIR.glob("*.md")):
        m = FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
        if not m:
            print(f"FAIL {path.name}: no front matter", file=sys.stderr)
            continue
        fields = {}
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            fields[name.strip()] = value.split("#", 1)[0].strip()
        key = fields.get("tech_debt", "").strip()
        if not key:
            continue
        body = m.group(2)
        title_m = HEADING_RE.search(body)
        title = title_m.group(1).strip() if title_m else path.stem
        items[key] = {
            "state": fields.get("state", ""),
            "issue": fields.get("issue", ""),
            "path": path,
            "title": title,
            "body": body.strip(),
        }
    return items


def candidates(items: dict[str, dict]) -> list[tuple[str, dict]]:
    out = []
    for k, meta in sorted(items.items(), key=lambda kv: int(kv[0])):
        issue = meta["issue"].rstrip(".")
        if (not issue or issue == "null") and meta["state"] != "closed":
            out.append((k, meta))
    return out


def existing_issues(repo: str, token: str) -> dict[str, str]:
    """Map issue_number -> title for every open or closed Issue with
    the tech-debt label. Used to detect duplicates from a previous
    interrupted run."""
    out: dict[str, str] = {}
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo}/issues?state=all&per_page=100&page={page}&labels={LABEL}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            page_items = json.load(resp)
        if not page_items:
            break
        for issue in page_items:
            if "pull_request" in issue:
                continue
            out[str(issue["number"])] = issue["title"]
        page += 1
    return out


def create_issue(repo: str, token: str, title: str, body: str) -> int:
    payload = {"title": title, "body": body, "labels": [LABEL]}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "tech_debt_create_missing.py",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.load(resp)
    return out["number"]


def write_issue_no(path: Path, issue_no: int) -> None:
    src = path.read_text(encoding="utf-8")
    new, count = re.subn(
        r"^issue:\s*\S+",
        f"issue: {issue_no}",
        src,
        count=1,
        flags=re.M,
    )
    if count != 1:
        raise RuntimeError(f"could not patch front-matter in {path}")
    path.write_text(new, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="default; do not POST")
    mode.add_argument("--apply", dest="apply", action="store_true", help="actually POST and rewrite")
    parser.set_defaults(apply=False)
    args = parser.parse_args()

    repo = os.environ.get("REPO")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo:
        print("FAIL: REPO is not set (e.g. 'vaskes/llama.cpp_telegram').", file=sys.stderr)
        return 2
    if args.apply and not token:
        print("FAIL: GITHUB_TOKEN is not set.", file=sys.stderr)
        return 2

    items = parse_items()
    needs = candidates(items)
    if not needs:
        print("Nothing to create: every item has an issue or is closed.")
        return 0

    # Duplicate guard: search GitHub for an Issue whose title matches one
    # we are about to create. If a previous run was interrupted after the
    # POST but before the front-matter rewrite, the new POST would
    # double-post. Bail with a clear message so a human can stitch.
    existing: dict[str, str] = {}
    if token:
        try:
            existing = existing_issues(repo, token)
        except urllib.error.HTTPError as e:
            print(f"WARN: could not fetch existing issues: HTTP {e.code}", file=sys.stderr)
    title_to_nos: dict[str, list[str]] = {}
    for n, t in existing.items():
        title_to_nos.setdefault(t, []).append(n)

    for k, meta in needs:
        if meta["title"] in title_to_nos:
            print(
                f"  ABORT item {k}: an issue with this title already exists at "
                f"#{', #'.join(title_to_nos[meta['title']])}. Either patch the "
                f"front-matter manually or rename the item before re-running.",
                file=sys.stderr,
            )
            return 3

    print(f"Plan: {'POST' if args.apply else 'would POST'} {len(needs)} issue(s) to {repo}\n")
    for k, meta in needs:
        rel = meta["path"].relative_to(ROOT)
        print(f"  item {k} ({rel})")
        print(f"    title : {meta['title']}")
        if args.apply:
            no = create_issue(repo, token, meta["title"], meta["body"])
            write_issue_no(meta["path"], no)
            print(f"    -> #{no} (rewrote front-matter)")
        else:
            body_preview = meta["body"].splitlines()[0] if meta["body"] else "(empty)"
            print(f"    body  : {body_preview[:80]}{'...' if len(body_preview) > 80 else ''}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to create the issues.")
    else:
        print(f"\nDone: {len(needs)} issue(s) created. Re-run tech_debt_sync.py to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
