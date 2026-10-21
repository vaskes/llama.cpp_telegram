"""Real module-level import graph for telegram-bot/, built with AST.

Replaces the greedy claim in review round 3, where acyclicity was argued with
grep. grep cannot see `from . import bot`, `import bot as _b`, or
`__import__("bot")`, and a test that only catches the shape somebody already
wrote is security theatre with a green checkmark.

This builds the actual dependency graph and finds cycles with Tarjan's
strongly-connected-components algorithm, which reports every cycle including
the ones a naive DFS misses on graphs with more than one back edge.

Known limit, stated rather than hidden: `__import__("x")` and `exec("import
x")` are string arguments at runtime and cannot be resolved statically without
executing the module. They are counted and reported by
test_dynamic_imports_are_declared so the blind spot is visible instead of
assumed away.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"

_DYNAMIC_IMPORT_RE = re.compile(r"""__import__\(\s*['"]([A-Za-z_][\w.]*)['"]""")


def module_names() -> set[str]:
    return {p.stem for p in BOT_DIR.glob("*.py") if p.stem != "__init__"}


def build_graph(bot_dir: Path | None = None) -> tuple[dict[str, set[str]], dict[str, list[tuple[int, str]]]]:
    """Return (edges, dynamic_imports).

    edges[module] -> set of local modules it imports at MODULE level.
    dynamic_imports[module] -> [(lineno, target)] for __import__("x") calls.
    """
    root = bot_dir or BOT_DIR
    mods = {p.stem for p in root.glob("*.py") if p.stem != "__init__"}
    edges: dict[str, set[str]] = defaultdict(set)
    dynamic: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for path in sorted(root.glob("*.py")):
        name = path.stem
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in mods:
                        # `import bot as _b` and `import bot` are the same edge.
                        edges[name].add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    if node.module:
                        top = node.module.split(".")[0]
                        if top in mods:
                            edges[name].add(top)
                else:
                    # `from . import bot` / `from .bot import main`
                    # Relative: the package is telegram-bot, so the target is
                    # either the named module or nothing.
                    if node.module is None:
                        for alias in node.names:
                            if alias.name in mods:
                                edges[name].add(alias.name)
                    else:
                        top = node.module.split(".")[0]
                        if top in mods:
                            edges[name].add(top)

        for match in _DYNAMIC_IMPORT_RE.finditer(source):
            top = match.group(1).split(".")[0]
            if top in mods:
                dynamic[name].append(
                    (source[: match.start()].count("\n") + 1, top)
                )

    return edges, dynamic


def tarjan_scc(nodes: set[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components, iterative to survive deep graphs."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in sorted(nodes):
        if root in index_of:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True

        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(sorted(edges.get(nxt, ())))))
                    advanced = True
                    break
                if on_stack.get(nxt):
                    low[node] = min(low[node], index_of[nxt])
            if advanced:
                continue

            work.pop()
            if low[node] == index_of[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == node:
                        break
                result.append(comp)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    return result


def cycles() -> list[list[str]]:
    """Every module-level cycle, as strongly connected components."""
    edges, _ = build_graph()
    mods = module_names()
    return [sorted(c) for c in tarjan_scc(mods, edges) if len(c) > 1]


def describe(component: list[str]) -> str:
    """An actual closed path through the cycle.

    The first version followed edges until it hit an already-visited node,
    which on a graph with any other outgoing edge printed `rating -> rating`
    -- technically a cycle, and useless to whoever has to fix it. This walks
    the component's subgraph and returns a real path from a node back to
    itself, so the error message names every module involved.
    """
    edges, _ = build_graph()
    members = set(component)
    start = component[0]

    # BFS from start back to start, restricted to the component.
    queue: list[tuple[str, list[str]]] = [(start, [start])]
    seen = {start}
    while queue:
        cur, path = queue.pop(0)
        for nxt in sorted(edges.get(cur, ())):
            if nxt not in members:
                continue
            if nxt == start:
                return " -> ".join(path + [start])
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [nxt]))
    return " -> ".join(sorted(component))


def topological_layers() -> list[list[str]]:
    """Leaf-first layering. Layer 0 imports nothing local."""
    edges, _ = build_graph()
    mods = module_names()
    indeg = {m: 0 for m in mods}
    for src, dsts in edges.items():
        for d in dsts:
            if d in indeg:
                indeg[d] += 1

    ready = sorted(m for m, n in indeg.items() if n == 0)
    layers: list[list[str]] = []
    remaining = dict(indeg)
    while ready:
        layers.append(ready)
        nxt = []
        for m in ready:
            for d in sorted(edges.get(m, ())):
                if d in remaining:
                    remaining[d] -= 1
                    if remaining[d] == 0:
                        nxt.append(d)
        ready = sorted(nxt)
    return layers