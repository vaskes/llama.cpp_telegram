"""The engine's prefix cache, and what the bot has to do to earn it.

Measured on the production engine on 2026-11-01, with the engine's own
chat template and the real chat of the operator:

  * the same body sent twice, unchanged:  `20 552 = 20 547 reused + 5
    read` (0.07 s) against `20 552 = 0 reused + 20 552 read` (41.3 s) --
    565 times faster for one digit;
  * a prompt whose BEGINNING moved (a tool description that names this
    turn's ids): `reused 0`, every turn;
  * a context that slides (the newest 20 messages): `reused 0`, every
    turn -- 13 077 tokens re-read in 40.2 s;
  * a growing prefix of 37 165 tokens: reused whole, 1.1 s.

The engine matches the new prompt against the one it holds, prefix by
prefix, up to the last turn marker, and the match is all-or-nothing: one
changed digit and NOTHING is reused. So the bot has to keep two things
byte-identical from turn to turn:

  1. the BEGINNING of the prompt -- the tools block and the system
     message;
  2. the part of the history that was already there.

Every test in this file is RED on the tree of 2026-10-31, which slid
the context and put the turn's ids into the tool's description. That is
the point of the file: the failure is invisible from the outside (the
bot answers, just slowly, forever) and nothing in the logs says why.
"""
import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import context_budget as cb  # noqa: E402
import llm_delete  # noqa: E402
import llm_messages  # noqa: E402
import llm_tools  # noqa: E402


# === the beginning of the prompt ===


def test_the_tool_description_takes_no_per_turn_arguments():
    """The description is in the tools block, and the engine renders the
    tools block FIRST -- before the system message and the whole
    history. So a per-turn fact in the description moves the very
    beginning of the prompt, and the prefix the engine holds stops
    matching.

    The strongest form of the property is that the door is not there: the
    function takes nothing, so nothing can walk into the description.
    """
    params = list(inspect.signature(
        llm_delete.delete_messages_tool_def).parameters)
    assert params == [], (
        f"the delete tool's description must not be a function of the "
        f"turn, but it takes {params}")


def test_the_tool_description_is_the_same_bytes_on_every_turn():
    assert (llm_delete.delete_messages_tool_def()
            == llm_delete.delete_messages_tool_def())


def test_the_tool_list_builder_takes_no_per_turn_arguments():
    """Same rule for the whole list: the builder is handed the chat and
    the chat type, and nothing that changes from turn to turn."""
    params = set(inspect.signature(llm_tools.build_tool_defs).parameters)
    assert "reply_to_message_id" not in params, params
    assert "current_message_id" not in params, params


def test_the_ids_of_the_turn_travel_in_the_turn_context():
    """The model still gets the ids -- in the turn context, at the end of
    the user's message.

    Without them the model invents ids, and the bot is an administrator:
    an invented id is a real attempt to delete a stranger's message
    (the live failure of 2026-10-26).
    """
    block = llm_delete.turn_context_block(
        100, reply_to_message_id=699, current_message_id=703)
    desc = llm_delete.delete_messages_tool_def()["function"]["description"]
    assert "699" in block and "703" in block, block
    assert "699" not in desc and "703" not in desc, desc


# === the history that was already there ===


def test_the_context_only_ever_grows():
    """What was in the context stays in the context.

    A sliding window -- the newest N messages -- drops the oldest one on
    every turn, so the history in the prompt moves and the engine has
    nothing to match: `reused 0`, and the whole prompt read again
    (13 077 tokens in 40.2 s, measured on the production engine).
    """
    budget = cb.ContextBudget(anchor=0.0)
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(40)]
    for n in range(1, len(msgs) + 1):
        first = budget.apply(msgs[:n])
        later = budget.apply(msgs)
        assert later[:len(first)] == first, (
            f"the context moved when the {n}th message arrived: what was "
            f"in it is not in it any more")


def test_the_fold_drops_the_oldest_and_keeps_the_newest():
    """The fold is the price of the reuse, and it is spent on the OLDEST
    messages: the newest turn is the reason for the call."""
    budget = cb.ContextBudget(anchor=0.0, token_budget=100)
    msgs = [{"role": "user", "content": "x" * 200} for _ in range(10)]
    out = budget.apply(msgs)
    assert out, "the fold dropped everything"
    assert out[-1] is msgs[-1], "the newest message was folded out"
    assert out[0] is msgs[-len(out)], "the fold kept something else"


def test_a_message_alone_over_the_budget_is_still_kept():
    """A prompt with nothing to answer is worse than a slow one."""
    budget = cb.ContextBudget(anchor=0.0, token_budget=10)
    msgs = [{"role": "user", "content": "x" * 5000}]
    out = budget.apply(msgs)
    assert out == msgs


# === the render, live and from the database ===


def test_the_turn_context_never_reaches_the_engine_as_a_field():
    """It is metadata, not a message field. The engine must not see a
    key it does not know."""
    m = {"role": "user", "content": "hi", "_turn_context": "[turn context] x"}
    out = llm_messages._tag_sender(m)
    assert "_turn_context" not in out, out
    assert set(out) == {"role", "content"}, out


@pytest.mark.asyncio
async def test_the_render_from_the_database_is_the_same_as_the_live_one(
        tmp_path, monkeypatch):
    """The next turn re-renders the message from the database.

    If that render differed by a byte from the live one, the prefix the
    engine holds would stop matching ONE TURN LATER -- and the reuse
    would die with nothing in the logs to say why. So the turn context
    is stored with the row, not rebuilt at render time.
    """
    import persistence
    import storage as storage_mod
    from storage import Storage
    store = Storage(str(tmp_path / "t.db"))
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)

    ctx = llm_delete.turn_context_block(
        100, reply_to_message_id=699, current_message_id=703)
    await persistence.persist(100, "t", "user", "hello",
                              sender_name="Vasia",
                              telegram_message_id=703,
                              turn_context=ctx)
    history = await persistence.load_history(100, "t")
    assert len(history) == 1, history
    from_db = llm_messages._tag_sender(history[0])["content"]
    live = llm_messages._tag_sender({
        "role": "user", "content": "hello", "_sender_name": "Vasia",
        "_telegram_message_id": 703, "_turn_context": ctx})["content"]
    assert from_db == live, (
        f"the re-render differs from the live one:\n{from_db!r}\n{live!r}")


@pytest.mark.asyncio
async def test_the_record_keeps_the_users_own_text(tmp_path, monkeypatch):
    """SQLite is the source of truth, and the training data is made of
    it. The turn context travels BESIDE the text, never inside it."""
    import persistence
    import storage as storage_mod
    from storage import Storage
    store = Storage(str(tmp_path / "t.db"))
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)

    ctx = llm_delete.turn_context_block(
        100, reply_to_message_id=699, current_message_id=703)
    await persistence.persist(100, "t", "user", "hello",
                              sender_name="Vasia",
                              telegram_message_id=703,
                              turn_context=ctx)
    row = store.get_messages(100, "t", 10)[0]
    envelope = json.loads(row["content"])
    assert envelope["content"] == "hello", envelope
    assert envelope["_turn_context"] == ctx, envelope


# === the whole thing, stated as one property ===


def _prompt(messages, tools=None, system="system rules"):
    """The prompt as the engine sees it: the tools block, then the system
    message, then the messages -- in that order, because that is the
    order the engine's template renders them."""
    parts = [json.dumps(tools if tools is not None else [],
                        sort_keys=True, ensure_ascii=False), system]
    for m in messages:
        parts.append(json.dumps(llm_messages._tag_sender(m),
                                sort_keys=True, ensure_ascii=False))
    return "\n".join(parts)


def test_the_prompt_of_the_next_turn_starts_with_the_prompt_of_the_last():
    """The reuse, written down.

    The engine keeps the prompt of the last turn and matches the new one
    against it prefix by prefix, up to the last turn marker. So the
    prompt of the last turn has to be a PREFIX of the prompt of this
    one. Everything else in this file is a way of keeping that true.
    """
    budget = cb.ContextBudget(anchor=0.0)
    tools = [llm_delete.delete_messages_tool_def()]
    history = [{"role": "user", "content": f"m{i}", "_sender_name": "Vasia",
                "_telegram_message_id": 100 + i} for i in range(30)]
    last = _prompt(budget.apply(history), tools)
    now = _prompt(budget.apply(history + [
        {"role": "user", "content": "m30", "_sender_name": "Vasia",
         "_telegram_message_id": 130}]), tools)
    assert now.startswith(last), (
        "the prompt of this turn does not start with the prompt of the "
        "last one: the engine has nothing to reuse")


@pytest.mark.asyncio
async def test_the_prompt_of_the_next_turn_starts_with_the_prompt_of_the_last_(
        tmp_path, monkeypatch):
    """The same property, through the database.

    The history the engine is given comes from the record, one turn
    later. This is the version that runs in production: the messages are
    read back, not carried in memory.
    """
    import persistence
    import storage as storage_mod
    from storage import Storage
    store = Storage(str(tmp_path / "t.db"))
    monkeypatch.setattr(storage_mod, "get_store", lambda: store)
    monkeypatch.setattr(persistence, "get_budget",
                        lambda: cb.ContextBudget(anchor=0.0))

    for i in range(10):
        ctx = llm_delete.turn_context_block(
            100, reply_to_message_id=None, current_message_id=100 + i)
        await persistence.persist(100, "t", "user", f"m{i}",
                                  sender_name="Vasia",
                                  telegram_message_id=100 + i,
                                  turn_context=ctx)
    tools = [llm_delete.delete_messages_tool_def()]

    history = await persistence.load_history(100, "t")
    last = _prompt(history, tools)
    now = _prompt(history + [{
        "role": "user", "content": "m10", "_sender_name": "Vasia",
        "_telegram_message_id": 110,
        "_turn_context": llm_delete.turn_context_block(
            100, reply_to_message_id=None, current_message_id=110),
    }], tools)
    assert now.startswith(last)
