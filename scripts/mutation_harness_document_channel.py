#!/usr/bin/env python3
"""Mutation harness for the document file channel (2026-11-01).

A document that arrives in the chat is a FILE. The bot's file channel
is the sandbox: the bytes go in through the door's upload half, the
model is told where the file is, and the prompt carries a pointer (and
a labelled preview) instead of the file.

What it was before this wave: the bot read the first 16 000 characters
of a text document and pasted them into the prompt. The rest of the
file was gone, nothing said the text in front of the model was a piece,
and the model -- asked to put the file in the workspace -- re-typed the
piece it had and reported the result, with a byte count and a sha256,
as the file the user had sent.

**Group A -- the behaviour.** Each mutation puts one of the defects
back, and the suite has to notice:

  * the document is pasted into the prompt again;
  * the bytes that land are not the bytes that arrived (the file is
    re-typed on the way in);
  * the excerpt is not labelled as a piece (a silent cut);
  * the refusal is swallowed (the file is lost and nobody says so);
  * the binary document stops going to the sandbox (the path that
    already worked breaks).

**Group B -- the tripwires.** Each mutation removes a tripwire *and*
the behaviour it is supposed to catch, and the suite has to go
**green**. A green suite with the tripwire gone is the proof that the
tripwire was the thing doing the work.

The rules this harness holds itself to:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse()`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the name of the test that went red is printed;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte;
  * the tripwires are silenced with a `return` at the top of the test
    body, not by renaming the function: a renamed test leaves the file,
    and the CODEBOOK's table quotes how many tests it has.

    python3 scripts/mutation_harness_document_channel.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
#: The image the nightly job built for the harnesses. Empty
#: means "run on this interpreter" -- the local way.
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The files that carry the rule and the tests that catch it.
TARGETS = ["tests/test_document_file_channel.py"]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

HANDLERS = ROOT / "telegram-bot" / "handlers.py"
TEST_CHANNEL = ROOT / "tests" / "test_document_file_channel.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
#: The def line of every seam test, exactly as the file has it. A group
#: B mutation replaces the line with itself and a `return` under it: the
#: test stays in the file -- the CODEBOOK quotes how many tests each
#: file has, and a renamed test leaves the collection -- and stops
#: testing anything.
SILENCE = {
    "lands": "async def test_a_text_document_lands_in_the_sandbox(monkeypatch, tmp_path):",
    "pointer": ("async def test_the_model_is_given_a_pointer_not_the_file(monkeypatch,\n"
                "                                                          tmp_path):"),
    "piece": "async def test_a_piece_of_a_file_says_it_is_a_piece(monkeypatch, tmp_path):",
    "short": "async def test_a_short_document_arrives_whole(monkeypatch, tmp_path):",
    "refusal": ("async def test_a_file_the_sandbox_will_not_take_is_named_as_lost(\n"
                "        monkeypatch, tmp_path):"),
    "binary": ("async def test_a_binary_document_still_lands_in_the_sandbox(monkeypatch,\n"
               "                                                             tmp_path):"),
}


def _silence(key):
    """The op that turns one tripwire off."""
    line = SILENCE[key]
    return (TEST_CHANNEL, re.escape(line) + "\n",
            line + "\n    return\n", 1)


#: Which tests each defect turns red. MEASURED, not guessed: every
#: mutation below was applied to the tree and the red tests were read
#: off the run (2026-11-01). A group B mutation silences exactly those
#: tests, so "the tripwire is gone" means what it says -- and a
#: tripwire nobody silences would make the group B proof a fiction.
CAUGHT_BY = {
    "paste": ["lands", "pointer", "piece", "short", "refusal", "binary"],
    "bytes": ["lands", "pointer", "binary"],
    "piece": ["piece"],
    "refusal": ["refusal"],
    "upload": ["lands", "pointer", "refusal", "binary"],
    "kind": ["binary"],
}


def _group_b(keys, behaviour_op):
    """The tripwires that were doing the work, off, and the defect back."""
    return [_silence(key) for key in keys] + [behaviour_op]


#: The behaviour mutations -- the defects this wave put back.
BEHAVIOUR = {
    "paste": (HANDLERS,
              r"            user_content = await _document_to_sandbox\(\n"
              r"                update, doc, doc_bytes, caption,\n"
              r"                is_text=_reads_as_text\(doc_bytes\)\)\n",
              "            user_content = (f\"{caption}\\n\\n--- Document ---\\n\"\n"
              "                            + doc_bytes.decode(\"utf-8\",\n"
              "                                          errors=\"ignore\")[:16000])\n",
              1),
    "bytes": (HANDLERS,
              r"        uploaded = await _fu\.upload_to_sandbox\(doc_bytes, name\)\n",
              "        uploaded = await _fu.upload_to_sandbox(\n"
              "            doc_bytes.decode(\"utf-8\", errors=\"ignore\")[:16000]\n"
              "            .encode(\"utf-8\"), name)\n",
              1),
    "piece": (HANDLERS,
              r"        f\"Only the first \{len\(preview\)\} characters of it are quoted below\. \"\n"
              r"        f\"The file in the sandbox is the complete one: read it there \"\n"
              r"        f\"before you say anything about its contents\. The excerpt is not \"\n"
              r"        f\"the file\.\\n\\n\"\n",
              "",
              1),
    "refusal": (HANDLERS,
                r"        f\"The file `\{name\}` \(\{size\} bytes\) could not be put in the \"\n"
                r"        f\"sandbox: \{reason\.rstrip\('\.'\)\}\. Tell the user that plainly\.\\n\\n\"\n",
                "        f\"The file `{name}` ({size} bytes).\\n\\n\"\n",
                1),
    "upload": (HANDLERS,
               r"        uploaded = await _fu\.upload_to_sandbox\(doc_bytes, name\)\n",
               "        uploaded = _fu.UploadedFile(\n"
               "            name=name, path=f\"/workspace/{name}\", size=size,\n"
               "            sha256=\"0\" * 64)\n",
               1),
    "kind": (HANDLERS,
             r"                is_text=_reads_as_text\(doc_bytes\)\)\n",
             "                is_text=True)\n",
             1),
}

#: The mutations themselves: (name, expectation, ops).
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the document is pasted into the prompt again", "CAUGHT",
     [BEHAVIOUR["paste"]]),
    ("the bytes that land are not the bytes that arrived", "CAUGHT",
     [BEHAVIOUR["bytes"]]),
    ("the excerpt is not labelled as a piece", "CAUGHT",
     [BEHAVIOUR["piece"]]),
    ("the refusal is swallowed", "CAUGHT",
     [BEHAVIOUR["refusal"]]),
    ("the document never reaches the sandbox", "CAUGHT",
     [BEHAVIOUR["upload"]]),
    ("a binary document is called a text file", "CAUGHT",
     [BEHAVIOUR["kind"]]),

    # --- group B: the tripwires -------------------------------------
    ("the tripwires are gone, and the document is pasted again",
     "SURVIVED", _group_b(CAUGHT_BY["paste"], BEHAVIOUR["paste"])),
    ("the tripwires are gone, and the file is re-typed on the way in",
     "SURVIVED", _group_b(CAUGHT_BY["bytes"], BEHAVIOUR["bytes"])),
    ("the tripwire is gone, and the excerpt is not labelled",
     "SURVIVED", _group_b(CAUGHT_BY["piece"], BEHAVIOUR["piece"])),
    ("the tripwire is gone, and the refusal is swallowed",
     "SURVIVED", _group_b(CAUGHT_BY["refusal"], BEHAVIOUR["refusal"])),
    ("the tripwires are gone, and the document never reaches the sandbox",
     "SURVIVED", _group_b(CAUGHT_BY["upload"], BEHAVIOUR["upload"])),
    ("the tripwire is gone, and a binary is called a text file",
     "SURVIVED", _group_b(CAUGHT_BY["kind"], BEHAVIOUR["kind"])),
]

def _command():
    """The suite runs in the image, when the nightly job gave us one.

    The nightly job builds the bot's own image and hands its name in
    `MUTATION_HARNESS_IMAGE`, because the harnesses have to run in the
    environment the code ships into -- the image the bot runs in, with
    its dependencies, not the runner's own Python.

    A harness that runs on the runner's Python instead measures
    something else: the runner has no pytest, so the suite does not
    even start, the preflight reads "(no summary)", and the job goes
    red on a night this harness was supposed to measure. That is what
    happened to this harness on 2026-11-07 (mutation-nightly, the
    document channel's night) -- and it would happen to every night
    this harness is picked.
    """
    if IMAGE:
        return ["docker", "run", "--rm", "-v", f"{ROOT}:/w", "-w", "/w",
                "-e", "CONVERSATIONS_DB=/tmp/mut.db",
                "-e", "ALLOWED_USER_IDS=1",
                IMAGE, "python", "-m", "pytest", *TARGETS, "-q", "-rf",
                "-p", "no:cacheprovider"]
    return [sys.executable, "-m", "pytest", *TARGETS, "-q", "-rf",
            "-p", "no:cacheprovider"]


def run_tests():
    """('pass' | 'fail' | 'hang', summary, who_failed).

    `who_failed` is the list of the test names that went red. A harness
    that reports only "the suite went red" cannot tell a mutation that
    was caught by the test that was written for it from one that was
    caught by an import error three files away.
    """
    import os
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(_command(), cwd=str(ROOT), capture_output=True,
                              text=True, timeout=SUITE_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return "hang", "the suite did not finish", []
    out = proc.stdout + proc.stderr
    tail = [line for line in out.splitlines()
            if " passed" in line or " failed" in line or "error" in line.lower()]
    who = [line.split("::")[-1].split(" ")[0]
           for line in out.splitlines() if line.startswith("FAILED ")]
    return ("fail" if proc.returncode != 0 else "pass"), \
           (tail[-1].strip() if tail else "(no summary)"), who


def _restore(path: Path, before: bytes):
    path.write_bytes(before)
    if path.read_bytes() != before:
        raise SystemExit(f"RESTORE FAILED for {path} -- the tree is dirty")


def main():
    print(f"root: {ROOT}")
    print(f"runner: {'docker ' + IMAGE if IMAGE else sys.executable}")
    print("=== preflight on the CLEAN tree ===", flush=True)
    verdict, summary, who = run_tests()
    print("  ", summary, flush=True)
    if verdict != "pass":
        # The names, not just the count. A preflight that says "2 failed"
        # and nothing else sends the reader to the tree; one that says
        # WHICH two sent them to the test. Measured on 2026-11-12: the
        # mutation-nightly job went red with "2 failed, 73 passed" and
        # nothing to look at -- the log had no way to name the two.
        for name in who:
            print(f"    RED: {name}", flush=True)
        print("PREFLIGHT FAILED -- the harness would be measuring itself")
        return 2

    caught = survivors = refused = invalid = 0
    wrong = []
    for name, expectation, ops in MUTATIONS:
        before = {}
        try:
            mutated = {}
            total = 0
            refused_here = False
            for path, pattern, replacement, expect in ops:
                # The source of an op is the CURRENT state of the file,
                # including the ops that already ran on it. Re-reading
                # the file from disk each time would undo the previous
                # op, and the harness would score a mutation it never
                # actually applied.
                text = mutated.get(path, path.read_text(encoding="utf-8"))
                found = list(re.finditer(pattern, text, re.M))
                if len(found) != expect:
                    refused += 1
                    refused_here = True
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed.", flush=True)
                    break
                total += len(found)
                mutated[path] = re.sub(pattern, replacement.replace("\\", "\\\\"),
                                       text, flags=re.M)
            if refused_here:
                continue

            for path, src in mutated.items():
                if src == path.read_text(encoding="utf-8"):
                    invalid += 1
                    print(f"  INVALID {name}: the substitution produced "
                          f"identical text in {path.name}", flush=True)
                    break
                before[path] = path.read_bytes()
                path.write_text(src, encoding="utf-8")
            else:
                for path, src in mutated.items():
                    if path.suffix == ".py":
                        try:
                            compile(src, str(path), "exec")
                        except SyntaxError as e:
                            invalid += 1
                            print(f"  INVALID {name}: {path.name} does not "
                                  f"compile -- it caught nothing ({e})",
                                  flush=True)
                            break
                else:
                    verdict, summary, who = run_tests()
                    red = verdict == "fail"
                    hung = verdict == "hang"
                    by = ("  by: " + ", ".join(who[:3])) if who else ""
                    if expectation == "CAUGHT":
                        if red or hung:
                            caught += 1
                            word = "CAUGHT" if red else "CAUGHT-BY-HANG"
                            print(f"  {word} {name}  [{total} site(s)]  "
                                  f"{summary}{by}", flush=True)
                        else:
                            wrong.append(name)
                            survivors += 1
                            print(f"  SURVIVED {name}  [{total} site(s)]  "
                                  f"{summary}{by}  <-- expected CAUGHT",
                                  flush=True)
                    else:  # SURVIVED
                        if not red and not hung:
                            survivors += 1
                            print(f"  SURVIVED {name}  [{total} site(s)]  "
                                  f"{summary}  (the tripwire was the thing "
                                  f"catching it)", flush=True)
                        else:
                            wrong.append(name)
                            print(f"  CAUGHT {name}  [{total} site(s)]  "
                                  f"{summary}{by}  <-- expected SURVIVED: "
                                  f"something else is also guarding this",
                                  flush=True)
        finally:
            for path, blob in before.items():
                _restore(path, blob)

    print(f"\n{caught} caught, {survivors} survived, {refused} refused, "
          f"{invalid} invalid  ({len(MUTATIONS)} mutations)", flush=True)
    if wrong:
        print("WRONG VERDICT:", wrong, flush=True)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--short"],
                           capture_output=True, text=True)
    print("tree after the run:", dirty.stdout.strip() or "clean")
    return 1 if (wrong or refused or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
