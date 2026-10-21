"""T70 — tests for context_budget.

Three groups:

1. Behaviour of ContextBudget.apply() — pure, no I/O, no clock.
2. Tripwires for the two claims that are easy to state and easy to break:
   the anchor is a module constant (NOT a per-request recomputation), and
   storage no longer deletes history.
3. Overflow classification (T70.4).

The tripwires exist because the failure they guard is invisible. If the
anchor slides instead of staying put, every test that passes real messages
through a fresh ContextBudget() still passes — the bug only appears in
production, weeks later, as "the bot forgot the beginning of our chat". A
claim in a docstring is not a guard; these are.
"""
import ast
import re
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import context_budget as cb  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400.0


def msg(age_seconds: float | None = 0.0, text: str = "x") -> dict:
    m = {"role": "user", "content": text}
    if age_seconds is not None:
        m["_created_at"] = NOW - age_seconds
    return m


# ===========================================================================
# 1. Behaviour
# ===========================================================================

def test_window_keeps_messages_inside_the_window():
    b = cb.ContextBudget(strategy="window", max_messages=100, anchor=NOW - 7 * DAY)
    keep = msg(age_seconds=6 * DAY)
    drop = msg(age_seconds=8 * DAY)
    out = b.apply([keep, drop])
    assert out == [keep]
    assert out[0]["_created_at"] == keep["_created_at"]


def test_window_boundary_is_inclusive():
    """Exactly at the anchor is inside the window — off-by-one in either
    direction would silently drop or resurrect one message per call.

    The anchor is passed explicitly rather than left to the default: the
    default derives from `time.time()`, so a boundary computed against
    NOW here would be compared against a real-clock anchor there and the
    test would drift with wall time.
    """
    b = cb.ContextBudget(anchor=NOW - 7 * DAY)
    at_boundary = msg(age_seconds=7 * DAY)
    assert b.apply([at_boundary]) == [at_boundary]
    just_before = msg(age_seconds=7 * DAY + 1)
    assert b.apply([just_before]) == [just_before]
    just_inside = msg(age_seconds=7 * DAY - 1)
    assert b.apply([just_inside]) == [just_inside]


def test_remember_days_zero_disables_age_filter():
    b = cb.ContextBudget(anchor=cb._anchor_for_days(0, now=NOW))
    ancient = msg(age_seconds=3650 * DAY)
    assert b.apply([ancient]) == [ancient]
    assert b.age_cutoff_active() is False


def test_message_cap_takes_the_newest():
    b = cb.ContextBudget(anchor=0.0, max_messages=3)
    msgs = [msg(text=str(i)) for i in range(10)]
    out = b.apply(msgs)
    assert len(out) == 3
    assert [m["content"] for m in out] == ["7", "8", "9"]


def test_missing_timestamp_is_kept_not_dropped():
    """An unknown age must not delete history — that is the whole point of
    the feature. Dropping it would be silent data loss."""
    b = cb.ContextBudget(anchor=NOW - DAY)
    no_ts = {"role": "user", "content": "legacy"}
    out = b.apply([no_ts, msg(age_seconds=100)])
    assert no_ts in out


def test_unparseable_timestamp_is_kept():
    b = cb.ContextBudget(anchor=NOW - DAY)
    bad = {"role": "user", "content": "weird", "_created_at": "not-a-number"}
    assert bad in b.apply([bad, msg(age_seconds=100)])


def test_last_message_never_dropped_even_if_older_than_the_window():
    """The incoming message reaches the engine ONLY through this list.

    call_llama builds `msgs = [sys] + messages` and its `user_text`
    parameter is dead code, so if apply() removed the newest message the
    model would answer with nothing to answer. This floor is one message,
    deliberately not the open T66.1 question.
    """
    b = cb.ContextBudget(anchor=NOW + DAY)  # anchor in the future: everything is old
    recent = msg(age_seconds=1000)
    out = b.apply([recent])
    assert out == [recent]


def test_out_of_order_arrival_does_not_wipe_the_window():
    """Regression: the in-window history must survive when the newest
    message is older than the window.

    This happens after every restart cycle — the anchor moves forward while
    a thread's last rows sit in the past. An earlier draft of the
    "last message is never dropped" rule tested `selected[-1] is not
    items[-1]`, which is true here, and then returned `items[-1:]`: the whole
    in-window history was discarded and one stale message was sent
    instead. Found by the test below rather than by reasoning.
    """
    b = cb.ContextBudget(max_messages=100, anchor=NOW - 7 * DAY)
    inside_old = msg(age_seconds=6 * DAY, text="inside")
    stale_newest = msg(age_seconds=9 * DAY, text="stale")
    out = b.apply([inside_old, stale_newest])
    assert [m["content"] for m in out] == ["inside"], (
        "an out-of-window newest message must not wipe in-window history"
    )


def test_empty_window_falls_back_to_newest_message():
    """Everything older than the window, including the newest message:
    send the newest rather than an empty context."""
    b = cb.ContextBudget(max_messages=100, anchor=NOW - 7 * DAY)
    old1 = msg(age_seconds=8 * DAY, text="old1")
    old2 = msg(age_seconds=9 * DAY, text="old2")
    out = b.apply([old1, old2])
    assert out == [old2]


def test_empty_input_returns_empty():
    assert cb.ContextBudget().apply([]) == []


def test_apply_does_not_mutate_the_input():
    b = cb.ContextBudget(anchor=0.0)
    msgs = [msg(text="a"), msg(text="b")]
    before = [dict(m) for m in msgs]
    b.apply(msgs)
    assert msgs == before


def test_unknown_strategy_falls_back_to_window_not_crash():
    """A typo in .env must not stop the bot from starting."""
    b = cb.ContextBudget(strategy="wtf", anchor=NOW - DAY)
    assert b.strategy == "window"


def test_split_for_summary_partitions_by_age():
    b = cb.ContextBudget(anchor=NOW - 7 * DAY)
    old = msg(age_seconds=10 * DAY, text="old")
    new = msg(age_seconds=1 * DAY, text="new")
    inside, older = b.split_for_summary([old, new])
    assert [m["content"] for m in inside] == ["new"]
    assert [m["content"] for m in older] == ["old"]


def test_split_for_summary_with_no_cutoff_returns_everything_as_inside():
    b = cb.ContextBudget(anchor=0.0)
    inside, older = b.split_for_summary([msg(text="a")])
    assert len(inside) == 1 and older == []


def test_describe_exposes_the_policy():
    d = cb.ContextBudget(strategy="window", max_messages=20, anchor=123.0).describe()
    assert d == {"strategy": "window", "max_messages": 20,
                 "remember_days": 7, "anchor": 123.0}


def test_get_budget_returns_the_module_instance():
    assert isinstance(cb.get_budget(), cb.ContextBudget)


# ===========================================================================
# 2. Tripwires — the claims that break silently
# ===========================================================================

def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def test_anchor_is_a_module_level_constant():
    """CONTEXT_ANCHOR must be assigned once at module level.

    If it moves inside apply() or a method, the window slides: the bot
    forgets the start of every live conversation, one day at a time, with
    no error anywhere. Nothing in the behavioural tests above would catch
    it, because they construct their own ContextBudget.
    """
    tree = _tree(BOT_DIR / "context_budget.py")
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "CONTEXT_ANCHOR" for t in node.targets)
    ]
    assert len(assignments) == 1, (
        f"CONTEXT_ANCHOR must be assigned exactly once at module level, "
        f"found {len(assignments)} module-level assignments"
    )
    # Reassignment is now allowed — but ONLY inside reanchor(). T68 needs it:
    # `soft` mode re-anchors without restarting the process. What must stay
    # impossible is any other writer, because that is how the window starts
    # sliding. So: assert the writer set is exactly {module level, reanchor}.
    writers = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n]
            if any(getattr(t, "id", None) == "CONTEXT_ANCHOR"
                   for t in targets if isinstance(t, ast.Name)):
                if isinstance(n, ast.Assign) and n in assignments:
                    writers.add("<module>")
                    continue
                # Find the enclosing function.
                fn = _enclosing_function(tree, n)
                writers.add(fn.name if fn else "<module>")
    assert writers == {"<module>", "reanchor"}, (
        f"CONTEXT_ANCHOR may only be assigned at module level and inside "
        f"reanchor(), found writers: {sorted(writers)}. Any other writer risks "
        f"turning the anchored window into a sliding one."
    )


def _enclosing_function(tree, target):
    """Innermost FunctionDef whose body contains `target`, or None."""
    best = None
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(node is target for node in ast.walk(fn)):
            # Innermost wins: a nested def inside another still resolves to
            # itself, which is what we want for reanchor().
            if best is None or fn.lineno > best.lineno:
                best = fn
    return best


def test_apply_does_not_call_time_time():
    """apply() must not read the clock.

    A clock read inside apply() is exactly how a fixed window becomes a
    sliding one, no matter where the anchor constant lives.
    """
    tree = _tree(BOT_DIR / "context_budget.py")
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            if fn.name != "apply":
                continue
            called = {
                node.func.attr
                for node in ast.walk(fn)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            assert "time" not in called, (
                "ContextBudget.apply() calls time.* — the window would slide "
                "instead of staying anchored"
            )


def test_apply_is_pure_no_io_no_llm():
    """apply() must not touch the network, the DB, or an LLM.

    The summarize strategy needs an LLM round-trip; that is why the caller
    does it and passes the result in. Keeping apply() pure is what makes it
    testable without network or DB.
    """
    tree = _tree(BOT_DIR / "context_budget.py")
    forbidden = {"open", "urlopen", "read", "write", "to_thread", "run",
                 "post", "get", "request"}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            if fn.name != "apply":
                continue
            called = {
                node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Name, ast.Attribute))
            }
            bad = called & forbidden
            assert not bad, (
                f"ContextBudget.apply() performs I/O: {sorted(bad)}. "
                "It must stay pure so it can be tested without network or DB."
            )


def test_storage_no_longer_deletes_messages():
    """T67: the lazy trim is gone and must not come back.

    It DELETED rows above TRIM_HIGH at read time, with no log and no way to
    recover. The operator is keeping this history for LoRA training data, so
    silent deletion is data loss, not housekeeping. The old docstring
    described the trim as a self-throttling feature, which is exactly how
    someone would reinstate it six months from now.

    Checked on the AST, not on the source text. `delete_thread` (/reset) and
    `delete_last_user_message` (voice rollback) both legitimately issue
    DELETE FROM messages, and a substring search cannot tell those apart
    from the trim — it can only be silenced by removing the features the bot
    actually needs.

    What must not exist: a DELETE inside get_messages. That is where the trim
    lived, and it is the difference between "prune on read, silently" and
    "delete when the operator asked".
    """
    tree = _tree(BOT_DIR / "storage.py")
    methods = {
        node.name: node
        for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
        for node in cls.body if isinstance(node, ast.FunctionDef)
    }
    fn = methods["get_messages"]
    deletes = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(r"DELETE\s+FROM\s+messages", node.value, re.I)
    ]
    assert not deletes, (
        "get_messages deletes rows again — that is the lazy trim that "
        "destroyed LoRA training data. History is the source of truth; "
        "/reset (delete_thread) is the operator's explicit delete path."
    )

    # And the policy constants must not reappear as live assignments.
    consts = {
        t.id
        for node in tree.body if isinstance(node, ast.Assign)
        for t in node.targets if isinstance(t, ast.Name)
    }
    assert "TRIM_HIGH" not in consts
    assert "TRIM_LOW" not in consts


def test_get_messages_returns_created_at():
    """The age window needs the timestamp; without it every row is
    'unknown age' and the filter degrades into a no-op."""
    tree = _tree(BOT_DIR / "storage.py")
    fn = None
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for f in cls.body:
            if isinstance(f, ast.FunctionDef) and f.name == "get_messages":
                fn = f
    assert fn is not None, "get_messages disappeared from storage.py"
    selects = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "SELECT" in node.value.upper()
    ]
    assert selects, "get_messages has no SELECT to inspect"
    assert any("created_at" in s.value for s in selects), (
        "get_messages does not SELECT created_at — the age-based window "
        "cannot work without it"
    )


def test_persistence_uses_context_budget():
    """load_history must route through ContextBudget, otherwise the policy
    in the new module is dead code and history silently keeps LIMIT-only
    behaviour."""
    src = (BOT_DIR / "persistence.py").read_text()
    assert "context_budget" in src, (
        "persistence.py does not import context_budget — the new policy is "
        "not wired in"
    )
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_history"
    )
    names = {
        node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        for node in ast.walk(fn)
    }
    assert "apply" in names or "get_budget" in names, (
        "load_history never calls ContextBudget.apply()"
    )
    # It must no longer slice history with the old hardcoded constant. Checked
    # on the AST: the constant name still appears in a docstring explaining
    # that it is superseded, and a substring search would trip on that.
    body_names = {
        node.id for node in ast.walk(fn) if isinstance(node, ast.Name)
    }
    assert "CONTEXT_MESSAGES" not in body_names, (
        "load_history still limits history with CONTEXT_MESSAGES; the policy "
        "belongs to ContextBudget now"
    )


def test_handlers_do_not_persist_images():
    """T67.2: images participate in the current turn but are not stored.

    Also checks the photo still REACHES the model: since it is no longer in
    SQLite, handle_photo must append the turn explicitly. Losing the write
    without adding the append would make the bot blind to photos — the exact
    kind of half-fix this project has been bitten by repeatedly.
    """
    tree = _tree(BOT_DIR / "handlers.py")
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_photo"
    )
    calls = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_persist_message"
    ]
    assert not calls, (
        "handle_photo persists the image again — operator decided (2026-10-20) "
        "that images are not stored"
    )
    src = (BOT_DIR / "handlers.py").read_text()
    photo_fn_src = src[src.index("async def handle_photo"):src.index("async def handle_voice")]
    assert "current_turn" in photo_fn_src and "history = history + [current_turn]" in photo_fn_src, (
        "handle_photo neither persists the image nor appends it to the history "
        "it sends — the model would never see the photo"
    )


def test_rollbacks_removed_with_the_write():
    """The v0.9.1 Stop/crash rollbacks existed only to undo the image write.
    Left in place they would call delete_last_user_message for a message that
    was never written — deleting an unrelated earlier message instead."""
    src = (BOT_DIR / "handlers.py").read_text()
    photo_fn_src = src[src.index("async def handle_photo"):src.index("async def handle_voice")]
    assert "delete_last_user_message" not in photo_fn_src, (
        "handle_photo still rolls back a deleted image write — with T67.2 that "
        "would delete someone else's earlier message"
    )


# ===========================================================================
# 3. Overflow classification (T70.4)
# ===========================================================================

@pytest.mark.parametrize("text", [
    "the prompt exceeds the context window",
    "context length exceeded",
    "n_ctx is too small",
    "KV cache is full",
    "your prompt is too long",
    "reduce the length of the messages",
    "maximum context length is 262144",
])
def test_overflow_detected(text):
    assert cb.is_context_overflow(text) is True


@pytest.mark.parametrize("text", [
    "", None,
    "rate limit reached, try again later",
    "content filter blocked this request",
    "model not found",
    "connection reset by peer",
])
def test_not_overflow(text):
    assert cb.is_context_overflow(text) is False


def test_overflow_detection_is_case_insensitive():
    assert cb.is_context_overflow("CONTEXT WINDOW EXCEEDED") is True


def test_overflow_message_names_the_fix():
    """/reset is the one thing a user can do without waiting.

    This test used to also require "05:00" in the message. That was correct
    when re-anchoring happened by process restart, and became wrong the
    moment `soft` mode shipped: there, the window moves with no restart at
    all, so telling the user to wait for a 05:00 restart is advice that
    never pays off — on precisely the deployments where re-anchoring works
    best. See ADR-0003.
    """
    assert "/reset" in cb.OVERFLOW_MESSAGE
    assert "05:00" not in cb.OVERFLOW_MESSAGE, (
        "do not promise a restart time: in BOT_REANCHOR_MODE=soft there is no "
        "restart, and the anchor moves on its own schedule"
    )


# The modules that turn an empty-choices response into a user-visible
# answer. TECH-DEBT #3 Block C1 moved one of the three sites out of
# call_llama.py into llm_nonstream.py; the classification is unchanged,
# the file is not. Counting in one file asked "where does the code live"
# when the question was "does every site classify overflow".
_OVERFLOW_SITE_MODULES = ("call_llama.py", "llm_nonstream.py")


def test_call_llama_imports_the_overflow_helper():
    src = (BOT_DIR / "call_llama.py").read_text()
    assert "from context_budget import" in src
    assert "OVERFLOW_MESSAGE" in src

    total = sum(
        (BOT_DIR / name).read_text(encoding="utf-8").count("OVERFLOW_MESSAGE")
        for name in _OVERFLOW_SITE_MODULES
        if (BOT_DIR / name).exists()
    )
    assert total >= 3, (
        f"all three empty-choices sites should classify overflow, not just "
        f"one. Found {total} across {list(_OVERFLOW_SITE_MODULES)}: "
        f"if one of them stopped distinguishing context exhaustion from a "
        f"rate limit, the bot answers a truncated conversation as if it "
        f"were complete (T70.4)."
    )


def test_every_empty_choices_site_classifies_overflow():
    """The intent of the count above, stated directly.

    `is_context_overflow(...)` guards every site that handles an empty
    choices list. A site that returns a message without consulting it
    looks identical to a correct one to a substring count -- which is why
    the count alone was never the real check.
    """
    for name in _OVERFLOW_SITE_MODULES:
        path = BOT_DIR / name
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        if "_choices" not in src and '"choices"' not in src:
            continue
        guard_sites = src.count("is_context_overflow(")
        empty_sites = len(re.findall(r"not _choices|\[\"choices\"\]\)\s*\n\s*if", src))
        assert guard_sites >= 1, (
            f"{name} reads choices but never calls is_context_overflow"
        )


# ===========================================================================
# 4. Config surface
# ===========================================================================

def test_env_vars_are_read_and_documented():
    """A variable that exists only in code is a trap: forks set it and see
    nothing happen. This is the same class of drift the T63 env-name
    tripwire was written for."""
    src = (BOT_DIR / "context_budget.py").read_text()
    example = (BOT_DIR / ".env.example").read_text()
    for var in ("BOT_REMEMBER_DAYS", "CONTEXT_MAX_MESSAGES", "CONTEXT_STRATEGY"):
        assert var in src, f"{var} is not read by context_budget"
        assert var in example, f"{var} is not in .env.example"


def test_remember_days_docstring_says_length_not_lookback():
    """The env var is the window LENGTH. Written as 'how many days back',
    a future maintainer computes `now - N days` per request and turns the
    anchored window into a sliding one — the exact silent bug the anchor
    tripwire guards.

    Reads the comment block ABOVE the assignment; the explanatory text lives
    there, not below it.
    """
    example = (BOT_DIR / ".env.example").read_text()
    at = example.index("BOT_REMEMBER_DAYS=7")
    block = example[max(0, at - 900):at]
    assert "LENGTH" in block or "length" in block, (
        "BOT_REMEMBER_DAYS is not documented as the window LENGTH"
    )
    assert "does NOT slide" in block, (
        "the anchored-vs-sliding distinction must be stated in .env.example, "
        "not only in context_budget.py — that is where a fork operator reads it"
    )