"""prompts.py has content, and until this file it had no test. TECH-DEBT #12.

422 lines of module constants, zero functions, zero dedicated tests. One
other test file imports `prompts` incidentally, which is not the same
thing: it does not assert anything about the text.

The item exists because the prompts are about to move into files. That
move must not change a single byte the model sees, and "review 422 lines
of prose by eye" is not a verification strategy -- it is the same gap
that let `tests/test_t54` test a private copy of the code while the
production path went unverified.

Two layers, deliberately.

**Content pins.** SHA-256 of each constant. These do not tell you
whether a prompt got better; they make every edit visible and
deliberate. When one goes red the diff is in the commit that should have
said why the wording moved.

**Semantic properties.** These are what survive a relocation, and they
are the ones that actually catch a bad extraction. A prompt can be
reworded legitimately and the content pin updated with a stated reason;
these assertions still have to hold afterwards. Notably:

  * every command `BOT_COMMANDS` advertises must be a command the bot
    actually registers. Advertising a command that 404s is the defect
    a move would introduce;
  * the mandatory sections of `GROUP_CONTEXT` must still be present --
    the bash safety rules, the [llm] tag rule, and the ban on
    auto-bans are policy, not prose;
  * the prefix and emoji regexes must still parse what they are
    documented to parse.

The regex properties are the sharpest of the set. `_RATING_PREFIX_RE`
is what decides whether a model response counts as a rating or as text
to send to the user; a change that makes it greedier silently swallows
the user's reply.
"""
import ast
import hashlib
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tech_debt(item=12)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "telegram-bot"))

import prompts

# sha256[:16], utf-8. Update with a reason in the commit message.
CONTENT_PINS = {
    "GROUP_CONTEXT": "6f8f73267c413db3",
    "RATING_RULES": "c3797f4d8521a997",
    "WELCOME_TEXT": "d35362838a645cb4",
    "RATING_EMOJI": "bbda90f297e9f0c0",
    "BOT_COMMANDS": "785a53d39ee03f95",
}


# --- content pins ----------------------------------------------------

@pytest.mark.parametrize("name,expected", sorted(CONTENT_PINS.items()))
def test_prompt_content_is_unchanged(name, expected):
    value = getattr(prompts, name)
    raw = value if isinstance(value, str) else repr(value)
    got = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert got == expected, (
        f"{name} changed. If that is deliberate, say why in the commit and "
        f"update the pin to {got}. If it is not, this is the tripwire "
        f"doing the only thing it can.")


# --- the advertised commands must exist -------------------------------

def _registered_commands():
    """Command names dispatch.py hands to CommandHandler."""
    src = (ROOT / "telegram-bot" / "dispatch.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CommandHandler":
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                out.add(first.value)
    return out


def test_every_advertised_command_is_registered():
    advertised = {c for c, _ in prompts.BOT_COMMANDS}
    registered = _registered_commands()
    missing = sorted(advertised - registered)
    assert not missing, (
        f"BOT_COMMANDS advertises {missing} but dispatch.py registers no "
        f"handler for them. Telegram will show the button and the bot "
        f"will ignore it.")


def test_every_registered_command_is_advertised():
    registered = _registered_commands()
    advertised = {c for c, _ in prompts.BOT_COMMANDS}
    missing = sorted(registered - advertised)
    assert not missing, (
        f"dispatch.py registers {missing} that BOT_COMMANDS does not "
        f"advertise -- reachable but undiscoverable")


def test_command_names_are_unique():
    names = [c for c, _ in prompts.BOT_COMMANDS]
    assert len(names) == len(set(names)), f"duplicate command: {names}"


def test_every_command_has_a_description():
    for name, desc in prompts.BOT_COMMANDS:
        assert desc and desc.strip(), f"command {name!r} has no description"


def test_commands_with_an_argument_say_so():
    """`/newsub` with no argument is an error the user has to guess at."""
    for name, desc in prompts.BOT_COMMANDS:
        if name in ("newsub", "sub", "delsub"):
            assert f"/{name} <" in desc, (
                f"{name!r} takes an argument but its description "
                f"{desc!r} does not show the syntax")


# --- policy that must survive any rewording --------------------------

MANDATORY_SECTIONS = [
    "## Tool usage rules (ALWAYS)",
    "## bash_command safety rules (MANDATORY, personal mode only)",
    "## Core rules (ALWAYS)",
    "## The rating system",
    "## What you should NOT do",
]


@pytest.mark.parametrize("section", MANDATORY_SECTIONS)
def test_group_context_keeps_its_mandatory_sections(section):
    assert section in prompts.GROUP_CONTEXT, (
        f"GROUP_CONTEXT lost {section!r}. These are policy sections: "
        f"dropping one changes what the model is told to do, not how it "
        f"is worded.")


def test_the_llm_tag_rule_is_still_stated():
    """Rule 3 in Core rules. Every outbound message must carry [llm]."""
    assert "[llm]" in prompts.GROUP_CONTEXT, \
        "GROUP_CONTEXT no longer mentions the [llm] output tag"


def test_the_bash_safety_rules_still_name_all_five_categories():
    """The five prohibited operations, by category keyword.

    Keyword rather than exact text, so a reworded rule still passes while
    a dropped category does not. A dropped category is the real risk
    after a move: the paragraph survives, one item does not.
    """
    body = prompts.GROUP_CONTEXT
    for keyword in ("/workspace", "network", "system files",
                    "sensitive", "HISTFILE"):
        assert keyword in body, (
            f"the bash safety rules no longer mention {keyword!r}; a "
            f"prohibited category was dropped")


def test_auto_bans_are_still_forbidden():
    assert "NO AUTO-BANS" in prompts.GROUP_CONTEXT, \
        "the no-auto-bans rule is gone from GROUP_CONTEXT"


def test_rating_rules_still_describe_a_1_to_10_scale():
    assert "1-10" in prompts.RATING_RULES or "1–10" in prompts.RATING_RULES, \
        "RATING_RULES no longer states the 1-10 scale"


# --- the two regexes that decide what a model reply means ------------

def test_rating_prefix_regex_matches_the_documented_shape():
    """Greediness here swallows the user's actual reply.

    The wire format is `[[TYPE:<kind>]] [[RATE:<1-10>]] <the answer>`,
    with DOTALL so a multi-paragraph answer stays in `rest`. My first
    version of this test guessed a format (`RATING: 7`) that the regex
    never accepted, which is its own lesson: the test has to be written
    against what the code does, not against what the name suggests.
    """
    rx = prompts._RATING_PREFIX_RE

    m = rx.search("[[TYPE:question]] [[RATE:7]]\nhere is the answer")
    assert m, "a leading TYPE+RATE line must match"
    assert m.group("type") == "question"
    assert m.group("rate") == "7"
    assert "here is the answer" in m.group("rest")

    # TYPE without RATE is a legal shape: the rating is optional.
    m2 = rx.search("[[TYPE:statement]] just talking")
    assert m2, "TYPE without RATE must match"
    assert m2.group("type") == "statement"
    assert m2.group("rate") is None

    # The whole thing is anchored: a rating line in the middle of prose
    # is text to the user, not a rating.
    assert not rx.search("here is the answer\n[[TYPE:question]] [[RATE:7]]"), \
        "an unanchored match would strip the user's reply"

    # An out-of-range score: DOCUMENTED WEAKNESS, asserted as-is.

    # `[[RATE:0]]` still matches -- the whole RATE group is optional, so a
    # bad score is not "not a rating", it is a rating with no rate and the
    # literal marker left over in `rest`, which the user then sees in
    # the reply. Same for 11. My first version asserted these did not
    # match; they do, and the test was wrong about the code rather than
    # the code being wrong about what I expected.

    # Not fixed here. Tightening the alternation would change what counts
    # as a rating in production, on the path that decides whether a
    # model's answer is sent or swallowed. That is a product decision
    # about malformed-model-output handling, not a test-writing decision.
    for bad in ("0", "11", "07"):
        m = rx.search(f"[[TYPE:question]] [[RATE:{bad}]]")
        assert m, f"RATE={bad} unexpectedly rejected"
        assert m.group("rate") is None, \
            f"RATE={bad} was captured, which would be a fix"
        assert f"[[RATE:{bad}]]" in (m.group("rest") or ""), \
            (f"RATE={bad} should fall through into rest and be shown to "
             f"the user verbatim -- pin the current behaviour so a change "
             f"here is deliberate")

    # An unknown category is not a rating either -- a model inventing a
    # kind must not be able to route around the classifier.
    assert not rx.search("[[TYPE:gossip]] [[RATE:5]]"), \
        "an unknown TYPE must not match"

    # Multi-line answers survive intact, which is what DOTALL is for.
    m3 = rx.search("[[TYPE:info]] [[RATE:10]]\nline one\nline two")
    assert m3 and "line two" in m3.group("rest")


def test_emoji_char_regex_does_not_match_bare_text():
    rx = prompts._EMOJI_CHAR_RE
    assert rx.search("abc 🎉 def"), "an emoji must match"
    assert not rx.search("plain ascii only"), "ascii must not match"


def test_the_regexes_compile_as_written():
    for name in ("_RATING_PREFIX_RE", "_EMOJI_CHAR_RE", "_LLM_TOKEN_RE",
                 "_SUBTALK_NAME_RE", "_TOPIC_NAME_RE"):
        pat = getattr(prompts, name)
        assert isinstance(pat, re.Pattern), f"{name} is not a compiled pattern"
        assert pat.pattern, f"{name} has an empty pattern"


# --- structural: the module has no functions, and that is known --------

def test_prompts_still_has_no_functions():
    """Not an aspiration. It is what makes this file safe to relocate.

    If a helper function appears here, the constants are no longer the
    whole story and a byte-comparison of the constants stops being a
    byte-comparison of the prompt.
    """
    tree = ast.parse((ROOT / "telegram-bot" / "prompts.py").read_text())
    funcs = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert not funcs, f"prompts.py gained functions {funcs}"


def test_every_public_name_is_imported_somewhere():
    """A constant nothing imports is a prompt nobody sends."""
    tree = ast.parse((ROOT / "telegram-bot" / "prompts.py").read_text())
    public = [t.id for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets
              if isinstance(t, ast.Name) and not t.id.startswith("_")]
    assert public, "no public names found -- the parse above is wrong"
    imported = set()
    for f in sorted((ROOT / "telegram-bot").rglob("*.py")):
        if f.name == "prompts.py":
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "prompts":
                imported |= {a.name for a in node.names}
    orphans = sorted(set(public) - imported)
    assert not orphans, f"prompts.py defines {orphans}, which nothing imports"