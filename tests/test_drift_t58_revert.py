"""T60 Tripwire #1: dispatch.py dead Name reference.

Wave-trap class: acbd062 (T58 revert). The S40 finding:
dispatch.start() referenced `is_dev_mode_chat` after the
function definition was deleted, causing NameError on every
/start.

This tripwire walks EVERY function in dispatch.py (not just
start) and asserts that every Name() reference in the
function body is reachable: module-level def, imported
name, function parameter, local variable, or known
builtin.

If a future revert / refactor leaves a dead reference,
this test fails. Pre-commit hook runs it.
"""
from __future__ import annotations

import ast
import pytest

from test_drift_helpers import (
    DISPATCH_PY,
    KNOWN_BUILTINS,
    all_top_level_functions,
    function_locals,
    imported_names,
    module_level_names,
    parse_source,
)


# Names that are commonly used as `from X import _private`
# aliases and may not appear in module_level_names but are
# still legitimate. Add to this set only with explicit
# justification; if a name is "missing", the answer is
# usually to add the import, not to widen the set.
_ACCEPTABLE_LOCAL_ALIASES = frozenset({
    # _bc is `from personal_tools import bash_command as _bc`
    # used in call_bash_command path; matches in dispatch.py
    # come from comment references in chat_type blocks.
    # Keep this set empty by default; widen only when a
    # real test fails for a legitimate reason.
})


def test_no_undefined_names_in_any_dispatch_function():
    """T60 #1: walk every function in dispatch.py, assert no
    Name reference is undefined.
    """
    tree = parse_source(DISPATCH_PY)
    module_names = module_level_names(tree)
    imported = imported_names(tree)
    acceptable = module_names | imported | KNOWN_BUILTINS | _ACCEPTABLE_LOCAL_ALIASES

    failures: list[tuple[str, str]] = []
    for func in all_top_level_functions(tree):
        locals_ = function_locals(func)
        ref_names = {
            n.id
            for n in ast.walk(func)
            if isinstance(n, ast.Name)
        }
        unknown = ref_names - acceptable - locals_
        if unknown:
            # Pretty-print the failure as (function, name list)
            failures.append((func.name, sorted(unknown)))

    assert not failures, (
        "dispatch.py has functions referencing names that are "
        "not defined at module level, not imported, not "
        "function parameters / local variables, and not "
        "builtins. This is the wave-6 #S40 regression class "
        "(T58 revert left dead call sites in start()). "
        f"Failures: {failures}"
    )


def test_no_dev_line_reference_in_dispatch():
    """T60 #1 drift: dev mode is invisible in the user-facing
    chat by operator design (2026-10-14 11:19 MSK). The
    `dev_line` variable was deleted in acbd062. This test
    pins its absence so a future revert doesn't silently
    re-introduce it.
    """
    src = DISPATCH_PY.read_text()
    assert "dev_line" not in src, (
        "dispatch.py references 'dev_line'. Dev mode is "
        "invisible in the user-facing chat by operator "
        "design (2026-10-14 11:19 MSK). Restore only with "
        "explicit operator approval."
    )


def test_no_dev_mode_badge_in_dispatch():
    """T60 #1 drift: the loud red DEV MODE badge function
    was deleted in acbd062. This pins its absence so a
    future revert doesn't silently re-introduce the badge
    prefix on bot responses.
    """
    src = DISPATCH_PY.read_text()
    assert "dev_mode_badge" not in src, (
        "dispatch.py references 'dev_mode_badge'. The loud "
        "red DEV MODE badge was deleted in acbd062 (T58 "
        "revert) per operator feedback 2026-10-14 11:19 MSK. "
        "Restore only with explicit operator approval."
    )


def test_no_loud_dev_in_dispatch_comments():
    """T60 #1 drift: comments in dispatch.py must not claim
    that the user-facing chat shows a DEV MODE indicator.
    The LLM's prompts.py and the .env.example were fixed in
    T59; this pins the absence in dispatch.py too.
    """
    src = DISPATCH_PY.read_text()
    bad_phrases = [
        "loud red DEV MODE",
        "loud DEV MODE badge",
        "Commands run UNRESTRICTED",
        "Type with care",
    ]
    for phrase in bad_phrases:
        assert phrase not in src, (
            f"dispatch.py contains the phrase '{phrase}'. "
            f"Dev mode is invisible in the user-facing chat "
            f"by operator design (2026-10-14 11:19 MSK). "
            f"Update the comment to reflect that."
        )
