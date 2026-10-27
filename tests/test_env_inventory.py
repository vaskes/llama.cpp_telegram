"""Every env var the code reads is documented, and every one documented is read.

TECH-DEBT #11.

The item as filed said config.py reads 33 env vars with one validation.
Measured, that is wrong in three ways, and the wrongness matters because
it is what made the real gap invisible:

  * there are 36 distinct names across the package, not 33;
  * they are read from SEVEN modules, not one. `config.py` reads 25.
    `context_budget.py` reads 6, `selftest.py` 2, `reanchor.py` 2, and
    `call_llama.py` / `limits.py` / `prompts.py` read 1 each.
  * the value was never the problem. A misspelled NAME is the problem,
    and no amount of per-line value validation reaches a name.

The brief proposed failing at startup on any unknown variable carrying
an `LLAMABOT_` / `DONSETCH_` prefix. Only 3 of the 36 names carry
either prefix, so that guard would have been inert on 92% of the
surface it was supposed to protect.

The property actually worth pinning is two-way:

  * a name in `.env.example` that nothing reads is a documented lie --
    an operator sets it and nothing happens. This is the exact failure
    the item describes, so it is a hard failure.
  * a name the code reads that `.env.example` does not list is a
    setting nobody can discover. Also a hard failure now, because the
    21 of those have been added to the file.

Both directions were checked against the current tree before this
test was written, so neither starts red.
"""
import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=11)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "telegram-bot"
EXAMPLE = SRC / ".env.example"

# A read is a literal argument to os.environ.get / os.getenv, OR a
# literal passed to a helper whose body does that read with one of its
# parameters. The second case is not hypothetical: limits.py reads a
# dozen LLAMABOT_* settings through `_env_or(env_name, ...)`, and a
# regex over `os.environ.get` sees exactly one name there, not
# thirteen.
#
# The broader shortcut -- "every CAPS string literal in the package" --
# was measured and rejected: it yields 78 names including internal
# constants (BLOAT_EMOJI, WELCOME_TEXT, RATING_RULES) and prefix
# fragments left by f-string building (ALLOWED_, MAX_, LLAMA_). Forty
# false positives is worse than no check.
READ_RE = re.compile(
    r"os\.environ\.get\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
    r"|os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']")


def _code_files():
    """rglob, not glob: personal_tools/bash_command.py reads four of
    these variables and a non-recursive glob silently excluded the whole
    subpackage. `__pycache__` holds no .py.
    """
    return [f for f in sorted(SRC.rglob("*.py")) if "__pycache__" not in f.parts]


def _indirect_readers():
    """(module, callable, param) triples whose body does an indirect read.

    Derived by walking the AST rather than hardcoded, so a new helper
    of the same shape is picked up instead of quietly going missing.
    """
    out = set()
    for f in _code_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in node.args.args}
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                f_ = sub.func
                if not (isinstance(f_, ast.Attribute)
                        and f_.attr in ("get", "getenv")):
                    continue
                if not (f_.value.__class__.__name__ == "Attribute"
                        and f_.value.attr == "environ"):
                    continue
                if sub.args and isinstance(sub.args[0], ast.Name) \
                        and sub.args[0].id in params:
                    out.add((f.name, node.name, sub.args[0].id))
    return out


def _indirect_names():
    """String literals passed to those helpers at their call sites."""
    readers = _indirect_readers()
    if not readers:
        return set()
    found = set()
    for f in _code_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if not fname or not node.args:
                continue
            arg0 = node.args[0]
            if not (isinstance(arg0, ast.Constant) and isinstance(arg0.value, str)):
                continue
            for mod, fn, param in readers:
                if fn == fname and re.fullmatch(r"[A-Z][A-Z0-9_]*", arg0.value):
                    found.add(arg0.value)
    return found


def _module_const_names():
    """Names read via a module-level constant: PERSONA_ENV -> BOT_PERSONA_FILE."""
    found = set()
    for f in _code_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        consts = {n.targets[0].id: n.value.value
                  for n in tree.body
                  if isinstance(n, ast.Assign)
                  and len(n.targets) == 1
                  and isinstance(n.targets[0], ast.Name)
                  and isinstance(n.value, ast.Constant)
                  and isinstance(n.value.value, str)
                  and re.fullmatch(r"[A-Z][A-Z0-9_]*", n.value.value)}
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr in ("get", "getenv") \
                    and sub.args and isinstance(sub.args[0], ast.Name) \
                    and sub.args[0].id in consts:
                found.add(consts[sub.args[0].id])
    return found
# A commented-out `NAME=` counts as declared. That is this file's
# established convention for "available but off by default" --
# LLAMABOT_DEPLOYMENT_PROFILE, TELEGRAM_API and LLAMABOT_DEBUG_PAYLOAD
# are all written that way. Treating a comment as undocumented would
# have made the test demand that operators switch on features they do
# not want, which is the opposite of what the file is for.
DECLARE_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", re.M)


def _read_names():
    found = set()
    for f in _code_files():
        for m in READ_RE.finditer(f.read_text(encoding="utf-8")):
            found.add(m.group(1) or m.group(2))
    found |= _indirect_names()
    found |= _module_const_names()
    return found


def _declared_names():
    return set(DECLARE_RE.findall(EXAMPLE.read_text(encoding="utf-8")))


READ = _read_names()
DECLARED = _declared_names()


def test_the_glob_is_not_silently_empty():
    """A broken path makes every test below pass for the wrong reason."""
    assert len(READ) > 30, f"only found {len(READ)} env reads -- wrong path?"
    assert len(DECLARED) > 30, f"only found {len(DECLARED)} declared -- wrong path?"


# Reads whose name is not a literal, and therefore invisible to the
# inventory below. Pinned deliberately rather than forbidden: both are
# legitimate, and a hard failure would have meant either breaking them
# or deleting the test. An allowlist keeps the property -- a NEW
# dynamic read has to be acknowledged here -- without pretending the
# existing two do not exist.
# Reads the inventory cannot resolve to a literal. limits.py and
# personality.py both resolved (see _indirect_names and
# _module_const_names), so this set is expected to be EMPTY -- and
# test_computed_env_names_are_all_acknowledged below fails if anything
# lands in it, which is the point: an unresolvable read is a hole in
# the inventory and has to be a decision, not a silence.
#
# The third one is the MCP config block's `token_env`: the OPERATOR
# names the variable that holds the token, in the JSON block, so the
# name is not the bot's to know. It cannot be in the inventory of the
# bot's own names by construction, and the block is where it is
# visible.
KNOWN_COMPUTED_READS = {("config.py", "token_env")}


def test_computed_env_names_are_all_acknowledged():
    """A dynamic read drops out of the inventory silently.

    `os.environ.get(env_name)` matches neither READ_RE nor any document
    check, so the two-way inventory would report a clean bill of health
    while missing the variable entirely. Two such reads exist and are
    listed above, and a third one (the MCP block's `token_env`) is
    acknowledged because the operator, not the bot, names it. This
    fails when a fourth appears, which is the moment someone needs to
    decide whether it belongs in the inventory.
    """
    # Sites that _indirect_readers/_module_const_names already resolve
    # are not holes -- limits.py's `env_name` and personality.py's
    # PERSONA_ENV both come back as real names. Only what is left after
    # that resolution is a genuine hole.
    resolved = {(mod, param) for mod, _fn, param in _indirect_readers()}
    module_consts = set()
    for f in _code_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                    and isinstance(n.targets[0], ast.Name) \
                    and isinstance(n.value, ast.Constant) \
                    and isinstance(n.value.value, str) \
                    and re.fullmatch(r"[A-Z][A-Z0-9_]*", n.value.value):
                module_consts.add((f.name, n.targets[0].id))
    already = resolved | module_consts

    found = set()
    for f in _code_files():
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"os\.environ\.get\(\s*([^,\)]+)", src):
            arg = m.group(1).strip()
            if arg and not arg.startswith(("'", '"')) \
                    and (f.name, arg) not in already:
                found.add((f.name, arg))
    undeclared = found - KNOWN_COMPUTED_READS
    assert not undeclared, (
        "env var(s) read under a computed name and not acknowledged in "
        f"KNOWN_COMPUTED_READS: {sorted(undeclared)}. They are invisible to "
        "the inventory below, so they need either a literal read or an "
        "entry here stating why not.")


def test_every_documented_name_is_actually_read():
    """A documented lie: an operator sets it and nothing happens."""
    dangling = sorted(DECLARED - READ)
    assert not dangling, (
        f"documented in .env.example but read by nothing: {dangling}")


def test_every_read_name_is_documented():
    """A setting nobody can discover."""
    undocumented = sorted(READ - DECLARED)
    assert not undocumented, (
        f"read by the code but absent from .env.example: {undocumented}")


@pytest.mark.parametrize("name", sorted(READ))
def test_the_name_appears_in_the_example_file(name):
    """Per-name, so the failure names the variable rather than a set.

    Set-level assertions give one line listing twenty missing names;
    this gives twenty lines naming one each, which is the difference
    between a fixable report and a puzzle.
    """
    assert name in DECLARED, f"{name} is read but not documented"


def test_config_still_validates_the_one_variable_that_matters():
    """The single check that predates this item must not rot away."""
    src = (SRC / "config.py").read_text(encoding="utf-8")
    assert re.search(
        r'BOT_TOKEN\s*=\s*os\.environ\.get\(\"BOT_TOKEN\"\)', src), \
        "config.py must still read BOT_TOKEN without a default"
    assert "BOT_TOKEN env var is required" in src, \
        "the BOT_TOKEN startup check is gone"