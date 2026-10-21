"""Every Application.builder() must carry base_url.

W2-H2. `config.TELEGRAM_API` is env-driven so a smoke test can point the bot
at a mock Telegram. Before this, only the raw-httpx `getUpdates` loop used
it -- `dispatch.py:956` had just been de-clobbered, which made it look
fixed. Every call through the PTB object still went to the real API:

    getUpdates            raw httpx   TELEGRAM_API    (was already fixed)
    sendMessage           PTB Bot     api.telegram.org
    editMessageText       PTB Bot     api.telegram.org
    getMe                 PTB Bot     api.telegram.org
    setMyCommands         PTB Bot     api.telegram.org
    set_message_reaction  PTB Bot     api.telegram.org
    get_file / download   PTB Bot     api.telegram.org

So half of any smoke test would have reached production Telegram.

The previous tripwire in test_telegram_api_source.py forbids *rebuilding*
the constant. That is a different hole from failing to *pass* it, and it
passed the mutation while the bug was live. This one checks the thing that
was actually broken.
"""
import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"


def _chain_attrs(call):
    """Attribute names along a builder chain, outermost first.

    `Application.builder().token(X).base_url(Y).build()` nests outward, so
    the node ast.walk yields first is the build() call, not builder().
    Walking from `builder` upward matches nothing -- that version of this
    helper found zero builders and every test below it passed vacuously.
    """
    names = []
    cur = call
    while isinstance(cur, ast.Call):
        if isinstance(cur.func, ast.Attribute):
            names.append(cur.func.attr)
            cur = cur.func.value
        else:
            break
    return names


def _builders(path):
    """Yield (lineno, has_base_url, has_token) for each builder chain."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        names = _chain_attrs(node)
        # Only the outermost call of the chain. ast.walk also visits
        # .builder(), .token() and .base_url() as separate Call nodes, each
        # of which contains 'builder' in its downward chain -- counting them
        # reported three hits per builder and flagged the intermediate ones
        # for not carrying base_url themselves.
        if not names or names[0] != "build" or "builder" not in names:
            continue
        yield node.lineno, "base_url" in names, "token" in names


@pytest.mark.parametrize("path", sorted(BOT_DIR.glob("*.py")),
                         ids=lambda p: p.name)
def test_every_application_builder_sets_base_url(path):
    offenders = [ln for ln, base_url, _ in _builders(path) if not base_url]
    assert not offenders, (
        f"{path.name}: Application.builder() at lines {offenders} has no "
        f".base_url(TELEGRAM_API). Without it every PTB call -- sendMessage, "
        f"getMe, setMyCommands, reactions, file downloads -- bypasses "
        f"TELEGRAM_API and reaches the real api.telegram.org."
    )


def test_both_production_builders_are_covered():
    """The two in dispatch.py are the ones that matter. Counting them keeps
    the sweep above from passing because it found zero builders."""
    found = [ln for ln, _, token in _builders(BOT_DIR / "dispatch.py")
             if token]
    assert len(found) == 2, (
        f"expected 2 Application.builder().token() calls in dispatch.py, "
        f"found {len(found)} at {found}. If that count moved, this file's "
        f"assumption about where the bot is constructed is stale."
    )


def test_config_still_honours_the_env_override():
    """Why the whole thing exists."""
    source = (BOT_DIR / "config.py").read_text(encoding="utf-8")
    assert "os.environ.get" in source
    assert "TELEGRAM_API" in source, (
        "config.py no longer defines TELEGRAM_API; nothing to point at"
    )


def test_no_module_rebuilds_telegram_api_from_bot_token():
    """The earlier rule, kept. Rebuilding the constant discards the env
    override even when base_url is passed correctly."""
    for path in sorted(BOT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(getattr(t, "id", "") == "TELEGRAM_API"
                       for t in node.targets):
                continue
            names = {n.id for n in ast.walk(node.value)
                     if isinstance(n, ast.Name)}
            assert "BOT_TOKEN" not in names or path.name == "config.py", (
                f"{path.name}:{node.lineno} rebuilds TELEGRAM_API from "
                f"BOT_TOKEN, discarding the env override"
            )