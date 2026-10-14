"""T61 #R8: every name in KNOWN_TOOL_NAMES has a dispatch site.

Wave-trap pattern (predicted): a future commit adds a
tool name to KNOWN_TOOL_NAMES (the bot-side whitelist
in tool_isolation.py) but forgets to add the dispatch
site in call_llama.py. The LLM will emit a tool_call
with the new name; validate_tool_calls accepts it (it's
in the whitelist); the dispatch block has no elif
fn_name == branch; the bot silently returns None.
The user sees a tool result that says nothing useful.

This tripwire asserts: every name in KNOWN_TOOL_NAMES
appears in at least one dispatch site in call_llama.py.
"""
from __future__ import annotations

import re
import pathlib


# Source paths
TOOL_ISOLATION_PY = (
    pathlib.Path(__file__).parent.parent
    / "telegram-bot"
    / "tool_isolation.py"
)
CALL_LLAMA_PY = (
    pathlib.Path(__file__).parent.parent
    / "telegram-bot"
    / "call_llama.py"
)


def _extract_known_tool_names(src: str) -> set:
    m = re.search(
        r"KNOWN_TOOL_NAMES\s*:\s*frozenset\[str\]\s*=\s*frozenset\(\{(.*?)\}\)",
        src, re.DOTALL,
    )
    if not m:
        raise AssertionError(
            "Could not find KNOWN_TOOL_NAMES frozenset definition "
            "in tool_isolation.py. Tripwire is broken or the file "
            "has been heavily refactored. Update this tripwire."
        )
    body = m.group(1)
    names: set = set()
    pattern = re.compile(r"""['"]([a-zA-Z0-9_]+)['"]""")
    for sm in pattern.finditer(body):
        names.add(sm.group(1))
    return names


def _extract_dispatch_names(src: str) -> set:
    names: set = set()
    pattern_eq = re.compile(r"""fn_name\s*==\s*['"]([a-zA-Z0-9_]+)['"]""")
    for m in pattern_eq.finditer(src):
        names.add(m.group(1))
    pattern_dict = re.compile(
        r"^[A-Z_]+\s*(?::\s*dict|=\s*\{)([^{}]*)\}",
        re.MULTILINE,
    )
    pattern_str = re.compile(r"""['"]([a-zA-Z0-9_]+)['"]\s*:""")
    for m in pattern_dict.finditer(src):
        body = m.group(1)
        for sm in pattern_str.finditer(body):
            names.add(sm.group(1))
    return names


def test_every_known_tool_name_has_dispatch_site():
    """T61 #R8: every name in KNOWN_TOOL_NAMES must have at
    least one dispatch site in call_llama.py.
    """
    tool_isolation_src = TOOL_ISOLATION_PY.read_text()
    known_names = _extract_known_tool_names(tool_isolation_src)
    assert known_names, (
        "KNOWN_TOOL_NAMES is empty - the bot would have no tools. "
        "Either the file was refactored or this tripwire is broken."
    )

    call_llama_src = CALL_LLAMA_PY.read_text()
    dispatched_names = _extract_dispatch_names(call_llama_src)

    missing = known_names - dispatched_names
    assert not missing, (
        f"KNOWN_TOOL_NAMES has tool names with no dispatch site "
        f"in call_llama.py: {sorted(missing)}. Wave-trap pattern "
        f"predicted: a future commit adds a tool to KNOWN_TOOL_NAMES "
        f"but forgets the dispatch branch. The LLM will emit a "
        f"tool_call with this name; validate_tool_calls accepts it; "
        f"the dispatch block has no branch; the bot silently returns "
        f"None. Add a dispatch site (elif fn_name == 'name': ... or "
        f"include in DONSETCH_TOOLS / CUSTOM_TOOLS dict) or remove "
        f"the name from KNOWN_TOOL_NAMES."
    )
