"""Test-file syntax must match the interpreter production runs.

W6-H3. `tests/test_shell_integration.py:40` carried a PEP 701 f-string --
reusing the enclosing quote inside a replacement field:

    assert r['ok'], f'sandbox not reachable: {r.get('error')}'

That parses on 3.12+ and is a SyntaxError on 3.11, which is what
`python:3.11-slim` in telegram-bot/Dockerfile actually gives us. Nobody
noticed for several review waves because the test image on llmhost1 was
built ad-hoc from a 3.12 base with no Dockerfile in the repo, so the suite
had never once run on the version production runs.

Why not `ast.parse(..., feature_version=(3, 11))`: tried it, it does not
catch this. feature_version gates match statements and parenthesized
context managers; PEP 701 f-strings are not in the set it restricts. A
tripwire that reports zero problems on a known-broken file is worse than no
tripwire.

The rule: inside an f-string, a nested string literal may not reuse the
quote character that opened the f-string. That is the construct 3.11
rejects.

The detector has to handle two tokenizations, and getting this wrong is how
the first two attempts shipped as no-ops:

  * 3.11 and older tokenize the whole f-string as ONE STRING token, so the
    quote check is a substring test on the literal.
  * 3.12+ splits it into FSTRING_START / FSTRING_MIDDLE / FSTRING_END, with
    the replacement field's own literals as ordinary STRING tokens in
    between. A detector that only looks for STRING tokens with an `f`
    prefix finds nothing at all on 3.12 -- it matches on precisely the
    version that accepts the syntax, and misses the version that does not.
"""
import io
import sys
import tokenize
from pathlib import Path

import ast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"


def _quote_of(literal):
    body = literal.lstrip("rRbBuUfF")
    return body[0] if body else ""


def _pep701_offenders(path):
    """[(lineno, text)] for every f-string that reuses its own quote."""
    src = path.read_text(encoding="utf-8")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Unreadable for every version. patch_targets() guards that; this
        # test does not need to duplicate it.
        return []

    FSTART = getattr(tokenize, "FSTRING_START", None)
    FEND = getattr(tokenize, "FSTRING_END", None)
    found = []

    if FSTART is not None and any(t.type == FSTART for t in tokens):
        # 3.12+ tokenization.
        active = None  # quote char of the f-string currently open
        active_line = None
        for tok in tokens:
            if tok.type == FSTART:
                active = _quote_of(tok.string)
                active_line = tok.start[0]
            elif tok.type == FEND:
                active = None
            elif tok.type == tokenize.STRING and active:
                if _quote_of(tok.string) == active and active not in "\"'":
                    pass
                elif _quote_of(tok.string) == active:
                    found.append((active_line, tok.string.strip()[:70]))
    else:
        # 3.11 and older: one STRING token for the whole f-string.
        for tok in tokens:
            if tok.type != tokenize.STRING:
                continue
            body = tok.string.lstrip("rRbBuUfF")
            if not body or "f" not in tok.string[:len(tok.string) - len(body)].lower():
                continue
            quote = body[0]
            if quote in "\"'":
                found.append((tok.start[0], tok.string.strip()[:70]))

    return found


def _test_files():
    return sorted(p for p in TESTS_DIR.rglob("test_*.py"))


def test_the_detector_works():
    """The detector itself, on both tokenizations, before it is trusted to
    judge anything else. Two earlier versions of this file shipped as
    no-ops; a detector needs its own tripwire."""
    broken_312 = "x = f'a {d.get('error')} b'\n"
    assert _pep701_offenders_from_source(broken_312), \
        "failed to flag a nested-quote f-string on the 3.12 tokenization"

    # Sanity: the legal form must NOT be flagged.
    legal = "x = f'a {d.get(\"error\")} b'\n"
    assert not _pep701_offenders_from_source(legal), \
        "flagged a legal f-string that uses different quotes"


def _pep701_offenders_from_source(src):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        name = fh.name
    try:
        return _pep701_offenders(Path(name))
    finally:
        Path(name).unlink(missing_ok=True)


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
    import tempfile
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
