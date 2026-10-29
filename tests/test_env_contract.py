"""Startup warns about env vars nothing reads.

This file used to contain a hand-rolled AST linter that reconciled
.env.example against every string literal in the source tree. It took four
iterations, reported eight live variables as dead because limits.py reads
through its own _env_or() helper, then reported GET and POST as
undocumented variables, then missed personal_tools/, and finally derived its
known-set from a file that does not ship in the container -- so it produced
the right answer in CI and wrong answers in production.

A bot does not need a linter for its own configuration. What is worth
keeping is the runtime warning, which is ten lines and does something no test
can: it reaches the operator's actual environment. That is tested here.

The contract checks went with the linter. What they found is already in the
tree: LLAMABOT_LLAMA_MODELS_TIMEOUT, LLAMABOT_MAX_TOKENS_CAPTION and
LLAMABOT_MAX_TOKENS_REACT_ONLY were read by code and documented nowhere, and
are now in .env.example.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


def test_config_warns_about_unrecognised_env_vars():
    """A typo in the live .env must not be silent.

    This is the check that earns its keep: it runs against the operator's
    actual environment, which no CI check can see.

    Warning, not abort. This is a public repo with a public image, and
    refusing to boot over an unrelated exported variable would take every
    deployment down.
    """
    from config import warn_unrecognised_env

    warnings = warn_unrecognised_env({"BOT_REMEMBER_DAYZ": "7"})
    assert any("BOT_REMEMBER_DAYZ" in w for w in warnings), (
        "a typo'd variable was not reported; the fork set something and the "
        "bot silently used defaults"
    )
    clean = warn_unrecognised_env({"BOT_REMEMBER_DAYS": "7"})
    assert not clean, f"a known variable was reported as unknown: {clean}"


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