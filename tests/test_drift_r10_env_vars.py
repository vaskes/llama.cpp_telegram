"""T61 #R10 v2: every env var referenced in code is documented
in .env.example.

Wave-trap pattern (predicted): a future commit adds
os.environ.get("LLAMABOT_NEW_THING") in some .py file
but forgets to add it to .env.example.

This tripwire extracts every env var reference in
telegram-bot/ and asserts each one is documented in
.env.example either as `VAR=default` (active) or
`# VAR=...` (commented example).
"""
from __future__ import annotations

import re
import pathlib


TELEGRAM_BOT_DIR = (
    pathlib.Path(__file__).parent.parent / "telegram-bot"
)
ENV_EXAMPLE = TELEGRAM_BOT_DIR / ".env.example"


def _extract_env_var_refs_from_py(src: str) -> set:
    names: set = set()
    pattern_get = re.compile(r"""os\.environ\.get\(\s*['"]([A-Z][A-Z0-9_]+)['"]""")
    for m in pattern_get.finditer(src):
        names.add(m.group(1))
    pattern_sub = re.compile(r"""os\.environ\[\s*['"]([A-Z][A-Z0-9_]+)['"]\s*\]""")
    for m in pattern_sub.finditer(src):
        names.add(m.group(1))
    return names


def _extract_env_vars_documented(example_src: str) -> set:
    """Find every env var name in .env.example, whether
    active (VAR=...) or commented (# VAR=...)."""
    names: set = set()
    # Active: VAR=...
    for line in example_src.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]+)\s*=", line)
        if m:
            names.add(m.group(1))
    # Commented: # VAR=... or # comment about VAR
    pattern_commented = re.compile(r"^#\s*([A-Z][A-Z0-9_]+)\s*=", re.MULTILINE)
    for m in pattern_commented.finditer(example_src):
        names.add(m.group(1))
    return names


def test_every_env_var_in_code_is_documented():
    """T61 #R10: every os.environ.get/set reference in
    telegram-bot/ must be documented in .env.example.
    """
    code_env_vars: set = set()
    for path in TELEGRAM_BOT_DIR.glob("*.py"):
        src = path.read_text()
        code_env_vars |= _extract_env_var_refs_from_py(src)
    for path in (TELEGRAM_BOT_DIR / "personal_tools").glob("*.py"):
        src = path.read_text()
        code_env_vars |= _extract_env_var_refs_from_py(src)

    example_src = ENV_EXAMPLE.read_text()
    documented = _extract_env_vars_documented(example_src)

    missing = code_env_vars - documented
    SYSTEM_VARS = {"PATH", "HOME", "USER", "LANG", "SHELL", "PWD", "TERM", "SHLVL", "_"}
    missing -= SYSTEM_VARS

    assert not missing, (
        f"Env vars referenced in code but not documented in "
        f".env.example: {sorted(missing)}. T61 #R10 wave-trap: "
        f"a future commit adds an env var reference without "
        f"updating .env.example. Add a line VAR=default_value "
        f"(or `# VAR=example`) to telegram-bot/.env.example."
    )
