"""A3 -- docs/CALL_LLAMA.md must not drift away from the code again.

This document is not ordinary documentation. call_llama.py's docstring
makes it a precondition:

    DO NOT REFACTOR without reading docs/CALL_LLAMA.md end-to-end ...

So a stale copy is worse here than no copy: the first thing a future
maintainer does is trust it, and four of its sections were wrong when
Block A started. The signature was missing chat_type, silent and
shutdown_event; the handler table still quoted max_tokens values that
moved to limits.py; §9 claimed there was no persistence; §11 gave a
sub-talks schema that is not the schema.

Deliberately narrow. This checks the claims that have already cost
somebody time, not the prose. A doc test that pins wording rots the day
after it is written and gets disabled -- which is worse than the rot it
was meant to catch.
"""
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOT = REPO / "telegram-bot"
DOC = REPO / "docs" / "CALL_LLAMA.md"

sys.path.insert(0, str(BOT))


def _doc():
    return DOC.read_text(encoding="utf-8")


def _call_llama_params():
    """The real signature, from the AST -- not a regex over the source."""
    tree = ast.parse((BOT / "call_llama.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call_llama")
    names = [a.arg for a in fn.args.args]
    kwonly = [a.arg for a in fn.args.kwonlyargs]
    return names, kwonly


def _doc_signature_block():
    """The ```python block in §1 that documents the signature."""
    text = _doc()
    m = re.search(r"The function signature is:\s*```python\n(.*?)```", text, re.S)
    assert m, (
        "docs/CALL_LLAMA.md no longer has a 'The function signature is:' "
        "python block. This test needs to know what shape to compare "
        "against; if the section was restructured deliberately, update "
        "this along with it."
    )
    return m.group(1)


# --- §1 signature -------------------------------------------------------

def test_doc_lists_every_real_parameter():
    names, kwonly = _call_llama_params()
    block = _doc_signature_block()
    missing = [n for n in names + kwonly if n not in block]
    assert not missing, (
        f"call_llama() takes {missing} but docs/CALL_LLAMA.md §1 does not "
        f"mention them. That docstring is the precondition for refactoring "
        f"this function -- someone will read it and not know these exist."
    )


def test_doc_does_not_invent_parameters():
    """The other direction. A doc naming a parameter that is gone sends a
    refactorer looking for code that was deleted three releases ago."""
    names, kwonly = _call_llama_params()
    block = _doc_signature_block()
    # identifiers that look like parameters: a bare name before '=' or ','
    claimed = set(re.findall(r"^\s{4}([a-z_][a-z0-9_]*)\s*[=:,)]", block,
                             re.M))
    real = set(names + kwonly)
    invented = sorted(c for c in claimed - real if c not in ("self",))
    assert not invented, (
        f"docs/CALL_LLAMA.md §1 documents parameters {invented} that "
        f"call_llama() does not have. Real ones: {sorted(real)}"
    )


def test_doc_flags_chat_type_as_required():
    """The one that shipped a security regression.

    T52 wired chat_type through four handlers and forgot it in the
    signature; a caller that omitted it would enable bash_command in a
    context that should be locked down. The code now raises. The doc has
    to say so, because the doc is what a refactorer reads.
    """
    import call_llama  # noqa: F401  -- imported for the real default
    names, kwonly = _call_llama_params()
    assert "chat_type" in kwonly, (
        "chat_type is no longer keyword-only. If that changed on purpose, "
        "the callers that were written against the keyword-only form are "
        "the thing to re-check."
    )
    block = _doc_signature_block().lower()
    assert "required" in block, (
        "§1 no longer says chat_type is required. It is the parameter that "
        "broke T52 -- documenting it as optional is how that happens twice."
    )


# --- §8 handler table ---------------------------------------------------

def test_doc_does_not_hardcode_max_tokens():
    """Those numbers moved to limits.py. A doc quoting them is a doc that
    is wrong, and it reads as authoritative."""
    block = re.search(r"\| Handler.*?\n\n", _doc(), re.S)
    assert block, "the handler table in §8 is gone"
    hardcoded = re.findall(r"\|\s*(\d{3,6})\s*\|", block.group(0))
    assert not hardcoded, (
        f"§8 hardcodes max_tokens as {hardcoded}. Every value now comes from "
        f"limits.py (MAX_TOKENS_DEFAULT / MAX_TOKENS_CAPTION); a table that "
        f"quotes numbers the code no longer uses is worse than no table."
    )


def test_use_stream_false_sites_match_the_doc():
    """The doc claims exactly two production call sites."""
    handlers = (BOT / "handlers.py").read_text(encoding="utf-8")
    n = len(re.findall(r"use_stream=False", handlers))
    doc = _doc()
    assert f"exactly {n} production call sites" in doc, (
        f"handlers.py has {n} use_stream=False sites but §8 does not say so. "
        f"handle_photo and its retry are the expected two; anything else "
        f"means either the doc or the code moved."
    )


# --- §11 schema ---------------------------------------------------------

def test_schema_tables_named_in_the_doc_exist():
    """(chat_id, thread_id), not (user_id, sub_talk)."""
    storage = (BOT / "storage.py").read_text(encoding="utf-8")
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", storage))
    doc = _doc()
    named = set(re.findall(r"^(\w+)\(chat_id", doc, re.M))
    assert named, "§11 no longer lists any chat-keyed tables"
    missing = sorted(named - tables)
    assert not missing, (
        f"docs/CALL_LLAMA.md §11 documents table(s) {missing} that storage.py "
        f"does not create. Real tables: {sorted(tables)}"
    )


def test_doc_does_not_describe_the_retired_sub_talk_schema():
    """An earlier revision documented sub_talks(user_id, name) and
    active_sub_talk. That schema is not the one, and per-sub-talk context
    has to be isolated per chat as well or two users in a group share a
    thread.

    The first version of this test looked for the table names and only
    found them fine because the surrounding prose happened to contain the
    word "not". It survived a mutation that rewrote the schema line to
    `sub_talks(user_id, name, ...)` -- the exact bug -- because
    test_schema_tables_named_in_the_doc_exist only looks for tables that
    ARE keyed on chat_id, and the rest of the doc still had those.

    So: any live schema line must not introduce a table keyed on user_id.
    """
    doc = _doc()
    offenders = []
    for line in doc.splitlines():
        stripped = line.strip()
        # Skip prose, corrections and struck-through history.
        if stripped.startswith(("~~", ">", "-", "#", "*")) or "```" in stripped:
            continue
        m = re.match(r"^(\w+)\(([a-z_]+)\s*[,)]", stripped)
        if not m:
            continue
        table, first_col = m.group(1), m.group(2)
        if table in ("sub_talks", "active_sub_talk") or first_col == "user_id":
            offenders.append(stripped)
    assert not offenders, (
        f"§11 describes {offenders} keyed on user_id. The isolation key is "
        f"(chat_id, thread_id); per-sub-talk context isolated by user only "
        f"means two users in one supergroup share a thread."
    )


# --- §4 modes -----------------------------------------------------------

def test_doc_says_webhook_and_polling_are_both_real():
    """A previous version of this comment claimed the polling loop had
    been deleted. It had not -- WEBHOOK_MODE defaults to 0 and the
    getUpdates loop is still in dispatch.py. The doc had drifted in the
    opposite direction, which is just as misleading."""
    doc = _doc()
    assert "WEBHOOK_MODE" in doc, (
        "§4 does not mention WEBHOOK_MODE. Both transports exist and which "
        "one runs is an env var; a reader who assumes polling has been "
        "removed is wrong."
    )
    dispatch = (BOT / "dispatch.py").read_text(encoding="utf-8")
    assert "getUpdates" in dispatch, (
        "the polling loop really is gone now. Update §4 rather than leaving "
        "it describing two modes."
    )


def test_config_default_for_webhook_mode_is_still_zero():
    """Pinned so the sentence above cannot rot into the reverse claim."""
    config = (BOT / "config.py").read_text(encoding="utf-8")
    assert 'WEBHOOK_MODE = os.environ.get("WEBHOOK_MODE", "0")' in config, (
        "the WEBHOOK_MODE default changed. §4 says polling is the config "
        "default and production overrides it; if that inverted, the doc "
        "is now wrong in a way a reader cannot detect."
    )


# --- §3.3 the wall-clock budget -----------------------------------------

def _loop_budget_values():
    """Every profile's LOOP_BUDGET_SEC, read from the AST of limits.py."""
    tree = ast.parse((BOT / "limits.py").read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant)
                    and key.value == "LOOP_BUDGET_SEC"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, (int, float))):
                found[f"limits.py:{node.lineno}"] = value.value
    return found


def test_the_wall_clock_budget_in_the_doc_is_the_one_in_the_code():
    """The budget is one number in the code; the doc quotes it three times.

    It had rotted to the wrong value: the doc said 600 s while
    limits.py said 7200 -- a twelve-fold difference, and the document
    contradicted ITSELF, because the limits table in the same file
    already carried the right number. A reader who took the prose got
    a turn that is aborted after ten minutes; a reader who took the
    table got the two hours the code actually allows.

    So the number is checked against the code, in all three places the
    doc states it. The comment in call_llama.py used to carry a fourth
    copy ("default 600 (10 min)"); it carries no number now, because a
    number in a comment is a copy nobody recomputes.
    """
    budgets = _loop_budget_values()
    assert budgets, (
        "no LOOP_BUDGET_SEC found in limits.py -- the check would compare "
        "nothing and pass")
    values = set(budgets.values())
    assert len(values) == 1, (
        "the profiles disagree about the wall-clock budget: "
        + ", ".join(f"{where}={value}" for where, value in budgets.items())
        + ". The doc states one number, so it now has to state them all "
          "-- or the profiles have to agree again."
    )
    budget = values.pop()
    doc = _doc()
    quoted = {}
    heading = re.search(r"### 3\.3 Wall-clock budget: (\d+) s", doc)
    assert heading, (
        "the §3.3 heading is gone -- the check would compare nothing")
    quoted["§3.3 heading"] = int(heading.group(1))
    guard_line = re.search(r"wall_clock_budget=(\d+)s", doc)
    assert guard_line, (
        "§1.1 no longer states the wall-clock budget -- the check would "
        "compare nothing")
    quoted["§1.1 guard line"] = int(guard_line.group(1))
    table = re.search(r"\|\s*`LOOP_BUDGET_SEC`\s*\|\s*(\d+)\s*\|", doc)
    assert table, (
        "the limits table no longer has a LOOP_BUDGET_SEC row -- the "
        "check would compare nothing")
    quoted["the limits table"] = int(table.group(1))
    wrong = [f"{where} says {said}, limits.py says {budget}"
             for where, said in quoted.items() if said != budget]
    assert not wrong, "\n".join(wrong)