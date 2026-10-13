"""T53: AST-level regression for the chat_type fail-closed contract.

T52 (commit 76df436) added chat_type=update.effective_chat.type to
all 4 handlers' call_llama invocations, but forgot to add the
parameter to the call_llama signature itself. The bot crashed on
every message with TypeError: call_llama() got an unexpected
keyword argument 'chat_type'. Mavis's wave-3 review caught this
as #R3 (CRITICAL). The unit tests at the time all mocked
handlers.call_llama, so the bug never surfaced in pytest.

These tests are AST-level (milliseconds, no I/O) and pin three
contracts:

  1. call_llama accepts chat_type kwarg.
  2. call_llama refuses to run without chat_type (fail-closed).
  3. Every await call_llama(...) in handlers.py passes chat_type.

The third check would have caught the original T52 bug: a regex
over the four call sites would have flagged the missing
parameter at commit time.
"""
import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "telegram-bot"))

import pytest


def _parse(path):
    return ast.parse(path.read_text())


def test_call_llama_accepts_chat_type_kwarg():
    """The signature must have a parameter named chat_type.
    The T52 bug: chat_type was passed by all 4 handlers but
    not in the signature, so every call raised TypeError."""
    tree = _parse(REPO / "telegram-bot" / "call_llama.py")
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama"]
    assert funcs, "call_llama not found"
    args = funcs[0].args
    param_names = [a.arg for a in args.args] + [a.arg for a in args.kwonlyargs]
    assert "chat_type" in param_names, (
        f"call_llama must accept chat_type kwarg. "
        f"Current params: {param_names}"
    )


def test_call_llama_chat_type_is_fail_closed():
    """chat_type must not have a permissive default like 'private'.
    A new caller that forgets the kwarg must fail loudly, not
    silently enable bash_command in an arbitrary chat type.
    T53 #R4: fail-open defaults are how the next regression
    sneaks in."""
    tree = _parse(REPO / "telegram-bot" / "call_llama.py")
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama"]
    args = funcs[0].args
    all_defaults_map = {}
    n_regular = len(args.args)
    for i, d in enumerate(args.defaults):
        all_defaults_map[args.args[n_regular - len(args.defaults) + i].arg] = d
    for i, d in enumerate(args.kw_defaults):
        if d is not None:
            all_defaults_map[args.kwonlyargs[i].arg] = d
    if "chat_type" in all_defaults_map:
        d = all_defaults_map["chat_type"]
        if isinstance(d, ast.Constant):
            assert d.value is None, (
                f"call_llama(chat_type={d.value!r}) is a fail-OPEN default. "
                f"Use None as sentinel and raise TypeError if not provided."
            )


def test_call_llama_raises_without_chat_type():
    """The body must explicitly raise TypeError if chat_type is
    None. Belt-and-braces with the static check above: even if a
    future contributor removes the None default, the body still
    refuses to run without chat_type."""
    src = (REPO / "telegram-bot" / "call_llama.py").read_text()
    # Look for the chat_type None check and TypeError in the first
    # ~80 lines of the function body (between signature and the
    # first major work).
    fn_start = src.find("async def call_llama(")
    assert fn_start >= 0
    body_window = src[fn_start:fn_start + 6000]
    assert "chat_type is None" in body_window, (
        "call_llama body must check 'chat_type is None' and refuse. T53 #R3."
    )
    assert "TypeError" in body_window, (
        "call_llama must raise TypeError when chat_type is missing. T53 #R3."
    )


def test_every_handler_passes_chat_type():
    """Every await call_llama(...) invocation in handlers.py must
    pass chat_type=. This is the AST-level version of the grep
    that would have caught the T52 bug at commit time.

    T53 #R3: the T52 commit added chat_type= to all 4 call sites
    but forgot the signature. A future contributor might add a
    new handler and forget chat_type=. This test fails at the
    first such call site, with a precise line number.
    """
    tree = _parse(REPO / "telegram-bot" / "handlers.py")
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        call = node.value
        is_target = False
        if isinstance(call.func, ast.Name) and call.func.id in (
            "call_llama", "call_llama_compat",
        ):
            is_target = True
        if not is_target:
            continue
        has_chat_type = any(
            kw.arg == "chat_type" for kw in call.keywords
        )
        if not has_chat_type:
            offenders.append((node.lineno, ast.unparse(call)[:80]))
    assert not offenders, (
        "Every await call_llama(...) must pass chat_type=. "
        "Offending call sites:\n" +
        "\n".join(f"  line {ln}: {snippet}" for ln, snippet in offenders)
    )


def test_call_bash_command_chat_type_required():
    """T53 #R4: chat_type must be a required parameter on
    call_bash_command (no default). Otherwise a new caller
    that forgets the kwarg silently enables bash in any chat."""
    tree = _parse(REPO / "telegram-bot" / "personal_tools" / "bash_command.py")
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_bash_command"]
    assert funcs, "call_bash_command not found"
    args = funcs[0].args
    # Include both regular args and keyword-only args (after *).
    param_names = [a.arg for a in args.args] + [a.arg for a in args.kwonlyargs]
    assert "chat_type" in param_names, (
        f"call_bash_command must accept chat_type. Params: {param_names}"
    )
    defaults = args.defaults
    n_defaults = len(defaults)
    n_args = len(args.args)
    args_with_defaults = args.args[n_args - n_defaults:]
    for a in args_with_defaults:
        if a.arg == "chat_type":
            pytest.fail(
                "call_bash_command(chat_type=...) has a default value. "
                "T53 #R4: must be required so new callers fail-closed."
            )


def test_call_bash_command_refuses_non_private_chat():
    """The body must check chat_type and refuse anything that
    is not 'private'. This is the T52 #H1 chat-type gate."""
    src = (REPO / "telegram-bot" / "personal_tools" / "bash_command.py").read_text()
    assert 'chat_type != "private"' in src, (
        "call_bash_command must check chat_type != 'private' "
        "and return ok=False. T52 #H1."
    )


def test_dispatch_group_chat_hides_bash_command():
    """The /start text shown in group chats must mention that
    bash is unavailable. T52 #M4 user-facing copy."""
    src = (REPO / "telegram-bot" / "dispatch.py").read_text()
    assert "_is_group_chat" in src, "dispatch must check _is_group_chat"
    assert "Shell-команды (bash) недоступны" in src, (
        "dispatch /start must show 'bash unavailable' line in group chats. "
        "T52 #M4."
    )
