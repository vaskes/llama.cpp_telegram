"""T58: Developer mode (operator opt-in bash_command in non-private chats).

Mavis wave-5 carried the T58 dev mode question to wave-6
planning. The user (operator) requested: "developer mode
which unlocks this functionality for my prod [supergroup],
self-evidently this feature must be painted red - do not
touch or you will die".

Design constraints (per T58 review notes):
  * Dev mode is a THIRD profile (public / personal / dev),
    not a flag on personal.
  * Both env vars must be set:
      LLAMABOT_DEPLOYMENT_PROFILE=dev
      LLAMABOT_DEV_ALLOWED_CHAT_IDS=-100..,-100..
  * The chat_id list is hardcoded at operator level. The bot
    reads it at import time; restart required.
  * The bot's user-facing copy includes a loud red DEV MODE
    badge on every response in a dev-mode chat. The operator
    can never forget the chat is unrestricted.
  * Defence in depth: the T52 #H1 chat-type=private gate is
    BYPASSED only for chat_ids in the list. All other gates
    (rate limit, validators, per-session auth) still apply.
  * AST tests pin the contract so a future contributor cannot
    silently change the gate logic.

These tests are the single source of truth for the dev-mode
contract. They run in CI and pin:
  - the env-var parsing in bash_command
  - the chat-type gate logic
  - the badge text
  - the limits.py profile validation
"""
import os
import sys
import importlib
import pathlib
import pytest

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "telegram-bot"))


@pytest.fixture
def personal_with_token(monkeypatch):
    """profile=personal + token; the T38 default state."""
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "personal")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token-12345")
    import personal_tools.bash_command
    importlib.reload(personal_tools.bash_command)
    return personal_tools.bash_command


@pytest.fixture
def dev_with_allowlist(monkeypatch):
    """profile=dev + a list of allowed chat_ids."""
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setenv("LLAMABOT_DEV_ALLOWED_CHAT_IDS", "-100123,-100456")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token-12345")
    import personal_tools.bash_command
    importlib.reload(personal_tools.bash_command)
    return personal_tools.bash_command


@pytest.fixture
def dev_no_allowlist(monkeypatch):
    """profile=dev but no allowed list. Should be safe (fail-closed)."""
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "dev")
    monkeypatch.setenv("LLAMABOT_DEV_ALLOWED_CHAT_IDS", "")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token-12345")
    import personal_tools.bash_command
    importlib.reload(personal_tools.bash_command)
    return personal_tools.bash_command


@pytest.fixture
def public_no_dev(monkeypatch):
    """profile=public. Dev mode must never activate."""
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "public")
    monkeypatch.setenv("LLAMABOT_DEV_ALLOWED_CHAT_IDS", "-100123")
    monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token-12345")
    import personal_tools.bash_command
    importlib.reload(personal_tools.bash_command)
    return personal_tools.bash_command


# === is_dev_mode_chat: contract ===

def test_dev_mode_off_in_personal_profile(personal_with_token):
    """Personal profile: is_dev_mode_chat always False even if
    the chat_id is in the (empty) list."""
    assert personal_with_token.is_dev_mode_chat(-100123) is False
    assert personal_with_token.is_dev_mode_chat(12345) is False
    assert personal_with_token.is_dev_mode_chat(None) is False


def test_dev_mode_off_in_public_profile(public_no_dev):
    """Public profile: is_dev_mode_chat always False. The
    env-var allowlist is silently ignored."""
    assert public_no_dev.is_dev_mode_chat(-100123) is False
    assert public_no_dev.is_dev_mode_chat(99999) is False


def test_dev_mode_off_when_allowlist_empty(dev_no_allowlist):
    """profile=dev but empty allowlist: fail-closed."""
    assert dev_no_allowlist.is_dev_mode_chat(-100123) is False
    assert dev_no_allowlist.is_dev_mode_chat(0) is False


def test_dev_mode_on_only_for_listed_chat(dev_with_allowlist):
    """profile=dev + allowlist: True only for the listed chats."""
    bc = dev_with_allowlist
    assert bc.is_dev_mode_chat(-100123) is True
    assert bc.is_dev_mode_chat(-100456) is True
    assert bc.is_dev_mode_chat(-100999) is False
    assert bc.is_dev_mode_chat(0) is False
    assert bc.is_dev_mode_chat(None) is False


def test_dev_mode_requires_both_env_vars(monkeypatch):
    """Setting only ONE of the two env vars must NOT enable
    dev mode. Tested across 4 combinations of (profile=dev,
    allowlist set) x (profile=personal, allowlist empty)."""
    cases = [
        # (profile, allowlist, expected for in-list chat)
        ("dev", "", False),   # no list -> safe
        ("dev", "-100123", True),  # both set -> on
        ("personal", "-100123", False),  # only list, not dev profile
        ("public", "-100123", False),  # only list, public profile
        ("", "-100123", False),  # only list, no profile
    ]
    for profile, allowlist, expected in cases:
        monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", profile)
        monkeypatch.setenv("LLAMABOT_DEV_ALLOWED_CHAT_IDS", allowlist)
        monkeypatch.setenv("AGENT_STACK_TOKEN", "test-token")
        import personal_tools.bash_command
        importlib.reload(personal_tools.bash_command)
        result = personal_tools.bash_command.is_dev_mode_chat(-100123)
        assert result is expected, (
            f"profile={profile!r} allowlist={allowlist!r}: "
            f"is_dev_mode_chat(-100123) returned {result}, expected {expected}"
        )


# === call_bash_command: chat_type gate bypass ===

def test_personal_mode_refuses_supergroup(personal_with_token, monkeypatch):
    """Personal profile: supergroup (chat_type != 'private')
    is refused even though the env token is set."""
    import asyncio
    import personal_tools.bash_command
    result = asyncio.run(
        personal_tools.bash_command.call_bash_command(
            "ls", chat_type="supergroup", session_id="chat-12345",
        )
    )
    # The result is a coroutine, not a value. Use asyncio.run.
    import asyncio
    result = asyncio.run(
        personal_tools.bash_command.call_bash_command(
            "ls", chat_type="supergroup", session_id="chat-12345",
        )
    )
    assert result["ok"] is False
    assert "only available in private chats" in result["error"]


def test_dev_mode_allows_supergroup_for_listed_chat(dev_with_allowlist):
    """Dev mode + listed chat_id: supergroup is allowed
    (would fail in personal mode). The function returns ok
    only if the sandbox side-car is reachable; what we check
    is that the chat_type gate does NOT refuse."""
    import personal_tools.bash_command
    import asyncio
    import httpx
    # Mock httpx to return a known response so the test does not
    # depend on a real sandbox side-car.
    orig_post = httpx.AsyncClient.post
    async def fake_post(self, *a, **kw):
        class R:
            status_code = 200
            text = "{}"
            def json(self):
                return {"ok": True, "stdout": "hi", "stderr": "", "exit_code": 0, "duration_ms": 5}
        return R()
    httpx.AsyncClient.post = fake_post
    try:
        result = asyncio.run(
            personal_tools.bash_command.call_bash_command(
                "ls", chat_type="supergroup", session_id="chat--100123",
            )
        )
    finally:
        httpx.AsyncClient.post = orig_post
    # If dev mode worked, the result is either ok=True (sandbox
    # reached) or a network error (sandbox unreachable). What
    # matters is the chat_type gate did NOT refuse.
    if not result["ok"]:
        assert "only available in private chats" not in result["error"], (
            f"dev mode should bypass chat-type gate; got: {result['error']}"
        )


def test_dev_mode_still_refuses_unlisted_chat(dev_with_allowlist):
    """Dev mode + UNLISTED chat_id: chat-type gate still applies.
    Even though profile=dev, a chat not in the allowlist is
    refused just like in personal mode."""
    import personal_tools.bash_command
    import asyncio
    result = asyncio.run(
        personal_tools.bash_command.call_bash_command(
            "ls", chat_type="supergroup", session_id="chat-99999",
        )
    )
    assert result["ok"] is False
    assert "only available in private chats" in result["error"]


def test_dev_mode_still_refuses_public_profile(public_no_dev):
    """public profile + dev allowlist: chat-type gate still
    applies. The allowlist is silently ignored in public."""
    import personal_tools.bash_command
    import asyncio
    result = asyncio.run(
        personal_tools.bash_command.call_bash_command(
            "ls", chat_type="supergroup", session_id="chat-100123",
        )
    )
    assert result["ok"] is False


# === No dev_mode_badge (T58 revert) ===

def test_dev_mode_badge_removed_per_operator_request():
    """T58 revert (operator feedback 2026-10-14): the dev_mode_badge
    function was removed from dispatch. The operator explicitly
    requested that dev mode = ONLY remove the chat_type gate,
    with NO visual or any other effects in bot messages. The
    red "loud" badge was over-engineering. This test pins the
    absence so a future contributor does not silently re-add it.
    """
    import dispatch
    assert not hasattr(dispatch, "dev_mode_badge"), (
        "T58 revert: dispatch.dev_mode_badge was removed per operator "
        "request. Dev mode = only remove the chat_type gate. No visual "
        "badges in bot messages. Re-add only with explicit operator "
        "approval and a clear threat-model justification."
    )


def test_no_dev_prefix_in_send_reply(personal_with_token):
    """The send_reply function must NOT prepend any dev-mode
    text to bot responses. Dev mode is invisible in the user-
    facing chat; it only relaxes the chat_type gate inside
    call_bash_command.
    """
    import inspect
    from handlers import send_reply
    src = inspect.getsource(send_reply)
    # No references to dev_mode, dev badge, or "🔴" in send_reply.
    assert "dev_mode_badge" not in src, (
        "send_reply must not call dev_mode_badge; T58 revert removed "
        "user-facing dev-mode visual effects."
    )
    assert "🔴" not in src and chr(0x1F534) not in src, (
        "send_reply must not contain the red-circle emoji; T58 revert "
        "removed user-facing dev-mode visual effects."
    )


# === limits.py: dev profile accepted ===

def test_limits_accepts_dev_profile(monkeypatch):
    """limits.py must accept 'dev' as a valid profile (T58)."""
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "dev")
    # Reload limits
    import limits
    importlib.reload(limits)
    # If we got here without ValueError, the test passes.


def test_limits_rejects_unknown_profile(monkeypatch):
    """limits.py must still reject unknown profiles."""
    monkeypatch.setenv("LLAMABOT_DEPLOYMENT_PROFILE", "gibberish")
    with pytest.raises(ValueError, match="not a recognised profile"):
        import limits
        importlib.reload(limits)
