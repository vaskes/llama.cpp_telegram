"""No module may bind storage.get_store at import time.

Review round 3, correctly: patching get_store in six places is not a fixture
problem, it is a symptom. Six modules said `from storage import get_store`,
which freezes the function object at import time, so patching the owner
reached none of them.

That is now impossible to reintroduce silently. Binding it again would make
the test suite lie again -- the tests would still pass, because they patch the
owner and the code would be using a stale reference to the real DB.
"""
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

# bot.py is exempt and deliberately so: it is the entry point, and capturing
# the singleton at startup is the one place where that is the right thing.
EXEMPT = {"bot.py", "storage.py"}


def test_no_module_binds_get_store_at_import_time():
    offenders = []
    for path in sorted(BOT_DIR.glob("*.py")):
        if path.name in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "storage":
                for alias in node.names:
                    if alias.name == "get_store":
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"`from storage import get_store` reintroduced at {offenders}. "
        f"That binding is captured at import time, so patching "
        f"storage.get_store stops reaching this module and the suite goes "
        f"quietly back to using the real database. Use `import storage` and "
        f"call storage.get_store() at the point of use."
    )


def test_storage_is_reachable_through_the_module_everywhere():
    """The positive half: the consumers really do go through the module."""
    tree = ast.parse((BOT_DIR / "turn.py").read_text(encoding="utf-8"))
    calls_module_form = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_store"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "storage"
        for node in ast.walk(tree)
    )
    assert calls_module_form, (
        "turn.py no longer resolves storage.get_store() through the module; "
        "the rollback would go to the real database under test"
    )


def test_turn_accepts_an_injected_store():
    """DI has to actually exist, not just be documented in a docstring."""
    from turn import Turn

    sentinel = object()
    t = Turn(1, "main", "test", store=sentinel)
    assert t._store is sentinel, (
        "Turn ignored the store it was handed. A caller that already has one "
        "should not need to patch anything."
    )


def test_turn_defaults_to_the_module_singleton():
    """Injection is optional. Production constructs Turn with three args."""
    from turn import Turn
    import storage

    t = Turn(1, "main", "test")
    assert t._store is None
    assert storage.get_store() is not None, "singleton resolution broken"