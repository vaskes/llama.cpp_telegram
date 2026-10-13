"""Tests for limits.py tunable surface (Track 2).

All limits are env-overrideable. Tests verify:
- defaults match production values (Qwen3.8-27B-Heretec-MTP 256k)
- env vars override correctly
- types are correct (int vs float)
- imports work from call_llama.py and handlers.py
"""
import os
import importlib


def test_limits_module_imports():
    """The limits module loads without error."""
    import limits
    assert hasattr(limits, 'MAX_TOKENS_DEFAULT')
    assert hasattr(limits, 'MAX_TOKENS_CAPTION')
    assert hasattr(limits, 'LOOP_BUDGET_SEC')
    assert hasattr(limits, 'LLAMA_HTTP_TIMEOUT_SEC')
    assert hasattr(limits, 'LLAMA_MODELS_TIMEOUT_SEC')
    assert hasattr(limits, 'TRANSCRIBE_TIMEOUT_SEC')
    assert hasattr(limits, 'REASONING_RATIO_HARD_LIMIT')
    assert hasattr(limits, 'SELFTEST_MAX_TOKENS')
    assert hasattr(limits, 'SELFTEST_LOOP_BUDGET_SEC')


def test_max_tokens_default_is_32768():
    """Production max_tokens for plain text is 32768."""
    import limits
    assert limits.MAX_TOKENS_DEFAULT == 32768


def test_max_tokens_caption_is_16384():
    """Captions (photo/voice/document) get 16384, half the text budget."""
    import limits
    assert limits.MAX_TOKENS_CAPTION == 16384


def test_loop_budget_is_600_seconds():
    """Tool-loop wall clock default is 10 minutes (600 sec)."""
    import limits
    assert limits.LOOP_BUDGET_SEC == 600


def test_llama_http_timeout_is_600():
    """httpx timeout to llama-server is 600 sec, matches LOOP_BUDGET."""
    import limits
    assert limits.LLAMA_HTTP_TIMEOUT_SEC == 600.0


def test_reasoning_ratio_hard_limit_is_half():
    """Reasoning budget cap: if reasoning > 50% of max_tokens AND no
    content, surface a clear error instead of truncated tail."""
    import limits
    assert limits.REASONING_RATIO_HARD_LIMIT == 0.5


def test_selftest_max_tokens_smaller():
    """Selftest uses 2048 tokens (vs 32768 prod) for fast smoke."""
    import limits
    assert limits.SELFTEST_MAX_TOKENS < limits.MAX_TOKENS_DEFAULT


def test_env_override_max_tokens(monkeypatch):
    """LLAMABOT_MAX_TOKENS overrides the default."""
    monkeypatch.setenv('LLAMABOT_MAX_TOKENS', '99999')
    importlib.reload(__import__('limits'))
    import limits
    assert limits.MAX_TOKENS_DEFAULT == 99999
    monkeypatch.delenv('LLAMABOT_MAX_TOKENS')
    importlib.reload(__import__('limits'))


def test_env_override_loop_budget(monkeypatch):
    """LLAMABOT_LOOP_BUDGET_SEC overrides the default."""
    monkeypatch.setenv('LLAMABOT_LOOP_BUDGET_SEC', '120')
    importlib.reload(__import__('limits'))
    import limits
    assert limits.LOOP_BUDGET_SEC == 120
    monkeypatch.delenv('LLAMABOT_LOOP_BUDGET_SEC')
    importlib.reload(__import__('limits'))


def test_env_override_http_timeout(monkeypatch):
    """LLAMABOT_LLAMA_HTTP_TIMEOUT overrides the default."""
    monkeypatch.setenv('LLAMABOT_LLAMA_HTTP_TIMEOUT', '120.5')
    importlib.reload(__import__('limits'))
    import limits
    assert abs(limits.LLAMA_HTTP_TIMEOUT_SEC - 120.5) < 0.001
    monkeypatch.delenv('LLAMABOT_LLAMA_HTTP_TIMEOUT')
    importlib.reload(__import__('limits'))


def test_call_llama_uses_limits():
    """call_llama default max_tokens comes from limits, not hardcoded."""
    import limits
    import inspect
    from call_llama import call_llama
    sig = inspect.signature(call_llama)
    # The default must be the limits constant at import time, not 65536
    default = sig.parameters['max_tokens'].default
    assert default == limits.MAX_TOKENS_CALL_LLAMA_DEFAULT
    assert default == limits.MAX_TOKENS_DEFAULT
    # Sanity: must NOT be the old 65536 hardcoded value
    assert default != 65536


def test_call_llama_uses_limits_for_timeouts():
    """call_llama httpx clients use limits.LLAMA_HTTP_TIMEOUT_SEC, not 600.0."""
    import limits
    from call_llama import call_llama
    import inspect
    src = inspect.getsource(call_llama)
    # Must reference the limits constant
    assert 'limits.LLAMA_HTTP_TIMEOUT_SEC' in src
    # Must NOT have the old hardcoded 600.0 in a timeout=
    assert 'timeout=600.0' not in src
