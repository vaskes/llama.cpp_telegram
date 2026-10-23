"""A size budget, with the exceptions written down instead of remembered.

CODEBOOK S-1 and S-6. Two claims, each falsifiable:

  * a function is at most 200 lines unless it is registered here, with a
    reason and a trigger;
  * a production module over 1000 lines is registered in the debt
    register, with its current size declared.

Why 200 and not "smaller is better": the number is not the point, the
*registration* is. `handlers.py` is 1316 lines and healthy -- four
handlers of one shape, where a fix to the skeleton breaks four call
sites identically. `handle_photo` at 245 and `handle_text` at 209 are
that shape. `call_llama` at 324 and `run_one` at 319 are not, and they
are registered as open debt, which is where the difference has to show
up.

The point of writing the exceptions down is the second half of the rule
in CODEBOOK section 5: an exception carries a date, a reason and a
trigger. An exception without a trigger is a permanent exemption, and a
permanent exemption is how a monolith gets to be a monolith.

The registry below is also checked for rot: a function that has since
shrunk below the limit must leave the registry, or the exemption
outlives the thing it was written for.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "telegram-bot"
DEBT = ROOT / "docs" / "tech-debt"

FUNCTION_LIMIT = 200
MODULE_LIMIT = 1000

# name -> (file, measured length, date, reason, trigger)
#
# The measured length is recorded on purpose. If the function grows, the
# number here stops matching and the test says so -- which is the point.
# A registry that silently goes stale is the same failure as a tripwire
# that was quietly disabled.
EXCEPTIONS = {
    "call_llama": (
        "call_llama.py",
        324,
        "2026-10-22",
        "Carries a DO NOT REFACTOR notice with its own trigger; the "
        "transport, the four response buffers and the abort state across "
        "iterations are three unrelated concerns in one body. Open debt "
        "item 03.",
        "Split when the engine transport can be moved behind a seam "
        "without the abort state crossing the boundary with it.",
    ),
    "run_one": (
        "llm_iteration.py",
        319,
        "2026-10-22",
        "One tool-loop iteration, but nine concerns in the body. Open "
        "debt item 08, which is the item about splitting exactly this.",
        "Split when debt item 08 is picked up; it names this function.",
    ),
    "handle_photo": (
        "handlers.py",
        245,
        "2026-10-23",
        "One concern (a photo) plus a media path. Same shape as the other "
        "three handlers, so the repetition is the point -- a fix to the "
        "skeleton breaks four call sites identically. Open debt item 13.",
        "Split only together with the other three handlers, never alone: "
        "one handler on its own is four shapes, not one.",
    ),
    "handle_text": (
        "handlers.py",
        209,
        "2026-10-23",
        "Not a debt item -- same shape as handle_photo, and registered only "
        "because it crossed 200 lines while nobody was looking. The reason "
        "it is here at all is that the crossing was invisible, which is "
        "what this file exists to catch.",
        "Split only together with the other three handlers, never alone.",
    ),
}


def _functions():
    """(file, name, length) for every function in the production tree."""
    out = []
    for path in sorted(BOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - compile() is the gate
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((path, node.name, node.end_lineno - node.lineno + 1))
    return out


FUNCTIONS = _functions()


def test_the_scan_found_the_functions():
    """A guard on the guard: if the walk stopped matching, every check
    below would pass by finding nothing."""
    names = {name for _, name, _ in FUNCTIONS}
    assert len(FUNCTIONS) > 300, (
        f"only found {len(FUNCTIONS)} functions -- the walk stopped matching")
    for known in ("call_llama", "run_one", "handle_photo", "handle_text"):
        assert known in names, f"{known} is not in the scan -- wrong walk?"


def test_no_function_exceeds_the_limit_unless_it_is_registered():
    """CODEBOOK S-1. A function over the limit that is not registered is
    the failure this file exists to prevent."""
    unregistered = [
        f"{path.relative_to(ROOT)}:{name} is {length} lines"
        for path, name, length in FUNCTIONS
        if length > FUNCTION_LIMIT and name not in EXCEPTIONS
    ]
    assert not unregistered, (
        "\n".join(unregistered)
        + f"\n\nOver {FUNCTION_LIMIT} lines. Either it is one concern and can "
        f"be split, or it is registered in EXCEPTIONS above with a date, a "
        f"reason and a trigger. A permanent exemption is how a monolith "
        f"becomes a monolith.")


def test_every_registered_exception_still_exists():
    """An exception for a function that was renamed or deleted is an
    exemption for nothing."""
    by_name = {}
    for path, name, length in FUNCTIONS:
        by_name.setdefault(name, []).append((path, length))
    missing = []
    for name, (filename, _, _, _, _) in EXCEPTIONS.items():
        hits = by_name.get(name, [])
        if not hits:
            missing.append(f"{name}: no such function in the tree")
            continue
        if not any(path.name == filename for path, _ in hits):
            missing.append(
                f"{name}: registered against {filename}, but it now lives in "
                f"{', '.join(sorted({p.name for p, _ in hits}))}")
    assert not missing, "\n".join(missing)


def test_registered_lengths_match_the_tree():
    """The measured length is a claim. A claim that is not checked goes
    stale, and the next reader believes it."""
    stale = []
    for name, (_, declared, _, _, _) in EXCEPTIONS.items():
        actual = [length for _, n, length in FUNCTIONS if n == name]
        if not actual:
            continue
        longest = max(actual)
        if longest != declared:
            stale.append(
                f"{name}: registry says {declared} lines, the tree says "
                f"{longest}")
    assert not stale, (
        "\n".join(stale)
        + "\n\nThe function grew or shrank. Update the number, and if it "
        "grew, ask whether the exception still says why it is allowed.")


def test_the_registry_has_not_rotted():
    """An exception for something that is now under the limit has
    outlived its reason."""
    rotted = [
        f"{name} is registered but is now {length} lines"
        for name, (_, _, _, _, _) in EXCEPTIONS.items()
        for length in [max((l for _, n, l in FUNCTIONS if n == name), default=0)]
        if length <= FUNCTION_LIMIT
    ]
    assert not rotted, (
        "\n".join(rotted)
        + "\n\nDrop the exception -- it no longer exempts anything, and a "
        "stale exemption is one more thing to believe without checking.")


def test_every_exception_carries_a_date_a_reason_and_a_trigger():
    """CODEBOOK section 5, step 5. An exception without a trigger is a
    permanent exemption."""
    incomplete = []
    for name, entry in EXCEPTIONS.items():
        filename, _, date, reason, trigger = entry
        if not date.strip():
            incomplete.append(f"{name}: no date")
        if len(reason.strip()) < 40:
            incomplete.append(f"{name}: reason is too thin to be a reason")
        if len(trigger.strip()) < 40:
            incomplete.append(f"{name}: no trigger, or a trigger that is not "
                              f"a condition")
    assert not incomplete, "\n".join(incomplete)


def test_the_registry_itself_is_not_the_place_a_new_exemption_goes():
    """A sanity check on this file: the four entries above were all
    written against the tree, with reasons that name the debt item they
    belong to -- or say plainly that they are not debt. If someone adds
    a fifth, the entry has to say which, or it is a preference and not
    a registration."""
    for name, (_, _, _, reason, _) in EXCEPTIONS.items():
        assert re.search(r"item \d+|not a debt item", reason, re.IGNORECASE), (
            f"{name}: the reason neither names the debt item this "
            f"exception belongs to nor says it is not debt")


# --- CODEBOOK S-6: a big module is registered in the debt register ---

DECLARED_SIZE = re.compile(r"<!--\s*current:\s*(?P<body>.*?)\s*-->")
ENTRY = re.compile(r"(?P<path>[\w./\-]+\.py)\s*=\s*(?P<lines>\d+)")


def _registered_sizes():
    """{module name: {declared size}} across the whole debt register."""
    out = {}
    for path in sorted(DEBT.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = DECLARED_SIZE.search(text)
        if not match:
            continue
        for rel, lines in ENTRY.findall(match.group("body")):
            out.setdefault(Path(rel).name, set()).add(int(lines))
    return out


REGISTERED = _registered_sizes()


def test_a_module_over_the_limit_is_in_the_debt_register():
    """CODEBOOK S-6. A module that grew past the limit without an entry
    in the register is exactly how the register stops describing the
    debt."""
    big = []
    for path in sorted(BOT.rglob("*.py")):
        length = len(path.read_text(encoding="utf-8").splitlines())
        if length > MODULE_LIMIT and path.name not in REGISTERED:
            big.append(f"{path.relative_to(ROOT)} is {length} lines")
    assert not big, (
        "\n".join(big)
        + f"\n\nOver {MODULE_LIMIT} lines and not in docs/tech-debt/. "
        f"Either it is under control and should be brought back under the "
        f"limit, or it is debt and belongs in the register with a date, a "
        f"reason and a trigger.")


def test_the_registered_sizes_of_big_modules_are_current():
    """A registered module whose declared size no longer matches the tree
    has drifted, and the drift is what the next reader believes."""
    drifted = []
    for name, sizes in sorted(REGISTERED.items()):
        candidates = list(BOT.rglob(name))
        if not candidates:
            continue
        actual = len(candidates[0].read_text(encoding="utf-8").splitlines())
        if actual > MODULE_LIMIT and actual not in sizes:
            drifted.append(
                f"{name}: register says {sorted(sizes)}, the tree says "
                f"{actual}")
    assert not drifted, (
        "\n".join(drifted)
        + "\n\nUpdate the 'current:' declaration in docs/tech-debt/.")


def test_the_module_limit_is_not_above_everything():
    """A budget that nothing can reach is not a budget."""
    lengths = [len(p.read_text(encoding="utf-8").splitlines())
               for p in BOT.rglob("*.py")]
    assert max(lengths) > MODULE_LIMIT, (
        f"the largest production module is {max(lengths)} lines, at or "
        f"under the {MODULE_LIMIT}-line limit -- the limit has been "
        f"raised to fit the tree instead of the other way round")
    assert min(lengths) < MODULE_LIMIT
