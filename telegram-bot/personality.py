"""T69 — configurable bot persona, injected into the system prompt.

Why a file rather than a constant: this repo has 200+ forks and every operator
has a different idea of what their bot should be. A persona baked into
prompts.py means every fork either edits shared code or ships with somebody
else's character. So the identity lives in `personality.json` next to the bot,
and this module only reads and renders it.

Three rules, all of which cost something to learn:

1. **`enabled: false` means no persona at all** — not a default persona. A fork
   that never configured anything must not wake up speaking like somebody's
   aunt. Absence of a personality is the correct state for an unconfigured bot.

2. **Missing fields are omitted, never invented.** If the operator did not say
   the bot's age, the prompt does not mention age. A model left to fill gaps
   will invent a whole biography, and that invention becomes "the bot's
   personality" within a few turns.

3. **Rendered once, at import, into one system-message block.** The prompt is
   assembled per request; the persona is not re-read from disk per request,
   and it is not re-derived by string-formatting into GROUP_CONTEXT — that
   block is shared, and a fork that edits it would break the group rules for
   everyone.
"""

from __future__ import annotations

import json
import os

from logconf import get_logger
log = get_logger(__name__)

PERSONA_ENV = "BOT_PERSONA_FILE"
LOCAL_PERSONA_FILE = "persona.local.json"


def _load_path() -> str | None:
    """Resolve the persona file path. None means "no file configured".

    The file ships with the bot and is found next to this module, so an
    operator can either edit it in place or point BOT_PERSONA_FILE at their
    own copy. Pointing elsewhere is the option that survives `update.sh`,
    which syncs the whole telegram-bot/ tree from the repo.
    """
    override = os.environ.get(PERSONA_ENV, "").strip()
    if override:
        return override if os.path.isfile(override) else None
    here = os.path.dirname(os.path.abspath(__file__))
    # Only the operator's OWN file is read. personality.json.example ships with
    # the repo and is enabled=false on purpose: an unconfigured fork must not
    # acquire a stranger's persona just by pulling the latest commit.
    local = os.path.join(here, LOCAL_PERSONA_FILE)
    return local if os.path.isfile(local) else None


def load_persona(path: str | None = None) -> dict:
    """Read and validate the persona file. Never raises.

    A broken persona file must not stop the bot from starting — the same rule
    as CONTEXT_STRATEGY and BOT_REANCHOR_MODE. It returns {} and says so in
    the log, which means "no persona", i.e. the safe direction.
    """
    path = path or _load_path()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.info(f"[personality] cannot read {path}: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(data, dict):
        log.info(f"[personality] {path}: top level must be an object, ignoring")
        return {}
    if not data.get("enabled"):
        return {}
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    for key in ("name", "family_name", "patronymic", "profile"):
        if key in clean and not isinstance(clean[key], str):
            log.info(f"[personality] field {key!r} must be a string, dropping it")
            clean.pop(key)
    if "age" in clean:
        try:
            clean["age"] = int(clean["age"])
        except (TypeError, ValueError):
            log.info(f"[personality] field 'age' must be a number, dropping it")
            clean.pop("age")
    if "traits" in clean:
        if not isinstance(clean["traits"], list) or \
                not all(isinstance(t, str) for t in clean["traits"]):
            log.info("[personality] field 'traits' must be a list of strings, "
                  "dropping it")
            clean.pop("traits")
    return clean


def render_persona(persona: dict) -> str:
    """Turn the config into the block that goes into the system prompt.

    Returns "" when there is no persona — the caller then leaves the prompt
    exactly as it was.
    """
    if not persona or not persona.get("enabled"):
        return ""

    name = " ".join(
        p for p in (persona.get("name"), persona.get("patronymic"),
                    persona.get("family_name")) if p
    ).strip()

    lines = []
    if name:
        lines.append(f"Ты — {name}.")
    desc = _describe(persona)
    if desc:
        lines.append(desc)

    if persona.get("profile"):
        lines.append(persona["profile"].strip().rstrip(".").capitalize()
                     + ".")
    if persona.get("traits"):
        traits = [str(t).strip().rstrip(".") for t in persona["traits"]
                  if str(t).strip()]
        if traits:
            lines.append("Черты характера: " + "; ".join(traits) + ".")

    speech = persona.get("speech") or {}
    if isinstance(speech, dict):
        style = []
        if speech.get("tone"):
            style.append(str(speech["tone"]).strip().rstrip("."))
        if speech.get("address"):
            style.append(f"обращаешься {speech['address']}")
        if style:
            lines.append("Стиль речи: " + "; ".join(style) + ".")

    if len(lines) <= 1:
        # Only a bare name, nothing to say beyond it. Saying "Ты — Гадя." and
        # stopping gives the model room to invent the rest, which is exactly
        # what rule 2 forbids. Say nothing instead.
        return ""

    # Rule 2 was a docstring until 2026-10-22. A docstring is documentation
    # for whoever edits the file; the model never sees it. So the code
    # stopped inventing and the prompt did not tell the model not to --
    # which is half a guarantee, and the half that fails quietly.
    #
    # The directive names the class of thing rather than listing age and
    # sex: an enumeration is a list someone forgets to extend, and this is
    # exactly the failure that would be missed.
    lines.append(_NO_INVENTION)
    return "\n".join(lines)


#: Appended to every rendered persona. One sentence, imperative, and
#: phrased so that being asked directly produces a refusal rather than a
#: creative answer -- a model told "don't invent" and then asked "how old
#: are you" will usually comply, but only if the instruction also says
#: what to do when asked.
_NO_INVENTION = (
    "Не додумывай своя биография: всё, что не сказано выше, тебе "
    "неизвестно. Если спросят — говори, что не знаешь."
)


def _sex_ru(value) -> str:
    """Gender as a standalone phrase.

    Returns a phrase that stands on its own, so the caller can join it with
    age without producing "Тебе женщина, ты женщина, 36 лет." — which is what
    an earlier version rendered, and which reads as broken Russian to a model
    trained on correct text.
    """
    v = str(value).strip().lower()
    return {
        "female": "ты женщина",
        "male": "ты мужчина",
        "f": "ты женщина",
        "m": "ты мужчина",
    }.get(v, str(value))


def _describe(persona: dict) -> str:
    """One sentence of age and gender, or None if neither is set."""
    bits = []
    if persona.get("sex"):
        bits.append(_sex_ru(persona["sex"]))
    if persona.get("age"):
        age = persona["age"]
        bits.append(f"{age} {'год' if age % 10 == 1 and age % 100 != 11 else 'года' if age % 10 in (2, 3, 4) and age % 100 not in (12, 13, 14) else 'лет'}")
    if not bits:
        return None
    joined = ", ".join(bits)
    if bits and bits[0].startswith("ты "):
        # "ты женщина, 36 лет" already has a subject; "Тебе ты женщина" does not.
        return joined[0].upper() + joined[1:] + "."
    return "Тебе " + joined + "."


#: Rendered once at import. Rendered per request would mean re-reading a file
#: on every message, and a hot-reload path is not worth the failure modes.
PERSONA = load_persona()
PERSONA_BLOCK = render_persona(PERSONA)


def system_message() -> dict | None:
    """The persona as a system message, or None when there is no persona."""
    if not PERSONA_BLOCK:
        return None
    return {"role": "system", "content": PERSONA_BLOCK}