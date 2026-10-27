"""T61 #R8: every name in NATIVE_TOOL_NAMES has a dispatch site.

Wave-trap pattern (predicted): a future commit adds a
tool name to NATIVE_TOOL_NAMES (the bot-side whitelist
in tool_isolation.py) but forgets to add the dispatch
site in call_llama.py. The LLM will emit a tool_call
with the new name; validate_tool_calls accepts it (it's
in the whitelist); the dispatch block has no elif
fn_name == branch; the bot silently returns None.
The user sees a tool result that says nothing useful.

This tripwire asserts: every name in NATIVE_TOOL_NAMES
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
        r"NATIVE_TOOL_NAMES\s*:\s*frozenset\[str\]\s*=\s*frozenset\(\{(.*?)\}\)",
        src, re.DOTALL,
    )
    if not m:
        raise AssertionError(
            "Could not find NATIVE_TOOL_NAMES frozenset definition "
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
    # Block D4: the dispatch chain moved into llm_dispatch.py, where the
    # parameter is `name` rather than `fn_name`. The \b matters -- without
    # it the pattern would match the tail of `fn_name` too and could
    # double-count. Same property either way: a string compared against a
    # tool-name variable is a dispatch site.
    pattern_eq = re.compile(
        r"""\b(?:fn_)?name\s*==\s*['"]([a-zA-Z0-9_]+)['"]""")
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



# Every telegram-bot module that could own a tool registry. Kept as a
# glob rather than a list so a new module is picked up without editing
# this test -- the failure mode being guarded against is "the name got
# registered somewhere and nobody wired the dispatch".
TOOL_MODULES = sorted((CALL_LLAMA_PY.parent).glob("*.py"))


def _modules_with_tool_registries():
    """Modules that define an UPPER_CASE registry dict.

    call_llama is always included even if it no longer matches, so that
    a rename cannot quietly shrink the search to nothing and make the
    check pass for the wrong reason.
    """
    found = [CALL_LLAMA_PY]
    pattern = re.compile(r"^[A-Z_]{3,}\s*(?::\s*dict[^=]*)?=\s*\{", re.MULTILINE)
    for path in TOOL_MODULES:
        if path in found or path.name == "tool_isolation.py":
            continue
        if pattern.search(path.read_text()):
            found.append(path)
    return found

def test_every_known_tool_name_has_dispatch_site():
    """T61 #R8: every name in NATIVE_TOOL_NAMES must have at
    least one dispatch site in call_llama.py.
    """
    tool_isolation_src = TOOL_ISOLATION_PY.read_text()
    known_names = _extract_known_tool_names(tool_isolation_src)
    assert known_names, (
        "NATIVE_TOOL_NAMES is empty - the bot would have no tools. "
        "Either the file was refactored or this tripwire is broken."
    )

    # The dispatch sites live wherever the tool registry lives. Block B1
    # of TECH-DEBT #3 moved DONSETCH_TOOLS into donsetch.py; call_llama
    # imports it and dispatches through the imported dict, so reading
    # only call_llama.py found four tools "missing" that were in fact
    # dispatched.
    #
    # Followed via import rather than whitelisted: a whitelist of the four
    # names would leave the next tool added to the new module uncovered.
    dispatched_names = set()
    for module in _modules_with_tool_registries():
        dispatched_names |= _extract_dispatch_names(module.read_text())

    # Block D4 moved the dispatch chain itself into llm_dispatch.py.
    # Scanned through the shared location helper rather than by adding a
    # path here, so the next extraction is one edit in one place.
    from dispatch_location import iter_modules
    for _name, source in iter_modules():
        dispatched_names |= _extract_dispatch_names(source)

    missing = known_names - dispatched_names
    assert not missing, (
        f"NATIVE_TOOL_NAMES has tool names with no dispatch site: "
        f"{sorted(missing)}. Wave-trap pattern "
        f"predicted: a future commit adds a tool to NATIVE_TOOL_NAMES "
        f"but forgets the dispatch branch. The LLM will emit a "
        f"tool_call with this name; validate_tool_calls accepts it; "
        f"the dispatch block has no branch; the bot silently returns "
        f"None. Add a dispatch site (elif fn_name == 'name': ... or "
        f"include in DONSETCH_TOOLS / CUSTOM_TOOLS dict) or remove "
        f"the name from NATIVE_TOOL_NAMES."
    )
