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


def test_every_production_builder_is_counted():
    """Count the builders, do not remember how many there were.

    This test used to assert `== 2`, which was a claim about the shape of
    dispatch.py rather than about the bot. TECH-DEBT #10 collapsed the
    two `Application.builder().token()` sites into one
    `_build_application()`, and the count moved -- correctly.

    What matters is not the number, it is that the sweep above found
    them all: a builder that did not pass base_url must be a red test
    rather than an unexamined line. So this asserts there is at least
    one, and that every one of them carries the override.
    """
    found = [ln for ln, base_url, token
             in _builders(BOT_DIR / "dispatch.py") if token]
    assert found, (
        "no Application.builder().token() in dispatch.py; the bot is not "
        "built here any more, so the base_url sweep is passing on an empty "
        "set")
    missing = [ln for ln, base_url, token
               in _builders(BOT_DIR / "dispatch.py")
               if token and not base_url]
    assert not missing, (
        f"Application.builder().token() at {missing} has no base_url; "
        f"TELEGRAM_API is bypassed and traffic reaches the real "
        f"api.telegram.org")


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

# W7-H1 (2026-10-21 prod outage). Commit 88a119b added
# `.base_url(TELEGRAM_API)` calls; the prior default
# `f"https://api.telegram.org/bot{BOT_TOKEN}"` had the token baked in.
# PTB 21's base_url() then doubled the token into the URL path and
# every dispatch crashed with InvalidToken for ~2 hours.
#
# The earlier tripwire was written with the explicit exception
# "config.py may reference BOT_TOKEN here" -- the rationale was that
# this is the place where the default is constructed, so reading
# BOT_TOKEN was ok. That allowance was precisely the bug.
#
# This tripwire closes the exception: the default value of
# TELEGRAM_API in config.py must be a *prefix* that PTB will append
# /bot<TOKEN>/<method> onto, not a *full URL* with the token already
# baked in. The format is "<api-host>/bot" -- PTB's native default
# is exactly that, and that is the format the dispatcher must hand
# to .base_url().
def test_config_telegram_api_default_does_not_bake_token():
    """W7-H1 (2026-10-21 prod outage).

    Commit 88a119b added `.base_url(TELEGRAM_API)` calls; the prior
    default f"https://api.telegram.org/bot{BOT_TOKEN}" had the token
    baked in. PTB 21 then doubled the token into the URL path
    (/bot<TOKEN>/bot<TOKEN>/<method>) and Telegram replied with
    InvalidToken for ~2 hours.

    The earlier tripwire was written with the explicit exception
    "config.py may reference BOT_TOKEN here" -- the rationale was that
    this is the place where the default is constructed, so reading
    BOT_TOKEN was ok. That allowance was the bug-shaped exception.

    This tripwire closes it: the default value of TELEGRAM_API in
    config.py must be a *prefix* that PTB will append /bot<TOKEN>/<method>
    onto, not a *full URL* with the token already baked in. The format
    is "<api-host>/bot" -- PTB's native default is exactly that, and
    that is the format the dispatcher must hand to .base_url().
    """
    import ast
    src = (BOT_DIR / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    default_value = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "TELEGRAM_API"
                   for t in node.targets):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        # Match the call.func chain. os.environ.get(...) parses as
        # Attribute(Attribute(Name('os', Load), 'environ', Load), 'get', Load).
        # Bare `func.id` only matches a plain name like f(x); here we
        # have an attribute chain, so walk the chain to find the trailing
        # method name and the leading name.
        def _root_name(n):
            while isinstance(n, ast.Attribute):
                n = n.value
            return n.id if isinstance(n, ast.Name) else None
        def _last_attr(n):
            out = None
            while isinstance(n, ast.Attribute):
                if out is None:   # take the OUTERMOST attr (dotted suffix)
                    out = n.attr
                n = n.value
            return out
        if _root_name(node.value.func) != "os":
            continue
        if _last_attr(node.value.func) != "get":
            continue
        if len(node.value.args) < 2:
            continue
        default_value = node.value.args[1]
        break
    assert default_value is not None, (
        "config.py does not declare TELEGRAM_API = os.environ.get(..., "
        "default). If it was renamed or restructured, update this tripwire."
    )

    # The default may be a string literal, or a ternary like
    # "<value>" if BOT_TOKEN else "" (the shape the code uses today).
    # In the ternary case, inspect the truthy branch -- that is the
    # string the dispatcher hands to .base_url() in production.
    if isinstance(default_value, ast.IfExp):
        candidate = default_value.body
        guard = default_value.test
        guard_names = {n.id for n in ast.walk(guard)
                       if isinstance(n, ast.Name)}
        assert guard_names == {"BOT_TOKEN"}, (
            f"TELEGRAM_API default ternary guards on {guard_names!r}, not "
            f"only BOT_TOKEN. Reading other globals here bakes runtime state "
            f"into the value that .base_url() will double into the URL."
        )
    else:
        candidate = default_value

    # Whatever the source shape, the resolved value must be a string
    # constant -- NOT an f-string, NOT a format() call, NOT an
    # os.environ.get(), NOT a Name/BOT_TOKEN reference.
    assert isinstance(candidate, ast.Constant) and isinstance(candidate.value, str), (
        f"TELEGRAM_API default is not a string literal; got "
        f"{type(candidate).__name__}. Reading BOT_TOKEN here bakes the "
        f"token into the value that every downstream .base_url() then "
        f"double-paths into the URL."
    )

    # The default string must end with /bot. That suffix is what keeps
    # PTB 21 constructing /bot<TOKEN>/<method> as a path (with the
    # colon safely inside the path), not /bot<TOKEN>:<method> as an
    # authority (which httpx raises InvalidURL on).
    assert candidate.value.endswith("/bot"), (
        f"TELEGRAM_API default {candidate.value!r} does not end with "
        f"'/bot'. PTB 21's Application.builder().base_url() expects a "
        f"prefix with trailing /bot; a value without it puts the colon "
        f"in the token into the URL authority and httpx raises "
        f"InvalidURL. Real-Telegram default: https://api.telegram.org/bot. "
        f"Local mock (smoke): http://127.0.0.1:18090/bot."
    )

    # And the prefix must not be empty -- a /bot-only default would
    # hit api.telegram.org over HTTPS, which is the wrong server for
    # any smoke test that set TELEGRAM_API to a local mock.
    prefix = candidate.value[: -len("/bot")]
    assert prefix.startswith(("http://", "https://")), (
        f"TELEGRAM_API default {candidate.value!r} is not an http(s) URL."
    )