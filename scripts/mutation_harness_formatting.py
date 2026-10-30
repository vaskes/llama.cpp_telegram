#!/usr/bin/env python3
"""Mutation harness for the answer formatting (2026-10-25).

The console output the model produced is a fenced code block, and
Telegram renders a code block natively only when the message carries a
parse mode. So the answer goes out as HTML, and the block becomes a
`<pre>`.

There are two ways to get that wrong, and they are not the same kind
of wrong:

  * the block is **not** rendered -- the user reads the backticks, and
    the feature simply does not work;
  * the block is rendered **badly** -- a `<pre>` cut in half by the
    splitter, a code block bigger than one message, a fence the model
    left open. Telegram answers a message it cannot parse with
    "can't parse entities", and the user gets *nothing*. A lost answer
    is worse than an unformatted one.

**Group A -- the behaviour.** Each mutation puts one of those defects
back, and the suite has to notice.

**Group B -- the tripwires.** Each mutation removes a tripwire *and* the
behaviour it is supposed to catch, and the suite has to go **green**. A
green suite with the tripwire gone is the proof that the tripwire was
the thing doing the work: without it, the defect is invisible. This is
the direction a one-way harness never checks, and it is the one that
turns "the suite is green" into "the suite is green *because of this
test*".

The rules this harness holds itself to, because a mutation run that
cannot report INVALID is a decoration:

  * clean-tree preflight; without a PASS on the unmutated tree the word
    CAUGHT is meaningless;
  * `n_sub == n_expected` before anything else -- a mutation that does
    not change the source is REFUSED, never reported as SURVIVED;
  * `compile()` validates, not `ast.parse`;
  * the suite is the judge. A hang is caught-by-hang, not a pass;
  * the name of the test that went red is printed, because "the suite
    went red" and "the test written for this rule went red" are
    different claims;
  * the mutated files are restored whatever happens, and the restore is
    verified byte for byte;
  * a mutation of `handlers.py` keeps the line count: the size of that
    file is declared in four places, and a mutation that changed the
    count would turn the size tripwire red and hide the defect behind
    an unrelated failure.

Where it runs: with the current interpreter by default, so it works in
a checkout. Set MUTATION_HARNESS_IMAGE to run the same tests inside the
test image instead.

    python3 scripts/mutation_harness_formatting.py [ROOT]
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(__file__).resolve().parent.parent).resolve()
IMAGE = os.environ.get("MUTATION_HARNESS_IMAGE", "")

#: The files that carry the rule and the tests that catch it. A mutation
#: that no target can notice is a mutation that proves nothing.
TARGETS = [
    "tests/test_telegram_code_formatting.py",
    # The inline code: the same formatter, one more shape of it.
    "tests/test_telegram_inline_code.py",
    # The bold: the same formatter again, and the case the operator
    # named -- a code span inside a bold span.
    "tests/test_telegram_bold.py",
    # The answer path, in case the split moves the answer somewhere else.
    "tests/test_answer_chunking.py",
    # The structural tripwire: every handler sends its answer through
    # the helper.
    "tests/test_handler_tail.py",
    # The CODEBOOK names this tripwire; the row has to stay true.
    "tests/test_codebook_test_table.py",
]

#: A mutation that hangs the suite is still a catch, but it has to be
#: scored within a bounded time.
SUITE_TIMEOUT = 300

HANDLERS = ROOT / "telegram-bot" / "handlers.py"
FORMAT = ROOT / "telegram-bot" / "telegram_format.py"
TEST_FORMAT = ROOT / "tests" / "test_telegram_code_formatting.py"
TEST_INLINE = ROOT / "tests" / "test_telegram_inline_code.py"
TEST_BOLD = ROOT / "tests" / "test_telegram_bold.py"

# (name, expectation, [(path, pattern, replacement, expected_sites)])
#
# expectation "CAUGHT"  -- the suite has to go red.
# expectation "SURVIVED" -- the suite has to stay green, because the
#                          tripwire that would have caught it is gone
#                          too. That is the load-bearing proof.
MUTATIONS = [
    # --- group A: the behaviour -------------------------------------
    ("the answer goes out without the parse mode", "CAUGHT", [
        (HANDLERS,
         r'await send_reply\(update, chunk, strip=False, parse_mode="HTML"\)',
         'await send_reply(update, chunk, strip=False, parse_mode=None)', 1),
    ]),
    ("the text is not escaped", "CAUGHT", [
        (FORMAT,
         r"(?s)    if not text:\n        return \"\"\n"
         r"    return \(text\.replace\(\"&\", \"&amp;\"\).*?\n"
         r"                \.replace\(\">\", \"&gt;\"\)\)",
         "    return text", 1),
    ]),
    ("the code block is not converted", "CAUGHT", [
        (FORMAT, r"    spans = _fenced_blocks\(text\)\n",
         "    spans = []\n", 1),
    ]),
    ("an unclosed fence is converted too", "CAUGHT", [
        (FORMAT,
         r"(?m)^        if end < 0:\n"
         r"            # No closing fence: the block was cut, or there never was one\.\n"
         r"            return spans\n",
         "        if end < 0:\n            end = len(text)\n", 1),
    ]),
    ("a code block bigger than a message is converted anyway", "CAUGHT", [
        (FORMAT, r"        if len\(body\) \+ _TAG_OVERHEAD > limit:\n",
         "        if False:\n", 1),
    ]),
    ("the splitter cuts an element in half", "CAUGHT", [
        (FORMAT, r"(?m)^            if opened > closed:\n",
         "            if False:\n", 1),
    ]),
    ("the language hint stays in the code", "CAUGHT", [
        (FORMAT, r"(?m)^        if newline >= 0:\n",
         "        if False:\n", 1),
    ]),
    ("the inline code is not converted", "CAUGHT", [
        (FORMAT,
         r'    return "<code>" \+ body \+ "</code>"',
         '    return escape_html(raw)', 1),
    ]),
    ("a run of two backticks is a delimiter", "CAUGHT", [
        (FORMAT, r"(?m)^        if run_end - start != 1:\n",
         "        if run_end - start > 2:\n", 1),
        (FORMAT, r"(?m)^        if close_end - close != 1:\n",
         "        if close_end - close > 2:\n", 1),
    ]),
    ("a code span may run over a line break", "CAUGHT", [
        (FORMAT, r'(?m)^        if "\\n" in text\[run_end:close\]:\n',
         "        if False:\n", 1),
    ]),
    ("the code inside a fenced block is converted again", "CAUGHT", [
        (FORMAT,
         r'out\.append\("<pre>" \+ escape_html\(body\) \+ "</pre>"\)',
         'out.append("<pre>" + _render_prose(body, limit) + "</pre>")', 1),
    ]),
    ("the splitter does not protect the inline code", "CAUGHT", [
        (FORMAT,
         r'_ELEMENTS = \(\("<pre>", "</pre>"\), \("<code>", "</code>"\), \("<b>", "</b>"\)\)',
         '_ELEMENTS = (("<pre>", "</pre>"), ("<b>", "</b>"))', 1),
    ]),

    # --- the bold ---------------------------------------------------
    ("the bold is not converted", "CAUGHT", [
        (FORMAT,
         r'    return "<b>" \+ escape_html\(raw\) \+ "</b>"',
         '    return escape_html(raw)', 1),
    ]),
    ("a run of three asterisks is a delimiter", "CAUGHT", [
        (FORMAT, r"(?m)^        if run_end - start != len\(_BOLD_DELIM\):\n",
         "        if run_end - start > 3:\n", 1),
    ]),
    ("an unmatched bold pair is converted", "CAUGHT", [
        (FORMAT,
         r"(?m)^        if close < 0:\n"
         r"            # No closing delimiter: the rest is literal text\.\n"
         r"            return spans\n",
         "        if close < 0:\n"
         "            close = len(text) - len(_BOLD_DELIM)\n", 1),
    ]),
    ("the bold pair may cross an empty line", "CAUGHT", [
        (FORMAT, r'(?m)^        if "\\n\\n" in body:\n',
         "        if False:\n", 1),
    ]),
    ("the bold inside a code span is converted", "CAUGHT", [
        (FORMAT, r"(?m)^        if _inside\(code_spans, start\):\n",
         "        if False:\n", 1),
    ]),
    ("the bold and the code nest", "CAUGHT", [
        (FORMAT, r"(?m)^    return rendered\n",
         '    return "<b>" + rendered + "</b>"\n', 1),
    ]),
    ("the splitter does not protect the bold", "CAUGHT", [
        (FORMAT,
         r'_ELEMENTS = \(\("<pre>", "</pre>"\), \("<code>", "</code>"\), \("<b>", "</b>"\)\)',
         '_ELEMENTS = (("<pre>", "</pre>"), ("<code>", "</code>"))', 1),
    ]),

    # --- group B: the tripwires ------------------------------------
    # The tripwires are silenced with a `return` at the top of the test
    # body, not by renaming the function: a renamed test leaves the
    # file, and the CODEBOOK's table quotes how many tests it has --
    # so a renamed test turns an unrelated tripwire red and hides the
    # defect behind it.
    ("the tripwires are gone, and the code block is not converted",
     "SURVIVED", [
        (TEST_FORMAT,
         r"(?m)^def test_the_console_block_becomes_a_pre\(\):\n",
         "def test_the_console_block_becomes_a_pre():\n    return\n", 1),
        (TEST_FORMAT,
         r"(?m)^def test_the_code_inside_the_block_is_untouched\(\):\n",
         "def test_the_code_inside_the_block_is_untouched():\n    return\n", 1),
        (TEST_FORMAT,
         r"(?m)^def test_the_language_hint_is_not_part_of_the_code\(\):\n",
         "def test_the_language_hint_is_not_part_of_the_code():\n    return\n", 1),
        (TEST_FORMAT,
         r"(?m)^async def test_the_code_block_is_a_pre_in_the_message_the_user_gets\(update\):\n",
         "async def test_the_code_block_is_a_pre_in_the_message_the_user_gets(update):\n    return\n", 1),
        (TEST_FORMAT,
         r"(?m)^def test_the_console_block_survives_the_split_in_one_piece\(\):\n",
         "def test_the_console_block_survives_the_split_in_one_piece():\n    return\n", 1),
        (TEST_FORMAT,
         r"(?m)^def test_the_split_never_cuts_a_pre_in_half\(\):\n",
         "def test_the_split_never_cuts_a_pre_in_half():\n    return\n", 1),
        (TEST_FORMAT,
         r"(?m)^def test_a_cut_inside_a_pre_keeps_the_block_whole\(\):\n",
         "def test_a_cut_inside_a_pre_keeps_the_block_whole():\n    return\n", 1),
        # The inline-code file guards the fenced block too: it asserts
        # that the block IS a <pre> before it asserts that the code
        # inside it is not converted again. A group-B mutation has to
        # silence every tripwire that guards the behaviour it removes.
        (TEST_INLINE,
         r"(?m)^def test_the_code_inside_a_fenced_block_is_not_converted_again\(\):\n",
         "def test_the_code_inside_a_fenced_block_is_not_converted_again():\n    return\n", 1),
        # The bold file guards the fenced block too: it asserts that the
        # block IS a <pre> before it asserts that the bold inside it is
        # not converted again.
        (TEST_BOLD,
         r"(?m)^def test_the_bold_inside_a_fenced_block_is_not_converted_again\(\):\n",
         "def test_the_bold_inside_a_fenced_block_is_not_converted_again():\n    return\n", 1),
        (FORMAT, r"    spans = _fenced_blocks\(text\)\n",
         "    spans = []\n", 1),
    ]),
    ("the tripwire is gone, and the answer goes out without the parse mode",
     "SURVIVED", [
        (TEST_FORMAT,
         r"(?m)^async def test_the_answer_goes_out_with_the_parse_mode\(update\):\n",
         "async def test_the_answer_goes_out_with_the_parse_mode(update):\n    return\n", 1),
        # The inline-code file checks the parse mode too: the answer
        # carries the <code> elements *because* it goes out as HTML.
        (TEST_INLINE,
         r"(?m)^async def test_the_answer_goes_out_with_the_code_element\(update\):\n",
         "async def test_the_answer_goes_out_with_the_code_element(update):\n    return\n", 1),
        # The bold file checks the parse mode too: the answer carries the
        # <b> elements *because* it goes out as HTML.
        (TEST_BOLD,
         r"(?m)^async def test_the_answer_goes_out_with_the_bold_element\(update\):\n",
         "async def test_the_answer_goes_out_with_the_bold_element(update):\n    return\n", 1),
        (HANDLERS,
         r'await send_reply\(update, chunk, strip=False, parse_mode="HTML"\)',
         'await send_reply(update, chunk, strip=False, parse_mode=None)', 1),
    ]),
    # The inline code: every tripwire that guards it is silenced, and
    # the conversion is broken. The suite has to go green -- that is
    # the proof that the tripwire was the thing doing the work.
    ("the tripwires are gone, and the inline code is not converted",
     "SURVIVED", [
        (TEST_INLINE,
         r"(?m)^def test_an_inline_code_span_becomes_a_code_element\(\):\n",
         "def test_an_inline_code_span_becomes_a_code_element():\n    return\n", 1),
        (TEST_INLINE,
         r"(?m)^def test_the_operator_own_sentence_keeps_its_prose_and_marks_its_spans\(\):\n",
         "def test_the_operator_own_sentence_keeps_its_prose_and_marks_its_spans():\n    return\n", 1),
        (TEST_INLINE,
         r"(?m)^def test_the_code_inside_the_span_is_escaped\(\):\n",
         "def test_the_code_inside_the_span_is_escaped():\n    return\n", 1),
        (TEST_INLINE,
         r"(?m)^def test_the_split_never_cuts_a_code_in_half\(\):\n",
         "def test_the_split_never_cuts_a_code_in_half():\n    return\n", 1),
        (TEST_INLINE,
         r"(?m)^async def test_the_answer_goes_out_with_the_code_element\(update\):\n",
         "async def test_the_answer_goes_out_with_the_code_element(update):\n    return\n", 1),
        # The bold file holds code spans too: the case the operator named
        # is a code span inside a bold span, and the code has to survive
        # it -- both without the code element and without the nesting.
        (TEST_BOLD,
         r"(?m)^def test_a_code_span_inside_a_bold_span_keeps_both\(\):\n",
         "def test_a_code_span_inside_a_bold_span_keeps_both():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_the_bold_inside_an_inline_code_span_is_not_converted\(\):\n",
         "def test_the_bold_inside_an_inline_code_span_is_not_converted():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_a_delimiter_inside_a_code_span_does_not_open_a_bold_span\(\):\n",
         "def test_a_delimiter_inside_a_code_span_does_not_open_a_bold_span():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_the_bold_and_the_code_do_not_nest\(\):\n",
         "def test_the_bold_and_the_code_do_not_nest():\n    return\n", 1),
        (FORMAT,
         r'    return "<code>" \+ body \+ "</code>"',
         '    return escape_html(raw)', 1),
    ]),
    # The bold: every tripwire that guards it is silenced, and the
    # conversion is broken. The suite has to go green -- that is the
    # proof that the tripwire was the thing doing the work.
    ("the tripwires are gone, and the bold is not converted",
     "SURVIVED", [
        (TEST_BOLD,
         r"(?m)^def test_a_bold_span_becomes_a_bold_element\(\):\n",
         "def test_a_bold_span_becomes_a_bold_element():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_the_operator_own_sentence_keeps_its_prose_and_marks_its_bold\(\):\n",
         "def test_the_operator_own_sentence_keeps_its_prose_and_marks_its_bold():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_the_bold_inside_the_phrase_is_escaped\(\):\n",
         "def test_the_bold_inside_the_phrase_is_escaped():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_a_pair_that_crosses_a_line_break_is_converted\(\):\n",
         "def test_a_pair_that_crosses_a_line_break_is_converted():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_a_code_span_inside_a_bold_span_keeps_both\(\):\n",
         "def test_a_code_span_inside_a_bold_span_keeps_both():\n    return\n", 1),
        # The code-formatting file guards the bold as well: it asserts
        # that the phrase the model marked is bold in the chat.
        (TEST_FORMAT,
         r"(?m)^def test_the_prose_survives_as_it_was_written\(\):\n",
         "def test_the_prose_survives_as_it_was_written():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_a_delimiter_inside_a_code_span_does_not_open_a_bold_span\(\):\n",
         "def test_a_delimiter_inside_a_code_span_does_not_open_a_bold_span():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_the_bold_and_the_code_do_not_nest\(\):\n",
         "def test_the_bold_and_the_code_do_not_nest():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^def test_the_split_never_cuts_a_bold_in_half\(\):\n",
         "def test_the_split_never_cuts_a_bold_in_half():\n    return\n", 1),
        (TEST_BOLD,
         r"(?m)^async def test_the_answer_goes_out_with_the_bold_element\(update\):\n",
         "async def test_the_answer_goes_out_with_the_bold_element(update):\n    return\n", 1),
        (FORMAT,
         r'    return "<b>" \+ escape_html\(raw\) \+ "</b>"',
         '    return escape_html(raw)', 1),
    ]),
]


def _command():
    if IMAGE:
        return ["docker", "run", "--rm", "-v", f"{ROOT}:/w", "-w", "/w",
                "-e", "CONVERSATIONS_DB=/tmp/mut.db", "-e", "ALLOWED_USER_IDS=1",
                IMAGE, "python", "-m", "pytest", *TARGETS, "-q", "-rf",
                "-p", "no:cacheprovider"]
    return [sys.executable, "-m", "pytest", *TARGETS, "-q", "-rf",
            "-p", "no:cacheprovider"]


def run_tests():
    """('pass' | 'fail' | 'hang', summary, who_failed).

    `who_failed` is the list of the test names that went red. A harness
    that reports only "the suite went red" cannot tell a mutation that
    was caught by the test that was written for it from one that was
    caught by an import error three files away -- and those are not the
    same evidence.
    """
    try:
        proc = subprocess.run(_command(), cwd=str(ROOT), capture_output=True,
                              text=True, timeout=SUITE_TIMEOUT)
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
    verdict, summary, _who = run_tests()
    print("  ", summary, flush=True)
    if verdict != "pass":
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
                # op: two renames in one file would leave only the last
                # one in the tree, and the harness would score a
                # mutation it never actually applied.
                text = mutated.get(path, path.read_text(encoding="utf-8"))
                found = list(re.finditer(pattern, text, re.M))
                if len(found) != expect:
                    refused += 1
                    refused_here = True
                    print(f"  REFUSED {name}: expected {expect} site(s) for "
                          f"{pattern[:44]!r}, found {len(found)}. Refusing to "
                          f"score a mutation that did not apply where "
                          f"claimed -- that is how a harness reports a pass "
                          f"it never measured.", flush=True)
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
                    try:
                        compile(src, str(path), "exec")
                    except SyntaxError as e:
                        invalid += 1
                        print(f"  INVALID {name}: {path.name} does not "
                              f"compile -- it caught nothing ({e})", flush=True)
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
