"""B2 -- the extracted modules must be importable on their own terms.

py_compile passed on every one of them while http_clients.py was missing
`import asyncio` -- compiling checks syntax, not names, and _abortable_post
uses asyncio.wait_for deep in its body. The suite caught it in two tests
and nothing else did.

So: import each extracted module, then reach for every name it is
responsible for. A module that imports cleanly but cannot resolve its own
globals is the failure mode worth pinning, because it is invisible to
every tool that does not execute the body.
"""
import ast  # noqa: E402
from loop_owner import loop_tree  # noqa: E402
import importlib
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
sys.path.insert(0, str(BOT))

pytestmark = pytest.mark.tech_debt(
    item=3,
    reason="Pins TECH-DEBT #3 B2: the extracted leaf modules import and "
           "resolve their own globals.")

EXTRACTED = {
    "llm_messages": ["_tag_sender"],
    "llm_tools": ["fetch_tools_from_llama", "_bash_command_tool_enabled"],
    "http_clients": ["_abortable_post", "transcribe_voice"],
    "donsetch": ["donsetch_call", "_donsetch_init", "DONSETCH_TOOLS"],
}

# The fork surface: names call_llama must keep exposing. Everything else
# in EXTRACTED lives only in its owner -- deliberately, so that the second
# binding cannot be created and then patched.
REEXPORTED = {
    "llm_messages": ["_tag_sender"],
    "llm_tools": ["fetch_tools_from_llama", "_bash_command_tool_enabled"],
    "http_clients": ["_abortable_post", "transcribe_voice"],
    "donsetch": ["donsetch_call", "DONSETCH_TOOLS",
                 "execute_donsetch_web_search", "execute_donsetch_web_fetch",
                 "execute_donsetch_web_crawl", "execute_donsetch_web_screenshot"],
}

# State and internals that must live in exactly one module and must NOT be
# re-exported.
# Re-exporting a module-level cache or a session id is how the donsetch
# no-op happened: a second binding looks like the original and gets
# patched, and nothing reads the patch.
PRIVATE_STATE = {
    "llm_tools": ["_TOOLS_CACHE"],
    "donsetch": ["_donsetch_session_id", "_donsetch_session_lock"],
}


@pytest.mark.parametrize("module_name,names",
                         sorted((m, n) for m, n in EXTRACTED.items()))
def test_extracted_module_imports_and_owns_its_names(module_name, names):
    mod = importlib.import_module(module_name)
    for name in names:
        assert hasattr(mod, name), (
            f"{module_name} imported cleanly but has no {name}. "
            f"py_compile cannot see this -- it checks syntax, not names."
        )


@pytest.mark.parametrize("module_name,names",
                         sorted((m, n) for m, n in EXTRACTED.items()))
def test_extracted_module_resolves_its_own_globals(module_name, names):
    """The header I actually got wrong.

    http_clients.py declared httpx and config but not asyncio, and
    _abortable_post calls asyncio.wait_for in its body -- so the module
    imported, compiled, and only failed when a test executed the path.
    Every name a module's functions reference has to be in its globals.
    """
    import ast

    mod = importlib.import_module(module_name)
    tree = ast.parse((BOT / f"{module_name}.py").read_text(encoding="utf-8"))

    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            referenced.add(node.id)

    declared = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                declared.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            declared.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            declared.add(node.id)
        elif isinstance(node, ast.arg):
            declared.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            declared.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            pass

    builtin = set(dir(__builtins__)) if not isinstance(__builtins__, dict) \
        else set(__builtins__)
    missing = sorted(n for n in referenced - declared - builtin
                     if not hasattr(mod, n))
    assert not missing, (
        f"{module_name} references {missing} but does not import or define "
        f"them. It compiles and imports; it fails only when that code path "
        f"runs."
    )


def test_call_llama_reexports_every_extracted_name():
    """Fork surface. dispatch.py and handlers.py import from call_llama, and
    tests/test_t39_integration.py calls call_llama._bash_command_tool_enabled.
    """
    import call_llama

    for module_name, names in REEXPORTED.items():
        for name in names:
            assert hasattr(call_llama, name), (
                f"call_llama no longer exposes {name} from {module_name}. "
                f"dispatch.py, handlers.py and the tests import through this "
                f"module."
            )


@pytest.mark.parametrize("module_name,names",
                         sorted((m, n) for m, n in EXTRACTED.items()))
def test_owner_is_the_only_place_a_non_reexported_name_appears(module_name, names):
    """Everything extracted must be reachable from exactly one module.

    A name that exists nowhere is a silent removal; a name that exists in
    two is a stale copy waiting to be patched. Both are failures, and only
    listing the names we expect catches the first one.
    """
    import call_llama

    mod = importlib.import_module(module_name)
    for name in names:
        assert hasattr(mod, name), (
            f"{name} was extracted into {module_name} but is not there"
        )
        if name in REEXPORTED.get(module_name, []):
            continue
        assert not hasattr(call_llama, name), (
            f"{name} is exposed by call_llama as well as {module_name}"
        )


def test_ensure_model_and_discover_stayed_behind():
    """The deliberate non-move, pinned so it cannot drift by accident.

    _ensure_model rebinds `global MODEL`. call_llama reads that same global
    when it builds the request body at :691 and :1418. If the function ever
    moves without those reads moving with it, every request carries
    "model": "" and the engine rejects it -- in production, silently.
    """
    import ast

    import call_llama

    tree = ast.parse((BOT / "call_llama.py").read_text(encoding="utf-8"))
    names = {n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert "_ensure_model" in names, (
        "_ensure_model left call_llama. That is allowed ONLY if the two "
        'read sites ("model": MODEL) moved with it. Check that before '
        "moving anything."
    )
    assert "_discover_default_model" in names, (
        "_discover_default_model left call_llama; see _ensure_model."
    )
    assert hasattr(call_llama, "_ensure_model")

    # The property, not the shape it used to have -- twice over.

    # v1 counted `"model": MODEL` dict literals and asserted >= 2. Moving
    # the builder into llm_request.py turned one of them into a keyword
    # argument, the count fell to 1, and the test went red on correct
    # code.
    #
    # v2 counted MODEL loads anywhere in the file. That went green, and
    # the mutation `model=MODEL` -> `model=""` SURVIVED it, because
    # _ensure_model reads the global twice on its own. A count is not a
    # property. This version resolves the call site instead: the keyword
    # argument handed to build_request_body must be the global itself,
    # not a literal and not a copy taken earlier.
    fn_tree = loop_tree()
    call_sites = [n for n in ast.walk(fn_tree)
                  if isinstance(n, ast.Call)
                  and getattr(n.func, "id", None) == "build_request_body"]
    assert call_sites, "build_request_body is never called from the loop"

    bad = []
    for call in call_sites:
        model_arg = next((kw.value for kw in call.keywords
                          if kw.arg == "model"), None)
        if not (isinstance(model_arg, ast.Name)
                and model_arg.id == "MODEL"
                and isinstance(model_arg.ctx, ast.Load)):
            bad.append(f"line {call.lineno}: model="
                       f"{ast.unparse(model_arg) if model_arg else '<absent>'}")
    assert not bad, (
        "the model passed to build_request_body must be the rebindable "
        "MODEL global, read at the call site. Found: " + "; ".join(bad)
        + ". _ensure_model rebinds the global; a literal or an earlier "
          "copy leaves every request carrying the old value, silently, in "
          "production.")

    # The owner must not have captured its own copy. If llm_request
    # imported MODEL, a rebind would leave it posting the old value and
    # the read count above would look healthy while the bug is live.
    req = ast.parse((BOT / "llm_request.py").read_text(encoding="utf-8"))
    assert not any(
        isinstance(n, (ast.Import, ast.ImportFrom)) and "MODEL" in ast.unparse(n)
        for n in req.body), (
        "llm_request must take `model` as a parameter. Importing MODEL "
        "there gives _ensure_model's rebind nothing to propagate to."
    )
    assert not any(
        isinstance(n, ast.Name) and n.id == "MODEL"
        for n in ast.walk(req)), "llm_request reads MODEL directly"

    builder = next(n for n in req.body
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "build_request_body")
    assert [a.arg for a in builder.args.args][:1] == ["model"], \
        "build_request_body must take `model` as its first parameter"


@pytest.mark.parametrize("module_name,names",
                         sorted((m, n) for m, n in PRIVATE_STATE.items()))
def test_private_state_is_not_reexported(module_name, names):
    """The negative half of B1's rule.

    call_llama._donsetch_session_id is gone on purpose. Keeping a second
    binding would look like the original and invite exactly the patch that
    reads nothing -- which is what the selftest used to do, silently, for
    two consecutive refactors.
    """
    import call_llama

    mod = importlib.import_module(module_name)
    for name in names:
        assert hasattr(mod, name), (
            f"{module_name} must own {name}; the state moved with the code"
        )
        assert not hasattr(call_llama, name), (
            f"call_llama.{name} exists again. It is a second, independent "
            f"binding to state owned by {module_name} -- writing to it "
            f"changes nothing that {module_name} reads."
        )


def test_config_still_owns_model():
    """The other half of the invariant: MODEL is defined in config and
    imported by call_llama, which is what makes the rebind local."""
    cfg = (BOT / "config.py").read_text(encoding="utf-8")
    assert "\nMODEL = " in cfg or "MODEL=" in cfg, (
        "config no longer defines MODEL. _ensure_model's `global MODEL` "
        "rebinds call_llama's imported binding, not config's."
    )