"""Tests for limits.py tunable surface (Track 2, T35).

limits.py defines two deployment profiles (public, personal) and
resolves every limit through `_env_or(env_name, profile_value)`.
"""
import os
import importlib
import sys


_ORIGINAL_ENV = dict(os.environ)


def _reload_with(env):
    """Set env, reload limits, return module."""
    os.environ.update(env)
    for mod_name in list(sys.modules):
        if mod_name == 'limits':
            del sys.modules[mod_name]
    import limits
    importlib.reload(limits)
    return limits


def _restore_env():
    for k in list(os.environ.keys()):
        if k not in _ORIGINAL_ENV:
            os.environ.pop(k, None)
    for k, v in _ORIGINAL_ENV.items():
        os.environ[k] = v


def test_limits_module_imports():
    try:
        import limits
    finally:
        _restore_env()
    for name in ('MAX_TOKENS_DEFAULT', 'MAX_TOKENS_CAPTION', 'LOOP_BUDGET_SEC',
                 'LLAMA_HTTP_TIMEOUT_SEC', 'LLAMA_MODELS_TIMEOUT_SEC',
                 'TRANSCRIBE_TIMEOUT_SEC', 'REASONING_RATIO_HARD_LIMIT',
                 'SELFTEST_MAX_TOKENS', 'SELFTEST_LOOP_BUDGET_SEC'):
        assert hasattr(limits, name), f'missing {name}'


def test_public_profile_defaults():
    try:
        limits = _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'public'})
        assert limits.MAX_TOKENS_DEFAULT == 65536
        assert limits.MAX_TOKENS_CAPTION == 16384
        assert limits.LOOP_BUDGET_SEC == 900
        assert limits.LLAMA_HTTP_TIMEOUT_SEC == 990.0
        assert limits.REASONING_RATIO_HARD_LIMIT == 0.95
    finally:
        _restore_env()


def test_personal_profile_defaults():
    try:
        limits = _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'personal'})
        assert limits.MAX_TOKENS_DEFAULT == 131072
        assert limits.MAX_TOKENS_CAPTION == 65536
        assert limits.LOOP_BUDGET_SEC == 1200
        assert limits.LLAMA_HTTP_TIMEOUT_SEC == 1320.0
        assert limits.REASONING_RATIO_HARD_LIMIT == 1.0
    finally:
        _restore_env()


def test_default_profile_is_public():
    try:
        limits = _reload_with({})
        assert limits.MAX_TOKENS_DEFAULT == 65536
    finally:
        _restore_env()


def test_env_overrides_profile_default():
    try:
        limits = _reload_with({
            'LLAMABOT_DEPLOYMENT_PROFILE': 'public',
            'LLAMABOT_MAX_TOKENS': '99999',
        })
        assert limits.MAX_TOKENS_DEFAULT == 99999
        assert limits.MAX_TOKENS_CAPTION == 16384
    finally:
        _restore_env()


def test_env_overrides_personal_profile():
    try:
        limits = _reload_with({
            'LLAMABOT_DEPLOYMENT_PROFILE': 'personal',
            'LLAMABOT_MAX_TOKENS': '12345',
        })
        assert limits.MAX_TOKENS_DEFAULT == 12345
        assert limits.LOOP_BUDGET_SEC == 1200
    finally:
        _restore_env()


def test_env_overrides_preserve_type():
    try:
        limits = _reload_with({
            'LLAMABOT_DEPLOYMENT_PROFILE': 'public',
            'LLAMABOT_LLAMA_HTTP_TIMEOUT': '700.5',
            'LLAMABOT_LOOP_BUDGET_SEC': '600',
            'LLAMABOT_MAX_TOKENS': '4096',
            'LLAMABOT_REASONING_RATIO_LIMIT': '0.33',
        })
        assert isinstance(limits.MAX_TOKENS_DEFAULT, int)
        assert limits.MAX_TOKENS_DEFAULT == 4096
        assert isinstance(limits.LLAMA_HTTP_TIMEOUT_SEC, float)
        assert abs(limits.LLAMA_HTTP_TIMEOUT_SEC - 700.5) < 0.001
        assert isinstance(limits.REASONING_RATIO_HARD_LIMIT, float)
    finally:
        _restore_env()


def test_invalid_profile_rejected():
    try:
        try:
            _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'bogus'})
        except ValueError as e:
            assert 'bogus' in str(e)
            assert 'public' in str(e)
            assert 'personal' in str(e)
        else:
            raise AssertionError('expected ValueError on invalid profile')
    finally:
        _restore_env()


def test_http_timeout_must_exceed_loop_budget():
    try:
        try:
            _reload_with({
                'LLAMABOT_DEPLOYMENT_PROFILE': 'public',
                'LLAMABOT_LLAMA_HTTP_TIMEOUT': '10',
                'LLAMABOT_LOOP_BUDGET_SEC': '60',
            })
        except ValueError as e:
            assert 'LLAMA_HTTP_TIMEOUT_SEC' in str(e)
            assert 'LOOP_BUDGET_SEC' in str(e)
        else:
            raise AssertionError('expected ValueError on timeout <= budget')
    finally:
        _restore_env()


def test_reasoning_ratio_must_be_in_open_interval():
    try:
        try:
            _reload_with({
                'LLAMABOT_DEPLOYMENT_PROFILE': 'public',
                'LLAMABOT_REASONING_RATIO_LIMIT': '1.5',
            })
        except ValueError as e:
            assert 'REASONING_RATIO_HARD_LIMIT' in str(e)
        else:
            raise AssertionError('expected ValueError on ratio > 1')
    finally:
        _restore_env()


def test_max_tokens_must_be_positive():
    try:
        try:
            _reload_with({
                'LLAMABOT_DEPLOYMENT_PROFILE': 'public',
                'LLAMABOT_MAX_TOKENS': '0',
            })
        except ValueError as e:
            assert 'MAX_TOKENS_DEFAULT' in str(e)
        else:
            raise AssertionError('expected ValueError on zero max_tokens')
    finally:
        _restore_env()


def test_selftest_max_tokens_smaller_than_production():
    try:
        limits = _reload_with({})
        assert limits.SELFTEST_MAX_TOKENS < limits.MAX_TOKENS_DEFAULT
    finally:
        _restore_env()


def test_call_llama_default_tracks_active_profile():
    """call_llama signature default max_tokens tracks the active profile.

    Because call_llama is `from call_llama import call_llama` and
    Python caches the import, we have to drop call_llama from
    sys.modules so it re-binds the limits module attribute
    at import time.
    """
    import inspect
    try:
        _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'public'})
        for mod_name in list(sys.modules):
            if mod_name in ('call_llama', 'limits'):
                del sys.modules[mod_name]
        from call_llama import call_llama
        sig = inspect.signature(call_llama)
        assert sig.parameters["max_tokens"].default == 65536
    finally:
        for mod_name in list(sys.modules):
            if mod_name in ('call_llama', 'limits'):
                del sys.modules[mod_name]
        _restore_env()
    try:
        _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'personal'})
        from call_llama import call_llama
        sig = inspect.signature(call_llama)
        assert sig.parameters["max_tokens"].default == 131072
    finally:
        for mod_name in list(sys.modules):
            if mod_name in ('call_llama', 'limits'):
                del sys.modules[mod_name]
        _restore_env()


def test_call_llama_uses_limits_for_timeouts():
    try:
        _reload_with({})
        from call_llama import call_llama
        import inspect
        src = inspect.getsource(call_llama)
        assert 'limits.LLAMA_HTTP_TIMEOUT_SEC' in src
        assert 'timeout=600.0' not in src
    finally:
        for mod_name in list(sys.modules):
            if mod_name in ('call_llama', 'limits'):
                del sys.modules[mod_name]
        _restore_env()
