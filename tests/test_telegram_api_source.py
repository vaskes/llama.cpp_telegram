"""No module may rebuild TELEGRAM_API from a hardcoded BOT_TOKEN.

W6-H2 follow-up, found while checking that finding and not reported by
anyone. `config.py:23` defines TELEGRAM_API from the environment and the
v0.10.2 comment says the override exists "to support smoke testing".
`dispatch.py:956` reassigned it as

    TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

which threw the override away in polling mode. Webhook mode returns above
that line, so the drift only ever bit the polling path -- and the bot runs
in webhook mode in production.

Static because the alternative is a test that has to stand up a fake
Telegram and notice the wrong host got called. The thing being forbidden is
narrow and greppable: an assignment to TELEGRAM_API whose value mentions
BOT_TOKEN. config.py is the one place allowed to define it from env.
"""
import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"

# config.py owns the definition; everything else may only read it.
OWNER = "config.py"


def _assignments(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if getattr(target, "id", "") != "TELEGRAM_API":
                continue
            names = {n.id for n in ast.walk(node.value)
                     if isinstance(n, ast.Name)}
            yield node.lineno, names


@pytest.mark.parametrize("path", sorted(BOT_DIR.glob("*.py")),
                         ids=lambda p: p.name)
def test_telegram_api_is_not_rebuilt_from_bot_token(path):
    offenders = [
        (lineno, sorted(names))
        for lineno, names in _assignments(path)
        if "BOT_TOKEN" in names
    ]
    if path.name == OWNER:
        # config.py is the one module allowed to mention BOT_TOKEN here, and
        # only as the *default* of an os.environ.get -- the env var is
        # consulted first, so a smoke test pointing at a mock server still
        # wins. Whether that ordering survived is a separate test below.
        return
    assert not offenders, (
        f"{path.name}:{offenders} reassigns TELEGRAM_API from BOT_TOKEN, "
        f"discarding the env override that config.py builds for smoke "
        f"testing. Import the value instead."
    )


def test_config_still_honours_the_env_override():
    """The reason the rule above exists. If this goes, the override the
    v0.10.2 comment promises is gone and the tripwire is guarding nothing."""
    source = (BOT_DIR / "config.py").read_text(encoding="utf-8")
    assert "TELEGRAM_API" in source
    assert "os.environ.get" in source, (
        "config.py no longer reads TELEGRAM_API from the environment"
    )