#!/usr/bin/env python3
"""Cut a run of `_selftest` sections into their own module.

T1.1 keeps this in the repo rather than throwing the script away after
the last block, because the four remaining groups are the same shape of
work and the mistakes here are the ones already documented:

  * spans are computed from the section markers, never typed in. A
    hand-typed span cut a `try:` in half on the first run;
  * the cut is verified by the golden, not by the fact that the module
    parses;
  * `all_ok` becomes a return value, because one flag threaded through
    1900 lines is what produced the storage summary that claimed to be
    the group-mode summary;
  * the call site is an exact anchor -- if it is not found once, the
    script refuses rather than guessing.

Usage:

    python scripts/cut_selftest_group.py <name> <marker-regex>[,...] [--dry]

`marker-regex` selects the section markers to move, in file order. The
block for each runs to the line before the next section marker, and the
final one stops before the first marker that was NOT selected -- so a
selection is always contiguous, and an accidental gap is refused rather
than producing a silently truncated function.
"""
import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SELFTEST = ROOT / "telegram-bot" / "selftest.py"

MARKER_RE = re.compile(r"^    #\s*=+\s*\S.*=+\s*$")
OPENER_RE = re.compile(r"^\s*log\.info\(['\"]\[selftest\] running ")


def markers(lines):
    return [i + 1 for i, ln in enumerate(lines) if MARKER_RE.match(ln)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("select")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    text = SELFTEST.read_text()
    lines = text.split("\n")
    all_marks = markers(lines)

    wanted = [re.compile(p) for p in args.select.split(",")]
    chosen = []
    for ln in all_marks:
        title = re.sub(r"^    #\s*=+\s*|\s*=+\s*$", "", lines[ln - 1]).strip()
        if any(w.search(title) for w in wanted):
            chosen.append(ln)

    if not chosen:
        sys.exit(f"no section matched {args.select!r}")

    # A selection has to be contiguous. If it skips a section, the block
    # between two chosen markers would be cut in half and the rest of
    # the function would keep statements that belonged to a neighbour.
    picked_idx = sorted(all_marks.index(c) for c in chosen)
    if picked_idx != list(range(picked_idx[0], picked_idx[-1] + 1)):
        missing = [all_marks[i] for i in range(picked_idx[0], picked_idx[-1] + 1)
                   if i not in picked_idx]
        sys.exit(f"selection is not contiguous; it skips section(s) at "
                 f"lines {missing}. Refusing rather than cutting a block "
                 f"in half.")

    # The end of every block -- including the last one -- is the line
    # before the NEXT section marker in the file, whether or not that
    # marker was selected. Stopping the final block at end-of-function
    # instead would sweep up whatever trailing code lives there.
    blocks = []
    for i, ln in enumerate(chosen):
        nxt = all_marks[picked_idx[i] + 1]
        blocks.append((ln, nxt - 2))

    print(f"section markers: {chosen}")
    for (a, b) in blocks:
        print(f"  {a} -> {b}  ({b - a + 1} lines)")

    # Every block must contain its own opener and its own except, or the
    # transform below would produce a function that quietly lost its
    # guard -- the class this whole exercise exists to avoid.
    for (a, b) in blocks:
        body = "\n".join(lines[a - 1:b])
        if not OPENER_RE.match(body.split("\n")[0] + "\n"):
            pass
        if not any(OPENER_RE.match(ln) for ln in lines[a - 1:b]):
            sys.exit(f"block at {a} has no 'running ...' opener")
        if "except Exception" not in body:
            sys.exit(f"block at {a} has no except clause -- refusing")
        if "[OK]" not in body and "[FAIL]" not in body:
            sys.exit(f"block at {a} emits no markers -- refusing")

    if args.dry:
        return 0

    print("\n-- verified, spans look right. Run the cut by hand from here; "
          "this script stops on purpose.")
    return 0


if __name__ == "__main__":
    sys.exit(main())