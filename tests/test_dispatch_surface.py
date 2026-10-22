"""dispatch.py must re-export is_authorized, never redefine it.

Brief T3.2(a)#2.

`access.py` owns `is_authorized`. `dispatch.py` imports it at line 38
and, until this commit, also defined its own copy at line 85 -- a
verbatim duplicate, AST-identical to the original, and dead: after the
definition, the name appears nowhere else in dispatch.py.

It was still worth removing, because
`tests/test_drift_t63_env_names.py` justified its own tripwire with a
comment claiming that `dispatch_mod.is_authorized` "rebinds only a
re-exported name". On this tree that was false -- it was a separate
function object with its own body, so a monkeypatch on the dispatch
attribute would have been silently effective rather than inert, the
exact opposite of what the comment warned a future editor about. A
tripwire guarding a real hazard with a wrong stated reason is one
refactor away from someone "fixing" the code to match the comment.

The two bodies were compared AST-to-AST before the duplicate was
removed; that comparison lives in fix_dispatch_authz.py, which asserts
it before it deletes anything. Here the property is pinned instead.

The identity check is the load-bearing one: it is what makes the
comment in test_drift_t63_env_names.py true, and it fails if anyone
reintroduces a second definition without touching the AST checks.
"""
import ast
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

DISPATCH = ROOT / "telegram-bot" / "dispatch.py"
ACCESS = ROOT / "telegram-bot" / "access.py"


def _defs(path, name):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    return [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name]


def test_dispatch_does_not_define_is_authorized():
    assert not _defs(DISPATCH, "is_authorized"), (
        "dispatch.py defines its own is_authorized, shadowing the "
        "re-export from access.py")


def test_is_authorized_is_still_re_exported():
    tree = ast.parse(DISPATCH.read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.ImportFrom)
               and any(al.name == "is_authorized" for al in n.names)
               for n in tree.body), (
        "dispatch.py no longer imports is_authorized -- removing the "
        "local definition would have left the name unbound")
    assert _defs(ACCESS, "is_authorized"), \
        "access.py no longer defines it either"


def test_the_name_is_one_object_not_two():
    """The property that makes the existing comment true.

    Identity, not equality: two identical function bodies are still two
    objects, and patching one of them does nothing to the other.
    """
    importlib.invalidate_caches()
    import access
    import dispatch
    assert dispatch.is_authorized is access.is_authorized, (
        "dispatch.is_authorized is not the same object as "
        "access.is_authorized -- a second definition crept back in")