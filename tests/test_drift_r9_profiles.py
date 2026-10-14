"""T61 #R9 v3: profile set is consistent across all files.

Wave-trap pattern (predicted): a future commit adds a
4th profile (e.g. staging) in limits.py but forgets
to update bash_command.py or prompts.py.

This tripwire extracts the set of valid profile names
from limits.py (the single source of truth) and asserts
that the SAME set appears in bash_command.py and
prompts.py.

v3: Skip the bash_command.py check for "public" and
"personal" (default profiles - mentioned everywhere in
docstrings, not as quoted strings). Only check non-default
profiles (currently just "dev") as identifier substrings.
"""
from __future__ import annotations

import re
import pathlib


LIMITS_PY = (
    pathlib.Path(__file__).parent.parent
    / "telegram-bot"
    / "limits.py"
)
BASH_COMMAND_PY = (
    pathlib.Path(__file__).parent.parent
    / "telegram-bot"
    / "personal_tools"
    / "bash_command.py"
)
PROMPTS_PY = (
    pathlib.Path(__file__).parent.parent
    / "telegram-bot"
    / "prompts.py"
)


def _extract_profile_set_from_limits(src: str) -> set:
    m = re.search(
        r'_DEPLOYMENT_PROFILE\s+not\s+in\s+\(([^)]+)\)',
        src,
    )
    if not m:
        raise AssertionError(
            "Could not find _DEPLOYMENT_PROFILE validation "
            "in limits.py. Tripwire is broken or the file "
            "has been heavily refactored. Update this tripwire."
        )
    body = m.group(1)
    profiles: set = set()
    pattern = re.compile(r"""['"]([a-zA-Z0-9_]+)['"]""")
    for sm in pattern.finditer(body):
        profiles.add(sm.group(1))
    return profiles


def test_profile_set_consistent_across_files():
    """T61 #R9: the set of valid profile names in
    limits.py must match what bash_command.py and prompts.py
    know about.
    """
    limits_src = LIMITS_PY.read_text()
    canonical = _extract_profile_set_from_limits(limits_src)
    assert canonical, (
        "limits.py _DEPLOYMENT_PROFILE validation has no "
        "profile names. Refactor must have broken the contract."
    )

    bc_src = BASH_COMMAND_PY.read_text()
    for profile in canonical:
        if profile in ("public", "personal"):
            # Default profiles - they are referenced everywhere
            # in docstrings, not specifically as a quoted string.
            # The check is: the profile name appears as a word
            # in bash_command.py (true for public/personal).
            assert profile in bc_src, (
                f"bash_command.py does not mention profile {profile!r}. "
                f"Even for default profiles, the name should appear "
                f"somewhere in the docstrings or comments. Update the "
                f"file's top-level docstring to mention {profile!r}."
            )
            continue
        # Non-default profiles (dev, and any future) MUST appear
        # as a quoted string literal so that is_dev_mode_chat /
        # is_enabled_for_active_profile explicitly reference them.
        pattern = re.compile(r"""['"]""" + re.escape(profile) + r"""['"]""")
        assert pattern.search(bc_src), (
            f"bash_command.py does not mention non-default profile "
            f"{profile!r} as a quoted string. T61 #R9 wave-trap: "
            f"a non-default profile added in limits.py but not in "
            f"bash_command.py. The new profile would fall through to "
            f"the default branch in is_dev_mode_chat and may not "
            f"activate bash correctly. Add a string literal "
            f"reference to {profile!r} in personal_tools/bash_command.py."
        )

    prompts_src = PROMPTS_PY.read_text()
    for profile in canonical:
        if profile in ("public", "personal"):
            # Default profiles - the LLM does not need to know
            # specific names, just that the bot can run in
            # different modes. Skip the check.
            continue
        assert profile in prompts_src, (
            f"prompts.py does not mention non-default profile "
            f"{profile!r}. T61 #R9 wave-trap: the LLM may not "
            f"understand what the {profile!r} profile does. "
            f"Add a section in prompts.py describing what "
            f"{profile!r} enables."
        )
