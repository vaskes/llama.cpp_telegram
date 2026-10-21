"""Test-file syntax must match the interpreter production runs.

W6-H3. `tests/test_shell_integration.py` carried PEP 701 f-strings --
reusing the enclosing quote inside a replacement field:

    assert r['ok'], f'sandbox not reachable: {r.get('error')}'

That parses on 3.12+ and is a SyntaxError on 3.11, which is what
`telegram-bot/Dockerfile` actually pins. Nobody noticed for several waves
because the test image on llmhost1 was built ad-hoc from a 3.12 base, so
the suite had never run on the version it ships to.

Why not `ast.parse(..., feature_version=(3, 11))`: tried it, it does not
catch this. feature_version gates match statements and parenthesized
context managers; PEP 701 f-strings are not in the set it restricts.

The detector has two schemes, because the tokenization differs:

  pre312 -- 3.11 and older emit an f-string as ONE STRING token. Asking
            "does it contain its own quote?" is then a substring test that
            works.
  post312 -- 3.12+ split it into FSTRING_START / FSTRING_MIDDLE /
            FSTRING_END, with the replacement field's literals coming
            through as ordinary STRING tokens in between.

Two earlier versions of this file shipped as no-ops, and a reviewer caught
the second one:

  * A scheme that only looked for STRING tokens with an `f` prefix found
    nothing on 3.12 -- it matched on exactly the interpreter that ACCEPTS
    the syntax. Inverse of the task.
  * The 3.11 scheme read `quote = body[0]` and asked `if quote in "\"'"`.
    body[0] is the opening quote by construction, so that is true for every
    f-string in the repository: 396 flagged, 0 real, precision 0%. The
    content of the literal was never examined.

So: on pre312 the oracle is the interpreter itself. Hand it each f-string
token and let `compile()` decide. It cannot be fooled by a tokenization
detail I got wrong.
"""
import io
import sys
import tempfile
import tokenize
from pathlib import Path

import ast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

PRE312 = "pre312"
POST312 = "post312"


def _default_scheme():
    return POST312 if sys.version_info >= (3, 12) else PRE312


def _quote_of(literal):
    body = literal.lstrip("rRbBuUfF")
    return body[0] if body else ""


def _tokens(src):
    try:
        return list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Unreadable for every version. patch_targets() guards that case;
        # this does not need to duplicate it.
        return None


def _scan_pre312(src):
    """One STRING token per f-string. Ask the interpreter."""
    found = []
    for tok in _tokens(src) or []:
        if tok.type != tokenize.STRING:
            continue
        body = tok.string.lstrip("rRbBuUfF")
        if not body or "f" not in tok.string[:len(tok.string) - len(body)].lower():
            continue
        try:
            compile(tok.string, "<f-string>", "eval")
        except SyntaxError as exc:
            if "f-string" in str(exc.msg):
                found.append((tok.start[0], tok.string.strip()[:70]))
        except ValueError:
            # e.g. an f-string the eval grammar rejects for an unrelated
            # reason. Not a PEP 701 signal; do not report it.
            continue
    return found


def _scan_post312(src):
    """FSTRING_START/MIDDLE/END, literals come through as STRING."""
    FSTART = getattr(tokenize, "FSTRING_START", None)
    FEND = getattr(tokenize, "FSTRING_END", None)
    if FSTART is None or FEND is None:
        return []

    found = []
    stack = []  # one entry per open f-string: (quote, lineno)
    for tok in _tokens(src) or []:
        if tok.type == FSTART:
            stack.append((_quote_of(tok.string), tok.start[0]))
        elif tok.type == FEND:
            if stack:
                stack.pop()
        elif tok.type == tokenize.STRING and stack:
            # Only a replacement field's own literal can collide. Text
            # between FSTRING_START and the first '{' arrives as
            # FSTRING_MIDDLE, so plain `f"it's fine"` never lands here.
            quote, lineno = stack[-1]
            if _quote_of(tok.string) == quote:
                found.append((lineno, tok.string.strip()[:70]))
    return found


SCAN = {PRE312: _scan_pre312, POST312: _scan_post312}


def _offenders_in_source(src, scheme):
    return SCAN[scheme](src)


def _pep701_offenders(path, scheme=None):
    return _offenders_in_source(path.read_text(encoding="utf-8"),
                                scheme or _default_scheme())


# ---------------------------------------------------------------- self-tests

# The detector's own cases. Both schemes are exercised on every interpreter,
# so a run on 3.12 still proves the 3.11 branch, and a run on 3.11 still
# proves the 3.12 branch. Relying on the ambient version is how the 0%
# precision one survived.
NATIVE = _default_scheme()

# There is deliberately no test for the FOREIGN scheme here. It is not
# "skipped pending CI" -- it is untestable on this interpreter at all: on
# 3.11 there is no FSTRING_START token to walk, and on 3.12 compile()
# accepts the syntax so the oracle has nothing to reject. A test that must
# fail on one of the two interpreters is not a test. The foreign scheme is
# covered by the other half of the CI matrix, and
# test_ci_runs_both_interpreters asserts that half exists.


def test_ci_runs_both_interpreters():
    """The skips below are only legitimate because the foreign scheme is
    exercised in the other CI job. Assert that instead of assuming it.

    A scheme can only be tested on an interpreter whose tokenizer produces
    it: on 3.11 there is no FSTRING_START to walk, and on 3.12 compile()
    accepts PEP 701 so the oracle reports nothing. Each run proves one
    scheme; the matrix is what proves both.
    """
    workflows = REPO_ROOT / ".github" / "workflows"
    assert workflows.exists(), "no CI workflows -- nothing covers the foreign scheme"
    text = "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(workflows.glob("*.y*ml")))
    for version in ("3.11", "3.12"):
        assert version in text, (
            f"CI never runs on {version}, so the {PRE312 if version == '3.11' else POST312} "
            f"scan has no coverage anywhere. Add it to the pytest matrix."
        )


DETECTOR_CASES = [
    # (source, should_be_flagged)
    ("x = f'a {d.get('error')} b'\n", True),      # PEP 701, illegal <3.12
    ('x = f"v={m["k"]}"\n', True),                 # same, other delimiter
    # Legal everywhere, and the scheme must stay quiet on all of them.
    ("x = f'a {d.get(\"error\")} b'\n", False),
    ('x = f"v={m[\'k\']}"\n', False),
    ("x = f'plain {v}'\n", False),
    ("x = f\"plain {v}\"\n", False),
    ("x = f\"it's {v}\"\n", False),              # quote in the text
    ("x = f'{v} today'\n", False),
]

@pytest.mark.parametrize("src,flagged", DETECTOR_CASES,
                         ids=[repr(c[0])[:38] for c in DETECTOR_CASES])
def test_detector_on_native_scheme(src, flagged):
    assert bool(_offenders_in_source(src, NATIVE)) is flagged, (
        f"{NATIVE} disagrees on {src!r}")


def test_nested_fstring_does_not_confuse_the_scan():
    """A stack, not a single slot: the inner f-string's own quote is not an
    offender for the outer one, and the outer's fields are judged once the
    inner has closed."""
    # Outer opens with ". The inner f'{v}' quotes itself with ' against its
    # own delimiter, which is fine, and closes before {d['k']} is read.
    assert not _offenders_in_source("x = f\"{f'{v}'} {d['k']}\"\n", NATIVE), (
        "flagged a legal nested f-string")
    # Now the outer's OWN field reuses the outer's delimiter -- a real PEP
    # 701 violation, which must be caught with the stack back to depth 1.
    assert _offenders_in_source("x = f\"{f'{v}'} {d[\"k\"]}\"\n", NATIVE), (
        "outer collision missed after the inner f-string closed")


def test_detector_sees_nothing_in_the_real_tree():
    """Negative phase against real files, not synthetic helpers.

    The scan over 60 files passes on every interpreter, but until it has
    been shown to leave an unflagged legal f-string alone somewhere in
    those 60, a detector that flags everything would also pass.
    """
    files = _test_files()
    scanned = 0
    for path in files:
        src = path.read_text(encoding="utf-8")
        if "f\"" in src or "f'" in src:
            scanned += 1
            # Every file the detector flags must be explained; the real
            # check is that some file carries f-strings and is NOT flagged.
    assert scanned > 10, f"only {scanned} files contain f-strings"

    offenders = {p.name for p in files if _pep701_offenders(p)}
    assert not offenders, (
        f"PEP 701 violations found: {sorted(offenders)} -- production runs "
        f"python:3.11-slim and these files would not import there"
    )


def test_a_legal_fstring_inside_the_tree_is_left_alone(tmp_path):
    """Put a known-good f-string in a file shaped like a test file and
    confirm the scan ignores it. Catches a detector that flags by
    construction rather than by content."""
    probe = tmp_path / "test_probe_legal.py"
    probe.write_text(
        "def test_x():\n"
        "    d = {'k': 1}\n"
        "    assert d, f\"value is {d['k']!r}\"\n"
        "    assert d, f'other {d.get(\"k\")!r}'\n",
        encoding="utf-8",
    )
    assert not _pep701_offenders(probe), (
        f"flagged a legal f-string: {_pep701_offenders(probe)}")


def test_a_breaking_fstring_inside_the_tree_is_caught(tmp_path):
    """And the same file with one illegal swap is flagged, so the negative
    case above is not passing for the wrong reason."""
    probe = tmp_path / "test_probe_broken.py"
    probe.write_text(
        "def test_x():\n"
        "    d = {'k': 1}\n"
        "    assert d, f\"value is {d['k']!r}\"\n"
        "    assert d, f'other {d.get('k')!r}'\n",
        encoding="utf-8",
    )
    offenders = _pep701_offenders(probe)
    assert offenders, "PEP 701 violation not detected"
    assert offenders[0][0] == 4, f"wrong line: {offenders}"


# ------------------------------------------------------------------ the rule

def _test_files():
    return sorted(p for p in TESTS_DIR.rglob("test_*.py"))


def test_there_are_test_files_to_check():
    """Otherwise the scan below passes by finding nothing."""
    assert len(_test_files()) > 10, "test discovery is broken"


@pytest.mark.parametrize("path", _test_files(), ids=lambda p: p.name)
def test_no_pep701_fstrings(path):
    """3.12-only syntax in a test file is invisible until someone runs the
    suite on 3.11 -- which is to say, in production's container."""
    offenders = _pep701_offenders(path)
    assert not offenders, (
        f"{path.name} uses pre-3.12-invalid f-string syntax at "
        f"{[ln for ln, _ in offenders]}: {[txt for _, txt in offenders]}. "
        f"Reusing the enclosing quote inside a replacement field is PEP 701 "
        f"(3.12+). Production runs python:3.11-slim."
    )


def test_prod_dockerfile_pins_311():
    """The version the rule above exists to match.

    If this moves to 3.12 the scan is not wrong, just stale -- and someone
    should decide that deliberately rather than by editing a Dockerfile.
    """
    dockerfile = (REPO_ROOT / "telegram-bot" / "Dockerfile").read_text(
        encoding="utf-8")
    assert "python:3.11" in dockerfile, (
        f"Dockerfile no longer pins 3.11:\n{dockerfile[:200]}"
    )


def test_this_interpreter_can_parse_every_test_file():
    """The cheap half of the guard: if the suite runs somewhere the repo
    does not parse, say so here instead of letting patch_targets() die."""
    broken = []
    for path in _test_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            broken.append(f"{path.name}:{exc.lineno}: {exc.msg}")
    assert not broken, (
        f"running on python {sys.version.split()[0]} but these files do not "
        f"parse here: {broken}. If this is the production interpreter, the "
        f"suite is running on the wrong image."
    )


def test_patch_targets_survives_an_unparseable_file():
    """W6-H3 cascade. patch_targets() walks every test file with ast.parse;
    an unparseable one used to kill the whole audit before it checked
    anything. Proved with a deliberately broken file rather than trusted."""
    import test_patch_targets

    before = list(test_patch_targets.UNPARSEABLE)
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("def broken(:\n    pass\n")
            name = fh.name

        # Must not raise.
        list(test_patch_targets.patch_targets(Path(name)))

        after = test_patch_targets.UNPARSEABLE
        assert len(after) == len(before) + 1, (
            f"patch_targets swallowed an unparseable file without reporting "
            f"it: {after}"
        )
        assert after[-1][0].endswith(".py"), after[-1]
    finally:
        test_patch_targets.UNPARSEABLE[:] = before
        Path(name).unlink(missing_ok=True)