"""T69 — configurable persona injected into the system prompt.

The operator's requirement: the persona lives in a file, because every one of
deployment wants a different bot and a constant in prompts.py would give every
unconfigured deployment somebody else's aunt.

The tests below are mostly about NOT having a persona. The happy path is one
assertion; the ways to ship a stranger's personality into someone else's bot
are many, and all of them look like success from inside the author's machine.
"""
import ast
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


def _write(tmp_path, data, name="personality.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# absence of a persona is the default
# ---------------------------------------------------------------------------

def test_disabled_persona_renders_nothing():
    import personality as p
    assert p.render_persona({"enabled": False, "name": "Гадя",
                             "profile": "сварливая"}) == ""


def test_disabled_persona_has_no_system_message():
    import personality as p
    assert p.system_message() is None or p.PERSONA.get("enabled") is False


def test_missing_file_is_not_an_error(tmp_path):
    import personality as p
    assert p.load_persona(str(tmp_path / "nope.json")) == {}


def test_broken_json_is_not_an_error(tmp_path, capsys):
    import personality as p
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert p.load_persona(str(bad)) == {}
    assert "cannot read" in capsys.readouterr().out


def test_non_object_toplevel_is_rejected(tmp_path, capsys):
    """A list where a dict was expected. Silently iterating it would produce
    nonsense in the prompt."""
    import personality as p
    lst = tmp_path / "l.json"
    lst.write_text("[1,2,3]", encoding="utf-8")
    assert p.load_persona(str(lst)) == {}
    assert "must be an object" in capsys.readouterr().out


def test_name_alone_renders_nothing():
    """"Ты — Гадя." and stop.

    That gives the model an identity with nothing to hold onto, and it will
    invent a biography — which then becomes the bot's personality within a few
    turns. Absence is the honest answer when the operator only gave a name.
    """
    import personality as p
    assert p.render_persona({"enabled": True, "name": "Гадя"}) == ""


# ---------------------------------------------------------------------------
# field validation — a bad type is dropped, not stringified
# ---------------------------------------------------------------------------

def test_bad_types_are_dropped_not_coerced(tmp_path):
    """age: "old" must not reach the prompt as "old".  It would read as
    nonsense to the model, or worse, be silently invented into a number."""
    import personality as p
    path = _write(tmp_path, {
        "enabled": True, "name": "Гадя", "profile": "брюзга",
        "age": "тридцать шесть", "sex": "female",
        "traits": "сварливая, пьяница",  # a bare string, not a list
    })
    out = p.load_persona(path)
    assert "age" not in out
    assert "traits" not in out
    assert out["name"] == "Гадя"   # the good fields survive


def test_string_age_is_coerced_when_numeric(tmp_path):
    """JSON gives 36 as int, but an operator hand-editing the file will quote
    it. "36" is unambiguous and worth accepting."""
    import personality as p
    path = _write(tmp_path, {"enabled": True, "name": "X", "profile": "p",
                             "age": "36"})
    assert p.load_persona(path)["age"] == 36


def test_numeric_name_is_dropped(tmp_path, capsys):
    import personality as p
    path = _write(tmp_path, {"enabled": True, "name": 42, "profile": "p"})
    out = p.load_persona(path)
    assert "name" not in out
    assert "must be a string" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_full_name_order_is_given_patronymic_family():
    """Russian order, not Western. "Гадя Хренова Петрович" reads wrong to a
    Russian-trained model and, more importantly, to the operator reading the
    transcript."""
    import personality as p
    out = p.render_persona({
        "enabled": True, "name": "Гадя", "patronymic": "Петрович",
        "family_name": "Хренова", "profile": "сварливая",
    })
    assert "Гадя Петрович Хренова" in out


def test_gender_does_not_produce_broken_russian():
    """An earlier version rendered "Тебе женщина, ты женщина, 36 лет." —
    which reads as broken text, and a model trained on correct Russian will
    treat the whole persona block as noise."""
    import personality as p
    out = p.render_persona({"enabled": True, "name": "X", "profile": "p",
                            "sex": "female", "age": 36})
    assert "Тебе ты женщина" not in out
    assert "Ты женщина, 36 лет" in out


@pytest.mark.parametrize("age,word", [
    (1, "год"), (2, "года"), (5, "лет"), (21, "год"), (22, "года"),
    (36, "лет"), (100, "лет"), (111, "лет"), (112, "лет"),
])
def test_russian_age_agreement(age, word):
    import personality as p
    out = p._describe({"sex": "female", "age": age})
    assert out.endswith(f"{age} {word}."), f"age {age}: got {out}"


def test_missing_fields_are_simply_omitted():
    """No age configured means no age in the prompt — not a guess."""
    import personality as p
    out = p.render_persona({"enabled": True, "name": "Гадя",
                            "profile": "сварливая"})
    assert "лет" not in out and "год" not in out


def test_traits_are_rendered():
    import personality as p
    out = p.render_persona({"enabled": True, "name": "Гадя",
                            "profile": "брюзга",
                            "traits": ["обсуждает всех", "любит пиво"]})
    assert "обсуждает всех" in out and "любит пиво" in out


def test_speech_block_is_optional():
    import personality as p
    assert "Стиль речи" not in p.render_persona(
        {"enabled": True, "name": "X", "profile": "p"})
    assert "Стиль речи" in p.render_persona(
        {"enabled": True, "name": "X", "profile": "p",
         "speech": {"tone": "короткое"}})


def test_non_dict_speech_is_ignored():
    import personality as p
    out = p.render_persona({"enabled": True, "name": "X", "profile": "p",
                            "speech": "просто строка"})
    assert "Стиль речи" not in out


def test_comment_keys_are_stripped(tmp_path):
    """The config file carries _comment keys for humans. They must not reach
    the model — they would be read as instructions."""
    import personality as p
    path = _write(tmp_path, {"enabled": True, "name": "X", "profile": "p",
                             "_comment": "instructions for the operator"})
    out = p.load_persona(path)
    assert not any(k.startswith("_") for k in out)


# ---------------------------------------------------------------------------
# injection into the prompt
# ---------------------------------------------------------------------------

def test_persona_is_injected_before_group_context():
    """The persona is one operator's; GROUP_CONTEXT is shared by every fork.
    Persona first, so an edit to the shared rules cannot demote it."""
    src = (BOT_DIR / "call_llama.py").read_text()
    assert "personality_system_message()" in src
    assert src.count("_persona_msg") >= 3, (
        "both the tools branch and the rating branch must carry the persona"
    )


def test_both_prompt_branches_carry_the_persona():
    """Missing it in one branch means the bot is a different person depending
    on whether tools happen to be on — which is the kind of bug nobody reports
    because it looks like the model being inconsistent."""
    src = (BOT_DIR / "call_llama.py").read_text()
    assert src.count('(_persona_msg or {}).get("content"') >= 2, (
        "the persona must be prepended in the tools branch AND the rating branch"
    )


def test_example_file_ships_and_is_valid_json():
    f = BOT_DIR / "personality.json.example"
    assert f.is_file(), "the repo must ship a template so operators have one"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "enabled" in data


def test_example_file_ships_disabled():
    """The one rule that protects every deployment.

    The operator's own persona must not become everybody else's persona. The repo
    ships personality.json.example with enabled=false and the loader reads
    ONLY persona.local.json. If someone flips the example to true, the
    template itself becomes the default for every deployment that never
    configured one — silently, on next pull.
    """
    data = json.loads((BOT_DIR / "personality.json.example")
                      .read_text(encoding="utf-8"))
    assert data.get("enabled") is False, (
        "personality.json.example must ship with enabled=false. The live "
        "persona belongs in persona.local.json (gitignored) or whatever "
        "BOT_PERSONA_FILE points at."
    )


def test_live_persona_file_is_not_committed():
    """The operator's persona is theirs, not the repo's."""
    gi = REPO_ROOT / ".gitignore"
    if not gi.is_file():
        pytest.skip("no .gitignore")
    assert "persona.local.json" in gi.read_text(), (
        "persona.local.json holds a real operator's identity and must be "
        "gitignored, or update.sh will publish it with the next deploy"
    )


def test_no_live_persona_file_in_the_repo():
    """Belt and braces: if it is gitignored it should not be here at all."""
    assert not (BOT_DIR / "persona.local.json").exists(), (
        "persona.local.json must not live inside the repo tree — it is "
        "synced to every deployment by update.sh and would be committed"
    )


def test_update_sh_preserves_the_live_persona_file():
    """update.sh rsyncs the whole telegram-bot/ tree with --delete. A new
    deploy must not wipe the operator's persona."""
    src = (REPO_ROOT / "scripts" / "update.sh").read_text()
    assert "persona.local.json" in src, (
        "update.sh must exclude persona.local.json from the sync, the same way "
        "it preserves .env"
    )

def test_the_never_invent_directive_reaches_the_model():
    """The rule moved out of a docstring on 2026-10-22 and nothing held it.

    It used to live in `render_persona`'s docstring, which is
    documentation for whoever edits the file: the model never sees it.
    The prompt now carries the directive, and this is the only thing that
    says so. Deleting the `lines.append(_NO_INVENTION)` line leaves every
    other test in this file green, and the bot quietly starts filling in
    a biography the operator never wrote.
    """
    sys.path.insert(0, str(BOT_DIR))
    import personality

    full = personality.render_persona({
        "enabled": True, "name": "Гадя", "sex": "female", "age": 36,
        "profile": "сварливая", "traits": ["сварливая"],
    })
    assert personality._NO_INVENTION in full, (
        "the rendered persona no longer carries the never-invent "
        "directive; the model will invent an identity the operator "
        "never gave it")
    assert full.rstrip().endswith(personality._NO_INVENTION), (
        "the directive is not last -- it reads as one rule among many "
        "instead of the thing that settles any question about identity")
