"""Tripwire: the bot's UI copy is English, and it stays English.

The operator's decision (2026-10-25): every text the *user* reads is
English. The model's own voice stays Russian by design -- the persona,
the summariser prompt and the context header are addressed to the model,
not to the user, and translating them would change the bot's speech.

So the rule is not "no Cyrillic in the repo". It is:

* a string literal that is NOT a docstring and contains Cyrillic is
  user-facing text unless the module is explicitly listed below with the
  fragment that is allowed there;
* docstrings and comments are developer-facing and stay out of the way
  (the check skips them, so a Russian docstring never masks a Russian
  notice -- they are different kinds of text).

Why an allowlist and not "the model-facing files may be Russian": the
allowlist is keyed by module *and* fragment, so a module that is allowed
one Russian line cannot silently grow a second one. A new module with
Russian copy is rejected by default -- the tripwire fails until someone
says which of the two kinds of text it is. And a mixed module (the
context builder holds both a model-facing header and a user-facing
overflow notice) is protected by the fragment, not by the file: a
Russian OVERFLOW_MESSAGE would not contain the allowed header fragment
and would be caught.

The checker is exercised on a synthetic case below: a tripwire that
cannot fail is not a tripwire.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOT = REPO / "telegram-bot"

_CYR = re.compile(r"[А-Яа-яЁё]")

# Module -> the Cyrillic fragments that are allowed there, with the
# reason. A module that is not listed allows NOTHING.
ALLOWED = {
    "personality.py": [
        # The persona template: the model's own voice (Russian by design).
        "Не додумывай",
        "Ты —",
        "Тебе",
        "ты",
        "ты женщина",
        "ты мужчина",
        "обращаешься",
        "Черты характера",
        "Стиль речи",
        "год",
        "года",
        "лет",
    ],
    "summarize.py": [
        # The summariser prompt: addressed to the model, not to the user.
        "Ты сжимаешь историю",
    ],
    "llm_tools.py": [
        # The reaction tool description, with a Russian request as the
        # example of what the user may type. Model-facing schema.
        # It moved here with the rest of the tool list (2026-10-27).
        "поставь",
    ],
    "context_budget.py": [
        # The context header shown to the MODEL before the user prompt.
        # NOT the overflow notice -- that one is user-facing and English.
        "Сводка разговора вне окна памяти",
    ],
    "storage.py": [
        # The DDL: the column that marks a summarised message.
        "СВЕДЕНО",
    ],
    "selftest_handlers.py": ["что на фото?"],
    "selftest_llm.py": ["проверь математику"],
    "selftest_media.py": ["Что на картинке?"],
    "llm_delete.py": [
        # The delete tool's description: the example of a message the
        # user names by what it wrote, in the words the operator used in
        # his own chat. Model-facing schema.
        "Сообщение 1",
    ],
    "prompt_text/group_context.py": [
        # The rules about where a message id may come from, with the
        # same example. Model-facing.
        "Сообщение 1",
    ],
}

# The shortest fragment that may be an exception. Two characters is
# enough: the match is a substring test, so a short fragment only
# forgives text in a module that is already vouched for model-facing.
_MIN_FRAGMENT = 2


def _docstring_nodes(tree: ast.AST) -> set:
    """ids() of the string nodes that are docstrings."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(
                    getattr(first, "value", None), ast.Constant) and isinstance(
                    first.value.value, str):
                out.add(id(first.value))
    return out


def cyrillic_literals(source: str) -> list:
    """Every non-docstring string literal of `source` that has Cyrillic."""
    tree = ast.parse(source)
    docs = _docstring_nodes(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        if id(node) in docs:
            continue
        if _CYR.search(node.value):
            out.append(node.value)
    return out


def violations(module: str, literals: list) -> list:
    """The literals of `module` that no exception covers."""
    allowed = ALLOWED.get(module, [])
    out = []
    for lit in literals:
        if not _CYR.search(lit):
            continue
        if any(a in lit for a in allowed):
            continue
        out.append(lit)
    return out


def _bot_modules() -> list:
    return sorted(
        p for p in BOT.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def test_the_bot_has_modules_to_check():
    """A tripwire over zero files is a tripwire that cannot fail."""
    modules = _bot_modules()
    assert len(modules) > 20, (
        f"only {len(modules)} modules found under {BOT} -- the glob is wrong, "
        "and the language check would pass over an empty set"
    )


@pytest.mark.parametrize("path", _bot_modules(),
                         ids=lambda p: p.name)
def test_no_russian_reaches_the_user(path: Path):
    """No user-facing text of the bot is Russian."""
    # The key is the path RELATIVE to telegram-bot/, not the file name:
    # a file in a subdirectory (prompt_text/, personal_tools/,
    # commands/) has a name that says nothing about which file it is --
    # and with the name as the key, such a file could not be vouched for
    # at all, which is a way of forbidding the exception rather than of
    # describing it. The allowlist is keyed the same way below, and
    # `test_the_allowlist_is_not_a_blanket` checks that every key is a
    # path that exists.
    module = str(path.relative_to(BOT))
    literals = cyrillic_literals(path.read_text(encoding="utf-8"))
    bad = violations(module, literals)
    assert not bad, (
        f"{path.relative_to(REPO)} carries Russian user-facing text:\n"
        + "\n".join(f"  {lit[:120]!r}" for lit in bad)
        + "\nThe UI language is English. If this text is addressed to the "
          "MODEL (persona, summariser prompt, context header), it belongs "
          "in ALLOWED in this file -- with the exact fragment."
    )


def test_the_allowlist_is_not_a_blanket():
    """Every exception is a real, used fragment.

    A stale entry (the text is gone) would let the tripwire pass over a
    string nobody is looking at any more -- and it would mean the
    allowlist no longer describes the tree.
    """
    for module, fragments in ALLOWED.items():
        path = BOT / module
        assert path.exists(), f"{module} does not exist any more"
        literals = cyrillic_literals(path.read_text(encoding="utf-8"))
        for frag in fragments:
            assert len(frag) >= _MIN_FRAGMENT, (
                f"{module}: the exception {frag!r} is too short to be "
                "specific -- it would match unrelated text"
            )
            assert any(frag in lit for lit in literals), (
                f"{module}: the exception {frag!r} is stale -- no literal "
                "in the file contains it any more"
            )


def test_the_checker_can_fail():
    """A tripwire that cannot fail is not a tripwire."""
    russian = "Сводка разговора вне окна памяти"
    english = "The engine is unavailable right now."

    # The synthetic Russian copy in a user-facing module is caught.
    assert violations("user_notice.py", [russian])
    # The same string is allowed in the module that owns it.
    assert not violations("context_budget.py", [russian])
    # English is always fine.
    assert not violations("user_notice.py", [english])
    # A module nobody vouched for allows nothing.
    assert violations("brand_new_module.py", [russian])
    # A docstring is not user-facing text -- the caller filters it out.
    assert not violations("user_notice.py", [])
