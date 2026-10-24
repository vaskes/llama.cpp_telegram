#!/usr/bin/env python3
"""Mutation harness for TECH-DEBT #10 blocks D/E/F.

Rules this harness holds itself to, because a mutation run that cannot
report INVALID is a decoration:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse` (which accepts `await`
    outside async on 3.11);
  * the suite is the judge. A hang is caught-by-hang, not a pass.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOST = sys.argv[1] if len(sys.argv) > 1 else "root@85.136.112.144"
SSH = ["ssh", "-i", "/root/.ssh/mavis_host", "-p", "12007", HOST]

# (id, file, old, new, n_expected)
MUTATIONS = [
    ("D1", "dispatch_polling.py",
     "    await _install_signal_handlers(shutdown_event)\n",
     "    pass\n", 1),
    ("D2", "dispatch_polling.py",
     "    await _prepare_runtime(register_bot_menu=register_bot_menu,\n"
     "                         run_selftest=run_selftest)\n",
     "    pass\n", 1),
    ("D3", "dispatch_polling.py",
     "    _dispatch_tasks.add(task)\n", "    pass\n", 1),
    ("D4", "dispatch_polling.py",
     "    global _dispatcher\n    _dispatcher = app\n",
     "    pass\n", 1),
    ("E1", "dispatch_webhook.py",
     "        start_reanchor_scheduler()\n", "        pass\n", 1),
    ("E2", "dispatch_webhook.py",
     "        start_summarise_on_boot()\n", "        pass\n", 1),
    ("E3", "dispatch_webhook.py",
     "        start_reanchor_scheduler()\n        start_summarise_on_boot()\n",
     "        pass\n", 1),
    ("F4", "dispatch.py",
     "    await app.process_update(upd)\n", "    pass\n", 1),
]

# F1, F2 and F3 were removed on 2026-10-24, with the edit-replace
# feature they guarded: F1 and F2 mutated `dispatch_edit.py`, and F3
# mutated the call to `_apply_edited_message` in `dispatch.py`. The
# files and the call are gone, so the mutations have nothing to bite
# on -- a mutation list that names deleted code is a list of REFUSED
# rows, and a harness that reports REFUSED as if it were coverage is
# lying about itself.
#
# The ids are not renumbered: F4 stays F4, so a report from before the
# removal and one from after can still be compared row by row.


def compile_check(path: Path) -> str | None:
    src = path.read_text(encoding="utf-8")
    try:
        compile(src, str(path), "exec")
    except SyntaxError as e:
        return f"{e.msg} at line {e.lineno}"
    return None


def run_suite(workdir: str) -> dict:
    """Run the suite. The verdict is INVALID, never CAUGHT, when the run
    did not actually happen.

    The first version of this harness reported CAUGHT for `cd: No such
    file or directory` -- it pushed a mutated file into a directory it
    never created, the suite never started, and 10 of 11 mutations came
    back caught. Every one of those was a lie. A non-zero exit is only
    evidence if the suite ran; the marker lines are what prove it did.
    """
    cmd = SSH + [
        f"cd {workdir} && docker run --rm "
        f"-v {workdir}:/w -w /w -e CONVERSATIONS_DB=/tmp/mut.db "
        f"-e ALLOWED_USER_IDS=1 bot-testimg311 "
        f"python -m pytest tests/ -q -p no:cacheprovider --tb=no "
        f"--ignore=tests/test_shell_integration.py",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        return {"verdict": "CAUGHT-BY-HANG", "tail": "timeout"}
    out = r.stdout + r.stderr
    # The run marker must be pytest's OWN summary line -- a digit followed
    # by "passed"/"failed". Matching on the substring "error" was wrong:
    # pytest's own `ERROR: file or directory not found: tests/` contains
    # it, so a collection failure was read as "the suite ran and failed",
    # and the mutation was reported caught having tested nothing.
    m = re.search(r"=+ .*?(\d+) (passed|failed|error).*? =+", out)
    if not m:
        return {"verdict": "INVALID",
                "detail": f"нет строки сводки pytest (rc={r.returncode}): "
                          f"{out[-300:].strip()}"}
    summary = m.group(0).strip()
    return {"verdict": "CAUGHT" if r.returncode != 0 else "SURVIVED",
            "detail": summary}


def push(workdir: str, files: dict):
    """Lay down a FULL copy of the repo, then apply the mutation on top.

    Copying just the mutated file was the second version of this mistake:
    pytest found no `tests/` at all and printed
    `ERROR: file or directory not found: tests/`, which the loose marker
    check counted as "the suite ran and failed". Eleven mutations, eleven
    false CAUGHTs, zero tests executed.

    Two independent requirements, both of which were skipped once:
      1. the whole tree has to be present -- the suite reads TECH-DEBT.md
         and docs/, not just the package;
      2. the mutated file has to come back byte-identical.
    """
    tar = subprocess.run(["tar", "-cf", "-", "-C", str(REPO),
                          "--exclude=.git", "--exclude=__pycache__",
                          "--exclude=.pytest_cache", "."],
                         capture_output=True, timeout=180)
    subprocess.run(SSH + [f"rm -rf {workdir} && mkdir -p {workdir}"],
                   capture_output=True, timeout=60)
    p = subprocess.run(SSH + [f"cd {workdir} && tar -xf -"],
                       input=tar.stdout, capture_output=True, timeout=300)
    if p.returncode != 0:
        return f"не удалось разложить репо: {p.stderr[-200:].decode(errors='replace')}"
    for rel, content in files.items():
        p = Path(workdir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        r = subprocess.run(["scp", "-i", "/root/.ssh/mavis_host", "-P", "12007",
                            str(p), f"{HOST}:{workdir}/{rel}"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return f"scp не доставил {rel}: {r.stderr[-200:]}"
        back = subprocess.run(SSH + [f"cat {workdir}/{rel}"],
                              capture_output=True, text=True, timeout=60)
        if back.stdout != content:
            return f"{rel} на хосте отличается от мутированного"
    return None


def main():
    results = []
    files = {}
    for mid, rel, old, new, n_exp in MUTATIONS:
        src = REPO / "telegram-bot" / rel
        if rel not in files:
            files[rel] = src.read_text(encoding="utf-8")
        base = files[rel]

        n_sub = base.count(old)
        if n_sub != n_exp:
            results.append((mid, "REFUSED", f"n_sub={n_sub}, ожидалось {n_exp}"))
            continue
        mutated = base.replace(old, new, n_exp)
        if mutated == base:
            results.append((mid, "INVALID", "замена не изменила исходник"))
            continue
        workdir = f"/tmp/mut_{mid}"
        subprocess.run(SSH + [f"rm -rf {workdir}"], capture_output=True)
        why = push(workdir, {"telegram-bot/" + rel: mutated})
        if why:
            results.append((mid, "INVALID", why))
            continue

        err = compile_check(Path(workdir) / "telegram-bot" / rel)
        if err:
            results.append((mid, "INVALID", f"не компилируется: {err}"))
            subprocess.run(SSH + [f"rm -rf {workdir}"], capture_output=True)
            continue

        r = run_suite(workdir)
        subprocess.run(SSH + [f"rm -rf {workdir}"], capture_output=True)
        results.append((mid, r["verdict"], r["detail"]))

    print(f"{'id':5} {'verdict':10} detail")
    caught = 0
    for mid, v, d in results:
        print(f"{mid:5} {v:10} {d[:80]}")
        if v.startswith("CAUGHT"):
            caught += 1
    print(f"\n{caught}/{len(results)} поймано")
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())