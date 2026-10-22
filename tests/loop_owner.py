"""Where the tool loop lives, as of TECH-DEBT #8 T8.1.

The loop body was 318 lines of `call_llama` until T8.1 moved it into
`llm_iteration.ToolLoop.run_one`. Every tripwire that asserted on the
loop's internals went red at once -- not because anything broke, but
because each one read `call_llama.py` and looked for code that is now
one module over.

This helper is the single place that knows the split, so the next
extraction changes one line here rather than twenty assertions spread
over seventeen files. `call_source()` stays for the checks that really
are about `call_llama` -- that it builds the loop, that it re-exports,
that it kept the two functions TECH-DEBT #1 said it would keep.

Both files are read, never a line range: a check that pins a span of
lines goes red on a harmless edit, and that is how the previous
generation of these tripwires ended up demanding a specific indent.
"""
import ast
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"

CALL_LLAMA = "call_llama.py"
LOOP = "llm_iteration.py"


def call_source() -> str:
    return (BOT_DIR / CALL_LLAMA).read_text(encoding="utf-8")


def loop_source() -> str:
    return (BOT_DIR / LOOP).read_text(encoding="utf-8")


def call_tree():
    return ast.parse(call_source())


def loop_tree():
    return ast.parse(loop_source())


def run_one():
    """The `ToolLoop.run_one` node -- the named iteration."""
    for cls in loop_tree().body:
        if isinstance(cls, ast.ClassDef) and cls.name == "ToolLoop":
            for node in cls.body:
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_one":
                    return node
    raise AssertionError("ToolLoop.run_one is gone -- the iteration is not named")


def loop_names():
    """Every bare name called inside `run_one`."""
    return {c.func.id for c in ast.walk(run_one())
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
