"""T60 Tripwire #2: BASH_COMMAND_TOOL_DEF divergence.

Wave-trap class: T52 #M5. The team claimed "BASH_COMMAND_TOOL_DEF
deduped" but actually left two divergent copies: one in
call_llama.py and one in personal_tools/bash_command.py. Caught
in wave-5 as #D7.

This tripwire asserts:
  1. call_llama.py has NO top-level definition of
     BASH_COMMAND_TOOL_DEF (the canonical one lives in
     personal_tools/bash_command.py).
  2. call_llama.py has exactly one import of
     BASH_COMMAND_TOOL_DEF from personal_tools.bash_command.
  3. The dict in personal_tools/bash_command.py is the
     SAME dict reference that call_llama.py imports
     (verified at parse time, not runtime — but a name
     clash would still trip test #1).

If a future commit re-defines BASH_COMMAND_TOOL_DEF in
call_llama.py, this test fails.
"""
from __future__ import annotations

import ast

from test_drift_helpers import (
    BASH_COMMAND_PY,
    CALL_LLAMA_PY,
    TELEGRAM_BOT_DIR,
    module_level_names,
    parse_source,
)


def test_call_llama_does_not_define_bash_command_tool_def():
    """T60 #2: call_llama.py must NOT have a top-level
    FunctionDef / Assign / AnnAssign named
    BASH_COMMAND_TOOL_DEF. The canonical definition lives
    in personal_tools/bash_command.py.
    """
    tree = parse_source(CALL_LLAMA_PY)
    bad: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "BASH_COMMAND_TOOL_DEF":
            bad.append(("FunctionDef", node.lineno))
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "BASH_COMMAND_TOOL_DEF":
                    bad.append(("Assign", node.lineno))
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "BASH_COMMAND_TOOL_DEF":
                bad.append(("AnnAssign", node.lineno))
    assert not bad, (
        f"call_llama.py contains a top-level definition of "
        f"BASH_COMMAND_TOOL_DEF at {bad}. The canonical "
        f"definition must live in personal_tools/bash_command.py "
        f"only. This is the wave-1 #M5 / wave-5 #D7 wave-trap "
        f"class (claimed 'deduped' but left a duplicate)."
    )


def test_the_tool_list_owner_imports_bash_command_tool_def_exactly_once():
    """T60 #2: exactly one module may import the canonical
    `BASH_COMMAND_TOOL_DEF` from personal_tools.bash_command.

    The property is about the SYMBOL, not about a file: the tool list
    moved from call_llama.py to llm_tools.py on 2026-10-27, and a
    tripwire bound to the old file would have gone red for a move
    that changed nothing. So the whole telegram-bot tree is scanned:
    one importer means one owner, and no module may answer for
    another.

    Multiple imports indicate divergence risk; zero imports indicate
    the tool is not wired up.
    """
    matches: list[str] = []
    for path in sorted(TELEGRAM_BOT_DIR.rglob("*.py")):
        tree = parse_source(path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "personal_tools.bash_command"
            ):
                for alias in node.names:
                    if alias.name == "BASH_COMMAND_TOOL_DEF" or alias.asname == "BASH_COMMAND_TOOL_DEF":
                        matches.append(f"{path.name}:{node.lineno}")
    assert len(matches) == 1, (
        f"exactly one module must import BASH_COMMAND_TOOL_DEF from "
        f"personal_tools.bash_command. Found {len(matches)}: "
        f"{matches}. Wave-1 #M5 / wave-5 #D7 wave-trap class: too few "
        f"imports means the tool isn't wired up; too many means "
        f"divergence risk."
    )


def test_bash_command_module_owns_the_definition():
    """T60 #2: the BASH_COMMAND_TOOL_DEF constant must be
    defined at module level in personal_tools/bash_command.py.
    This is the single source of truth.
    """
    tree = parse_source(BASH_COMMAND_PY)
    found = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "BASH_COMMAND_TOOL_DEF":
                    found = True
                    break
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "BASH_COMMAND_TOOL_DEF":
                found = True
                break
    assert found, (
        "personal_tools/bash_command.py must define "
        "BASH_COMMAND_TOOL_DEF at module level. This is the "
        "single source of truth for the tool def. If you are "
        "moving it, update call_llama.py's import, do not "
        "duplicate."
    )
