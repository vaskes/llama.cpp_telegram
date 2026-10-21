"""Every documented env var is read, and every read var is documented.

Review round 3: a fork that sets a variable and gets the default is a silent
failure, and nothing in this repo notices. Two directions, both silent:

  - a var documented in .env.example that no code reads: the fork sets it,
    config falls back to the default, and the bot behaves identically to a
    fork that never set anything
  - a var the code reads that .env.example does not mention: nobody can
    discover it

The third hazard -- a typo in the live .env -- is handled at startup with a
warning rather than a hard failure, because this repository is a fork base
with 200+ forks and a startup abort on an unrelated variable would take every
one of them down. See config.warn_unrecognised_env().
"""
import ast
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

EXAMPLE = BOT_DIR / ".env.example"

# Variables the runtime legitimately reads from the process environment that
# are not part of the bot's own configuration surface.
INFRA = {
    "PATH", "HOME", "PYTHONUNBUFFERED", "PYTHONPATH", "HOSTNAME", "TZ",
    "LANG", "LC_ALL", "USER", "SHELL", "PWD", "TERM",
    # container / compose plumbing
    "AGENT_STACK_URL", "BOT_INSTANCE", "SUPERVISOR_ENABLED",
}

# Not a regex over access patterns. The first version matched
# os.environ.get / os.environ[] / os.getenv and reported eight variables as
# dead that limits.py reads through its own _env_or("NAME", default) helper.
# That is the lying-checker failure this repo has now produced four times, and
# the fix is the same each time: enumerate what the code can do rather than
# guess at the shapes.
#
# So: every string literal in the module that looks like an env var counts as
# a reference. That is a superset -- a name mentioned only in a comment will
# look alive -- and the tradeoff is deliberate. A checker that blocks a merge
# because it missed a calling convention is worse than one that lets a
# commented-out name through. The residual gap is written down below rather
# than hidden.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


def _documented() -> set[str]:
    names = set()
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#"):
            line = line[1:].strip()
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if m:
            names.add(m.group(1))
    return names


def _read_by_code() -> set[str]:
    """Every env-var-shaped string literal anywhere in telegram-bot/*.py.

    Covers os.environ.get, os.environ[], os.getenv, limits._env_or, and any
    other helper, present or future, without having to enumerate them.
    """
    names = set()
    # rglob, not glob: personal_tools/ holds bash_command.py, which reads
    # LLAMABOT_BASH_RPM and LLAMABOT_DEV_ALLOWED_CHAT_IDS. A top-level-only
    # scan reported both as dead, which is the fourth lying-checker in this
    # repo and the fourth time the cause was the same: the checker looked
    # where it expected to find things.
    for path in BOT_DIR.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _ENV_NAME_RE.match(node.value):
                    names.add(node.value)
    return names


def test_read_env_vars_are_documented_scanner_is_not_too_narrow():
    """Guards the scanner itself.

    _env_or() in limits.py was invisible to the previous pattern-based scan,
    and eight live variables looked dead. If someone tightens the matcher
    again, this fails rather than the next var silently going unmonitored.
    """
    read = _read_by_code()
    for name in ("LLAMABOT_MAX_TOKENS", "LLAMABOT_LOOP_BUDGET_SEC",
                 "LLAMABOT_TRANSCRIBE_TIMEOUT"):
        assert name in read, (
            f"{name} is read via limits._env_or but the scanner no longer "
            f"sees it. The scanner over-matches on purpose; narrowing it "
            f"reintroduces false positives."
        )


def _narrow_read() -> set:
    """Env vars read through a call, by access pattern.

    Kept separate from the broad scanner on purpose: see the two tests for
    why they need different matchers.
    """
    names = set()
    for path in BOT_DIR.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant):
                continue
            if isinstance(first.value, str) and _ENV_NAME_RE.match(first.value):
                fn = node.func
                fname = (fn.attr if isinstance(fn, ast.Attribute)
                         else getattr(fn, "id", ""))
                if fname in {"get", "getenv", "_env_or", "int", "float",
                             "bool", "str"}:
                    names.add(first.value)
    return names


def test_documented_env_vars_are_actually_read():
    documented = _documented()
    read = _read_by_code() | INFRA
    dead = sorted(documented - read)
    assert not dead, (
        f"documented in .env.example but never read by any code: {dead}. "
        f"A fork that sets one of these gets the default and no warning, "
        f"which is indistinguishable from not setting it. Either wire it up "
        f"or delete the line."
    )


def test_read_env_vars_are_documented():
    """The reverse direction, using the NARROW matcher.

    The broad matcher cannot be used here: every string literal in the tree
    looks like an env var to it, including "GET", "POST" and "MAX_". Broad
    matching is right for "is this documented name alive" and wrong for "is
    this live name documented", because the two questions have opposite
    failure modes -- a missed dead name and a spurious blocker.
    """
    documented = _documented()
    read = _narrow_read()
    undocumented = sorted(read - documented)
    assert not undocumented, (
        f"read by code but absent from .env.example: {undocumented}. Nobody "
        f"can discover these without reading the source."
    )


def test_config_warns_about_unrecognised_env_vars(caplog):
    """A typo in the live .env must not be silent.

    Warning, not abort: this is a fork base, and refusing to boot on an
    unrelated variable would take down every fork that happens to export one.
    """
    from config import warn_unrecognised_env

    known = _documented() | _read_by_code() | INFRA
    warnings = warn_unrecognised_env({"BOT_REMEMBER_DAYZ": "7"})
    assert any("BOT_REMEMBER_DAYZ" in w for w in warnings), (
        "a typo'd variable was not reported; the fork set something and the "
        "bot silently used defaults"
    )
    clean = warn_unrecognised_env({"BOT_REMEMBER_DAYS": "7"})
    assert not clean, (
        f"a known variable was reported as unknown: {clean}"
    )
    assert isinstance(known, set)


def test_env_matching_is_prefix_aware_not_substring():
    """`BOT_REMEMBER_DAYZ` is not `BOT_REMEMBER_DAYS`.

    A substring check would call the first one known and miss the typo, which
    is the exact case the warning exists for.
    """
    from config import warn_unrecognised_env

    warnings = warn_unrecognised_env({
        "BOT_REMEMBER_DAYZ": "7",
        "MAX_VIDEO_NOTE_BYTEZ": "1",
        "MAX_VIDEO_NOTE_BYTES": "1",
    })
    joined = " ".join(warnings)
    assert "BOT_REMEMBER_DAYZ" in joined
    assert "MAX_VIDEO_NOTE_BYTEZ" in joined
    assert "MAX_VIDEO_NOTE_BYTES" not in joined, (
        "a correctly spelled variable was flagged"
    )