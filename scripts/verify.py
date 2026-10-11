#!/usr/bin/env python3
"""Quick verify: production bot health + pytest suite.

The operator's pain point: "I'm tired of sending photos and clicking
buttons." This script gives a fast answer to "is the bot healthy
and do all automated tests pass?" in <5 seconds.

Run:
    python3 scripts/verify.py
"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_HEALTH_URL = "https://bot.kuduza.com/healthz"


def check_health() -> bool:
    """Hit the production /healthz endpoint."""
    try:
        r = httpx.get(PROD_HEALTH_URL, timeout=5.0)
        ok = r.status_code == 200 and "webhook-ok" in r.text
        if ok:
            print(f"✓ production /healthz: {r.text.strip()!r}")
        else:
            print(f"✗ production /healthz: status={r.status_code} body={r.text[:80]!r}")
        return ok
    except Exception as e:
        print(f"✗ production /healthz: {type(e).__name__}: {e}")
        return False


def run_pytest() -> bool:
    """Run the local pytest suite (60 tests, <1s)."""
    print("→ Running pytest suite (60 tests)...")
    # Skip the in-tree selftest test - it requires a real bot.py path inside
    # the Docker container, not what we want for a quick verify.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--no-header", "-q",
         "--deselect", "tests/test_selftest.py::test_in_tree_selftest"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    # Last line is the summary
    last_lines = result.stdout.strip().split("\n")[-3:]
    for line in last_lines:
        print(f"  {line}")
    return result.returncode == 0


def main() -> int:
    print("=" * 60)
    print("llamabot verify (production health + pytest)")
    print("=" * 60)
    print()
    
    start = time.time()
    health_ok = check_health()
    print()
    tests_ok = run_pytest()
    print()
    
    elapsed = time.time() - start
    print(f"Done in {elapsed:.1f}s")
    print()
    if health_ok and tests_ok:
        print("✓ ALL CHECKS PASSED — bot is healthy and tests are green")
        return 0
    else:
        print("✗ SOME CHECKS FAILED:")
        if not health_ok:
            print("  - production /healthz did not return 'webhook-ok'")
        if not tests_ok:
            print("  - pytest suite has failures")
        return 1


if __name__ == "__main__":
    sys.exit(main())
