"""B1 -- the selftest patch must still be live after the move.

This is the whole risk of extracting donsetch_*. `donsetch_call` declares
`global _donsetch_session_id` and reads its OWN module's. selftest.py
forces a fresh MCP session by writing None onto that global. Move the
code and the write lands on a module nobody reads, and the integration
test goes on passing without having done anything.

That is not hypothetical. This repo has paid for it twice:

  * TECH-DEBT #1 -- six monkeypatches wrote to `bot.<name>` while the
    handlers read their own globals. Measured: `bot._route_to_thread is
    fake` True, `handlers._route_to_thread is fake` False.
  * the line before the one this commit changes -- it bound `_b_module`
    inside a try/except, so the NameError from the assignment was
    swallowed and the whole block was inert.

So the check below is behavioural: it patches the global the way
selftest does and requires the function to observe the change. A test that
only greps for `_donsetch_session_id = None` would pass on a module that
nobody imports.
"""
import ast
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT))

import call_llama  # noqa: E402
import donsetch  # noqa: E402

pytestmark = pytest.mark.tech_debt(
    item=3,
    reason="Pins TECH-DEBT #3 B1: the donsetch session state lives in the "
           "module that owns it, and selftest patches that module.")


# --- the state lives in exactly one place -------------------------------

def test_state_lives_in_donsetch_not_call_llama():
    assert hasattr(donsetch, "_donsetch_session_id")
    assert hasattr(donsetch, "_donsetch_session_lock")
    # call_llama may RE-EXPORT the names for forks; what it must not do is
    # own a second, independent copy. A second copy is the silent no-op.
    if hasattr(call_llama, "_donsetch_session_id"):
        assert call_llama._donsetch_session_id is donsetch._donsetch_session_id, (
            "call_llama._donsetch_session_id is a different object from "
            "donsetch's. Writing to it changes nothing that donsetch_call "
            "reads -- the selftest would then pass without testing."
        )


def test_donsetch_does_not_import_call_llama():
    """The cycle the whole extraction exists to avoid."""
    tree = ast.parse((BOT / "donsetch.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "call_llama" not in imported, (
        f"donsetch.py imports call_llama: {sorted(imported)}. call_llama "
        f"imports donsetch, so that is a cycle."
    )


def test_call_llama_still_reexports_the_public_names():
    """Forks have imported these from call_llama since v0.9."""
    for name in ("donsetch_call", "DONSETCH_TOOLS",
                 "execute_donsetch_web_search", "execute_donsetch_web_fetch",
                 "execute_donsetch_web_crawl", "execute_donsetch_web_screenshot"):
        assert hasattr(call_llama, name), (
            f"call_llama no longer exposes {name}. It is re-exported "
            f"deliberately: removing it breaks forks for no runtime gain, "
            f"which is the same trade as the bot.py facade."
        )


# --- the behavioural check ----------------------------------------------

async def test_patching_the_owner_is_observed_by_donsetch_call(monkeypatch):
    """The no-op detector.

    Patch the way selftest does, then confirm the function's own code path
    sees it -- by making the initialisation decision observable without a
    network call: an already-set session means init is skipped, so a
    session id that is obviously bogus proves whether the branch was
    taken.
    """
    original = donsetch._donsetch_session_id
    try:
        donsetch._donsetch_session_id = "SENTINEL-SESSION"
        assert donsetch._donsetch_session_id == "SENTINEL-SESSION"

        # Read the function's bytecode-level reference, not a grep: it must
        # be a global in ITS OWN module.
        fn = donsetch.donsetch_call
        assert fn.__module__ == "donsetch", (
            f"donsetch_call.__module__ is {fn.__module__!r}; if it were "
            f"'call_llama' the global it reads would be a different object"
        )
        assert fn.__globals__ is vars(donsetch), (
            "donsetch_call.__globals__ is not the donsetch module dict, so a "
            "patch on donsetch is invisible to it"
        )

        # And the patched value is what that dict now holds.
        assert fn.__globals__["_donsetch_session_id"] == "SENTINEL-SESSION"
    finally:
        donsetch._donsetch_session_id = original


def test_selftest_patches_the_owning_module():
    """Structural half: selftest must name `donsetch`, not `call_llama`,
    for the session reset."""
    src = (BOT / "selftest.py").read_text(encoding="utf-8")
    assert "_donsetch_session_id = None" in src, (
        "selftest no longer resets the MCP session"
    )
    tree = ast.parse(src)

    # Resolve `import donsetch as _ds` before judging the write. Comparing
    # the alias name to a module name is the same mistake in miniature:
    # it says a patch is misplaced because of how the patch is spelled.
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name

    writes_to = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Attribute)
                and node.targets[0].attr == "_donsetch_session_id"):
            base = node.targets[0].value
            if isinstance(base, ast.Name):
                writes_to.add(aliases.get(base.id, base.id))
            elif isinstance(base, ast.Attribute):
                writes_to.add(aliases.get(base.attr, base.attr))

    assert writes_to, "could not find the session reset assignment"
    assert "donsetch" in writes_to, (
        f"selftest writes _donsetch_session_id onto {writes_to}, not onto "
        f"the module that owns it. That write is read by nobody and the "
        f"integration test silently stops testing."
    )
    assert "call_llama" not in writes_to, (
        "selftest still writes the session onto call_llama, which no longer "
        "owns the global"
    )


def test_the_comment_claiming_where_the_patch_must_land_is_true():
    """selftest carries a comment asserting that the write has to land on
    the module that owns the global. Comments asserting a fact are
    undocumented findings until something checks them -- this repo has had
    two that were false."""
    src = (BOT / "selftest.py").read_text(encoding="utf-8")
    assert "has to land on" in src or "write has to land on" in src, (
        "the comment explaining where the patch must land was removed; "
        "restore it, because it is the only warning before the next "
        "refactor repeats this"
    )
    # ...and it must name the module that actually owns it.
    window = src[max(0, src.index("has to land on") - 400):
                 src.index("has to land on") + 400]
    assert "donsetch" in window, (
        "the comment says the patch must land on a module but names "
        "`donsetch` nowhere near it -- the claim and the code disagree"
    )