"""T60 Tripwire #4: LLM-controlled session_id regression.

Wave-trap class: T57 #S16. The team added session_id format
validation (T52 #S3 regex [A-Za-z0-9._-]{1,128}) but the
session_id was still controlled by the LLM via
`args.get('session_id') or f'chat-{chat_id}'`. The LLM could
emit `session_id="chat-{other_chat_id}"` and forward to
another chat's persistent tmux session. Direct cross-chat
PII / secret leak.

This tripwire asserts:
  1. call_llama.py does NOT reference `args.get('session_id')`
     anywhere (the LLM's session_id must be ignored).
  2. call_llama.py has exactly one
     `sid = f'chat-{chat_id}'` (or `f"chat-{chat_id}"`) line,
     which is the canonical assignment.

If a future commit re-introduces `args.get('session_id')` or
removes the sid line, this test fails.
"""
from __future__ import annotations

import ast
import re

from test_drift_helpers import (
    CALL_LLAMA_PY,
    parse_source,
)


def test_no_args_get_session_id_in_call_llama():
    """T60 #4: call_llama.py must NOT reference
    `args.get('session_id')`. The LLM's session_id is
    untrusted; the bot owns the session_id as
    `f'chat-{chat_id}'`.
    """
    src = CALL_LLAMA_PY.read_text()
    # Match either single or double quotes
    patterns = [
        "args.get('session_id')",
        'args.get("session_id")',
    ]
    for pat in patterns:
        assert pat not in src, (
            f"call_llama.py contains '{pat}'. The LLM's "
            f"session_id is untrusted. T57 #S16 fix: always "
            f"use `sid = f'chat-{{chat_id}}'`, never pull "
            f"session_id from the LLM's tool args."
        )


def test_sid_is_chat_dash_chat_id():
    """T60 #4: call_llama.py must have exactly one
    assignment matching `sid = f'chat-{chat_id}'` (or
    double-quoted variant). This is the canonical session_id
    construction.
    """
    tree = parse_source(CALL_LLAMA_PY)
    # Find every assignment where target is `sid` and value
    # is a JoinedStr (f-string).
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == "sid":
                if isinstance(node.value, ast.JoinedStr):
                    # Reconstruct the f-string source
                    src_line = ast.unparse(node.value)
                    matches.append((node.lineno, src_line))
    # Filter to only the `chat-{chat_id}` form
    canonical = [
        (lineno, src)
        for lineno, src in matches
        if "chat-" in src and "chat_id" in src
    ]
    assert len(canonical) == 1, (
        f"call_llama.py must have exactly one "
        f"`sid = f'chat-{{chat_id}}'` assignment. "
        f"Found {len(canonical)}: {canonical}. Wave-5 #S16 "
        f"fix: the LLM cannot choose the session_id; the "
        f"bot owns it via chat_id."
    )
    # And the comment must still say "never trust the LLM"
    lineno, _ = canonical[0]
    src = CALL_LLAMA_PY.read_text()
    # Look at the same line / next line for the S16 comment
    lines = src.splitlines()
    if lineno - 1 < len(lines):
        line = lines[lineno - 1]
        if "S16" not in line and (
            lineno < len(lines) and "S16" not in lines[lineno]
        ):
            assert False, (
                f"call_llama.py:{lineno}: sid = f'chat-{{chat_id}}' "
                f"is missing the S16 comment marker. Add "
                f"`# T57 #S16: never trust the LLM's session_id` "
                f"so a future contributor does not 'improve' it."
            )
