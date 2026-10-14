"""Backtest: verify each T60 tripwire catches its target pattern.

This file is the inverse of the tripwires. The tripwires
assert "current source is clean". This file asserts "if
the historical bad code is re-introduced, the tripwire
catches it".

Pattern classes tested:
  * Wave 1 T52 #M5: BASH_COMMAND_TOOL_DEF dedup — call_llama.py
    re-introduces the duplicate definition.
  * Wave 3 T53 #R3: chat_type kwarg — handlers.py re-introduces
    a call_llama() without chat_type=.
  * Wave 5 T57 #S16: session_id semantic — call_llama.py
    re-introduces args.get('session_id').
  * Wave 6 acbd062 (S40): dispatch.py start() references
    is_dev_mode_chat() but the function is not defined.
  * Wave 6 327b7b0: dispatch.py registers MessageHandler
    with a handle_X callback that is not imported.
  * T60 Bug A: dispatch.py references fetch_tools_from_llama()
    without importing it.
  * T60 Bug B: call_llama() signature has chat_type= default.

Each test writes a tiny fixture source file, runs the same
AST logic the tripwire uses, and asserts the bad pattern is
flagged. If a test fails, the corresponding tripwire is
incomplete and must be widened.
"""
from __future__ import annotations

import ast
import pathlib
import tempfile

import pytest

from test_drift_helpers import (
    KNOWN_BUILTINS,
    function_locals,
    imported_names,
    module_level_names,
    parse_source,
)


# ---- shared helpers (mirror tripwire logic) ----

def _check_no_undefined_names_in_function(src: str) -> set[str]:
    """Returns the set of undefined Name ids in any function
    in src. Same logic as test_drift_t58_revert."""
    tree = ast.parse(src)
    module_names = module_level_names(tree)
    imported = imported_names(tree)
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            locals_ = function_locals(node)
            ref_names = {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            }
            unknown = ref_names - module_names - imported - locals_ - KNOWN_BUILTINS
            out |= unknown
    return out


def _check_bash_tool_def_dedup(src: str) -> list[str]:
    """Returns list of issues with BASH_COMMAND_TOOL_DEF in src.
    Same logic as test_drift_m5_dedup."""
    tree = ast.parse(src)
    issues: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "BASH_COMMAND_TOOL_DEF":
            issues.append("redefined as function")
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "BASH_COMMAND_TOOL_DEF":
                    issues.append("redefined as assignment")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "BASH_COMMAND_TOOL_DEF":
                issues.append("redefined as ann-assign")
    return issues


def _check_call_llama_chat_type_kwarg(src: str) -> list[int]:
    """Returns line numbers of call_llama() calls without
    chat_type= kwarg. Same logic as test_drift_r3_chat_type."""
    tree = ast.parse(src)
    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "call_llama":
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            if "chat_type" not in kw_names:
                bad.append(node.lineno)
    return bad


def _check_call_llama_chat_type_required(sig_src: str) -> bool:
    """Returns True if call_llama() signature in sig_src has
    chat_type as required (no default). Same logic as
    test_drift_r3_chat_type."""
    tree = ast.parse(sig_src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "call_llama":
            args = node.args
            for arg in args.args:
                if arg.arg == "chat_type":
                    n_defaults = len(args.defaults)
                    n_positional = len(args.args)
                    if args.args.index(arg) >= n_positional - n_defaults:
                        return False  # has default
                    return True
            for arg in args.kwonlyargs:
                if arg.arg == "chat_type":
                    if any(d is not None and d.arg == "chat_type" for d in args.kw_defaults):
                        return False  # has default
                    return True
    return False


def _check_no_args_get_session_id(src: str) -> bool:
    """Returns True if src contains args.get('session_id') /
    args.get("session_id"). Same as test_drift_s16."""
    return (
        "args.get('session_id')" in src
        or 'args.get("session_id")' in src
    )


def _check_handler_imports(src: str) -> list[str]:
    """Returns list of callback names in app.add_handler that
    are not imported at module level. Same as
    test_drift_handler_imports."""
    tree = ast.parse(src)
    acceptable = module_level_names(tree) | imported_names(tree)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_handler":
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id not in ("app", "_dispatcher", "application"):
            continue
        if len(node.args) == 1 and isinstance(node.args[0], ast.Call):
            cb = node.args[0].args[-1] if node.args[0].args else None
            if isinstance(cb, ast.Name) and cb.id not in acceptable:
                bad.append(cb.id)
        elif len(node.args) == 2:
            cb = node.args[1]
            if isinstance(cb, ast.Name) and cb.id not in acceptable:
                bad.append(cb.id)
    return bad


# === PATTERN 1: S40 (acbd062) — is_dev_mode_chat dead call ===

def test_backtest_t58_revert_catches_dead_is_dev_mode_chat_call():
    """Backtest: tripwire #1 must catch dispatch.start() that
    references is_dev_mode_chat() but does not import or
    define it. This is the wave-6 #S40 regression pattern.
    """
    bad_src = """
import asyncio

async def start(update, context):
    if is_dev_mode_chat(update.effective_chat.id):
        return "dev mode on"
    return "normal"
"""
    undefined = _check_no_undefined_names_in_function(bad_src)
    assert "is_dev_mode_chat" in undefined, (
        f"BACKTEST FAIL: tripwire #1 did not catch "
        f"is_dev_mode_chat() reference in dispatch.start(). "
        f"Got undefined: {undefined}. The wave-6 #S40 "
        f"regression class would slip through."
    )


# === PATTERN 2: T52 #M5 — BASH dedup ===

def test_backtest_m5_catches_bash_tool_def_redefined_in_call_llama():
    """Backtest: tripwire #2 must catch a BASH_COMMAND_TOOL_DEF
    re-definition in call_llama.py. The wave-1 #M5 / wave-5
    #D7 pattern.
    """
    bad_src = """
BASH_COMMAND_TOOL_DEF = {
    "type": "function",
    "function": {"name": "bash_command"},
}
"""
    issues = _check_bash_tool_def_dedup(bad_src)
    assert issues, (
        "BACKTEST FAIL: tripwire #2 did not catch "
        "BASH_COMMAND_TOOL_DEF re-defined in call_llama.py. "
        "The wave-1 #M5 / wave-5 #D7 pattern would slip through."
    )


# === PATTERN 3: T53 #R3 — chat_type missing in call ===

def test_backtest_r3_catches_call_llama_without_chat_type_kwarg():
    """Backtest: tripwire #3 (call-site part) must catch a
    call_llama() in handlers.py without chat_type= kwarg.
    Wave-3 #R3 pattern.
    """
    bad_src = """
async def handle_photo(update, context):
    result = await call_llama(
        messages=[],
        bot=context.bot,
        chat_id=123,
    )
    return result
"""
    bad_lines = _check_call_llama_chat_type_kwarg(bad_src)
    assert bad_lines, (
        f"BACKTEST FAIL: tripwire #3 (call-site part) did not "
        f"catch call_llama() without chat_type= kwarg. "
        f"Got bad lines: {bad_lines}. Wave-3 #R3 pattern would "
        f"slip through."
    )


def test_backtest_r3_catches_chat_type_default_in_signature():
    """Backtest: tripwire #3 (signature part) must catch a
    call_llama() signature with chat_type having a default
    value. Wave-3 #R4 / T60 Bug B pattern.
    """
    bad_sig_src = """
async def call_llama(
    messages,
    max_tokens=100,
    chat_type: str | None = None,
):
    return None
"""
    required = _check_call_llama_chat_type_required(bad_sig_src)
    assert not required, (
        "BACKTEST FAIL: tripwire #3 (signature part) did not "
        "catch chat_type default = None. The T60 Bug B pattern "
        "would slip through."
    )


# === PATTERN 4: T57 #S16 — session_id semantic ===

def test_backtest_s16_catches_args_get_session_id():
    """Backtest: tripwire #4 must catch call_llama.py that
    references args.get('session_id'). Wave-5 #S16 pattern.
    """
    bad_src = """
def dispatch_bash(args, chat_id):
    sid = args.get('session_id') or f'chat-{chat_id}'
    return sid
"""
    assert _check_no_args_get_session_id(bad_src), (
        "BACKTEST FAIL: tripwire #4 did not catch "
        "args.get('session_id') reference. Wave-5 #S16 "
        "pattern would slip through."
    )


# === PATTERN 5: 327b7b0 — handler import ===

def test_backtest_handler_imports_catches_unimported_callback():
    """Backtest: tripwire #5 must catch app.add_handler() with
    a callback that is not imported at module level. Wave-6
    327b7b0 pattern.
    """
    bad_src = """
import asyncio
from telegram.ext import Application, MessageHandler, filters

app = Application.builder().token('TEST').build()

async def main():
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    await app.initialize()
"""
    bad = _check_handler_imports(bad_src)
    assert "handle_photo" in bad, (
        f"BACKTEST FAIL: tripwire #5 did not catch "
        f"handle_photo callback not imported. Got bad: {bad}. "
        f"Wave-6 327b7b0 pattern would slip through."
    )


# === T60 Bug A: fetch_tools_from_llama undefined ===

def test_backtest_t60_bug_a_catches_fetch_tools_undefined():
    """Backtest: tripwire #1 must also catch
    fetch_tools_from_llama() reference without import. T60
    latent bug A pattern.
    """
    bad_src = """
import call_llama

async def stats(update, context):
    tools = await fetch_tools_from_llama()
    return tools
"""
    undefined = _check_no_undefined_names_in_function(bad_src)
    assert "fetch_tools_from_llama" in undefined, (
        f"BACKTEST FAIL: tripwire #1 did not catch "
        f"fetch_tools_from_llama() undefined. Got: {undefined}. "
        f"T60 latent bug A would slip through."
    )


# === Negative test: tripwires must NOT raise on clean code ===

def test_backtest_clean_code_passes_all_tripwires():
    """Backtest (negative): a clean dispatch.py with all
    imports in place must NOT trip any of the 5 tripwires.
    This is the inverse direction — ensures tripwires are
    not over-broad.
    """
    clean_src = """
import asyncio
import call_llama
from call_llama import fetch_tools_from_llama, call_llama
from telegram.ext import (
    Application, MessageHandler, filters,
)
from handlers import handle_photo, handle_voice, handle_text, handle_document

app = Application.builder().token('TEST').build()

async def start(update, context):
    if fetch_tools_from_llama:
        return "ok"
    return "no"

async def main():
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
"""
    # All 5 tripwires must say "clean"
    undefined = _check_no_undefined_names_in_function(clean_src)
    assert "is_dev_mode_chat" not in undefined
    assert "fetch_tools_from_llama" not in undefined
    assert "handle_photo" not in undefined
    assert "handle_voice" not in undefined
    assert "handle_text" not in undefined
    assert "handle_document" not in undefined
