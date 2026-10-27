"""Tests for limits.py tunable surface (Track 2, T35).

limits.py defines three deployment profiles (public, personal, dev)
and resolves every limit through `_env_or(env_name, profile_value)`.
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
                 'TRANSCRIBE_TIMEOUT_SEC',
                 'SELFTEST_MAX_TOKENS', 'SELFTEST_LOOP_BUDGET_SEC'):
        assert hasattr(limits, name), f'missing {name}'


def test_public_profile_defaults():
    try:
        limits = _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'public'})
        assert limits.MAX_TOKENS_DEFAULT == 65536
        assert limits.MAX_TOKENS_CAPTION == 16384
        assert limits.LOOP_BUDGET_SEC == 7200
        assert limits.LLAMA_HTTP_TIMEOUT_SEC == 7920.0
    finally:
        _restore_env()


def test_personal_profile_defaults():
    try:
        limits = _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'personal'})
        assert limits.MAX_TOKENS_DEFAULT == 131072
        assert limits.MAX_TOKENS_CAPTION == 65536
        assert limits.LOOP_BUDGET_SEC == 7200
        assert limits.LLAMA_HTTP_TIMEOUT_SEC == 7920.0
    finally:
        _restore_env()


def test_default_profile_is_public():
    try:
        limits = _reload_with({})
        assert limits.MAX_TOKENS_DEFAULT == 65536
    finally:
        _restore_env()


def test_dev_profile_defaults():
    """The dev box: llama-server caps the thinking at 65536, so every
    per-path cap here has to clear that and still leave room for the
    answer. Operator's numbers, 2026-10-23."""
    try:
        limits = _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'dev'})
        assert limits.MAX_TOKENS_DEFAULT == 131072
        assert limits.MAX_TOKENS_CAPTION == 98304
        assert limits.LOOP_BUDGET_SEC == 7200
        assert limits.LLAMA_HTTP_TIMEOUT_SEC == 7920.0
    finally:
        _restore_env()


def test_dev_profile_is_not_the_public_profile():
    """Regression guard for the phantom profile.

    Until 2026-10-23 'dev' was accepted by the validation and then
    fell through to the PUBLIC defaults, so the operator's production
    ran the conservative numbers while believing it ran the
    aggressive ones. A profile that silently resolves to another
    profile is worse than a profile that does not exist."""
    try:
        dev = _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'dev'})
        dev_values = (dev.MAX_TOKENS_DEFAULT, dev.MAX_TOKENS_CAPTION,
                      dev.LOOP_BUDGET_SEC, dev.LLAMA_HTTP_TIMEOUT_SEC)
    finally:
        _restore_env()
    try:
        public = _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'public'})
        public_values = (public.MAX_TOKENS_DEFAULT, public.MAX_TOKENS_CAPTION,
                         public.LOOP_BUDGET_SEC, public.LLAMA_HTTP_TIMEOUT_SEC)
    finally:
        _restore_env()
    assert dev_values != public_values, (
        f"the dev profile resolves to the public numbers {public_values}. "
        f"The operator asked for 128K/96K/3600s on this box; the profile "
        f"table has to be its own."
    )


def test_dev_profile_caps_clear_the_llama_reasoning_budget():
    """Every per-path cap must exceed llama-server's --reasoning-budget
    (65536 on the dev box), or the thinking block can eat the whole
    cap and the answer is lost: observed 2026-10-23 20:45 on the
    document path -- reasoning 77977 chars, content 0."""
    try:
        limits = _reload_with({'LLAMABOT_DEPLOYMENT_PROFILE': 'dev'})
        llama_reasoning_budget = 65536
        for name in ('MAX_TOKENS_DEFAULT', 'MAX_TOKENS_CAPTION'):
            value = getattr(limits, name)
            assert value > llama_reasoning_budget, (
                f"{name}={value} is not above llama-server's "
                f"--reasoning-budget {llama_reasoning_budget}. The thinking "
                f"block can consume the entire cap and the answer is lost."
            )
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
        assert limits.LOOP_BUDGET_SEC == 7200
    finally:
        _restore_env()


def test_every_profile_outlives_the_command_it_runs():
    """The turn's wall-clock budget must be longer than the command limit.

    The guard that enforces the budget runs at the loop's tail: after a
    command returns, the turn is checked against the budget and aborted
    if it is over. So a command that runs for the whole limit has to fit
    *inside* the budget -- otherwise the guard throws the answer away the
    moment the command comes back, and the user gets "time budget
    exceeded" instead of it.

    2026-10-27: the sandbox command limit became an hour (the operator's
    ruling), and the dev profile's budget was exactly an hour -- the two
    numbers met at the edge, which is the one place where a command that
    runs to the limit loses its answer.
    """
    from personal_tools.sandbox_contract import EXEC_TIMEOUT_SECONDS

    for profile in ("public", "personal", "dev"):
        limits = _reload_with({"LLAMABOT_DEPLOYMENT_PROFILE": profile})
        try:
            assert limits.LOOP_BUDGET_SEC > EXEC_TIMEOUT_SECONDS, (
                f"{profile}: the turn budget is {limits.LOOP_BUDGET_SEC}s and "
                f"the sandbox command limit is {EXEC_TIMEOUT_SECONDS}s. A "
                f"command that runs to the limit would have its answer "
                f"thrown away by the budget guard."
            )
        finally:
            _restore_env()


def test_env_overrides_preserve_type():
    try:
        limits = _reload_with({
            'LLAMABOT_DEPLOYMENT_PROFILE': 'public',
            'LLAMABOT_LLAMA_HTTP_TIMEOUT': '700.5',
            'LLAMABOT_LOOP_BUDGET_SEC': '600',
            'LLAMABOT_MAX_TOKENS': '4096',
        })
        assert isinstance(limits.MAX_TOKENS_DEFAULT, int)
        assert limits.MAX_TOKENS_DEFAULT == 4096
        assert isinstance(limits.LLAMA_HTTP_TIMEOUT_SEC, float)
        assert abs(limits.LLAMA_HTTP_TIMEOUT_SEC - 700.5) < 0.001
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


def test_the_bot_has_no_reasoning_cap_of_its_own():
    """The thinking cap belongs to llama-server (--reasoning-budget).

    Operator, 2026-10-23: "управление лимитами если возможно в ллама
    должно происходить в лама" -- the bot must not be the limiter. The
    bot-side REASONING_RATIO_HARD_LIMIT was declared, validated,
    documented, and enforced nowhere (T70.6); it was removed rather
    than left looking like a live guard.

    This test is the tripwire for that decision: no module under
    telegram-bot/ may reference a reasoning-cap knob again. Comments
    and docstrings are ignored on purpose -- the AST only reports
    names the code actually binds or reads."""
    import ast
    import pathlib

    bot_dir = pathlib.Path(__file__).resolve().parent.parent / "telegram-bot"
    offenders = []
    for path in sorted(bot_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and "REASONING_RATIO" in node.id:
                offenders.append(f"{path.name}:{node.lineno} {node.id}")
            elif isinstance(node, ast.Attribute) and "REASONING_RATIO" in node.attr:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
    assert not offenders, (
        f"the bot must not carry its own reasoning cap: {offenders}. "
        f"The thinking budget is llama-server's --reasoning-budget; the "
        f"bot's job is to leave room for the answer after it."
    )


def test_the_removed_ratio_env_var_is_not_read_anymore():
    """A removed knob that still reads is a removed knob in name only."""
    import ast
    import pathlib

    bot_dir = pathlib.Path(__file__).resolve().parent.parent / "telegram-bot"
    for path in sorted(bot_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "LLAMABOT_REASONING_RATIO_LIMIT" not in node.value, (
                    f"{path.name}:{node.lineno} still names "
                    f"LLAMABOT_REASONING_RATIO_LIMIT. If the bot is not "
                    f"supposed to cap reasoning, remove the env var too."
                )


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
        # The three POSTs moved to llm_iteration at T8.1; the timeout
        # travelled with them. Checking call_llama's source for it is
        # exactly the pin-to-a-location that keeps going red.
        from llm_iteration import ToolLoop
        src = inspect.getsource(ToolLoop.run_one)
        assert 'limits.LLAMA_HTTP_TIMEOUT_SEC' in src
        assert 'timeout=600.0' not in src
    finally:
        for mod_name in list(sys.modules):
            if mod_name in ('call_llama', 'limits'):
                del sys.modules[mod_name]
        _restore_env()
