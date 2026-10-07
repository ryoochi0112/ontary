"""The static import graph of ``src/ontary`` has no cycles."""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
ROOT_PKG = "ontary"


def _modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in sorted((SRC / ROOT_PKG).rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def _targets(name: str, path: Path, tree: ast.AST, modules: dict[str, Path]) -> set[str]:
    is_package = path.name == "__init__.py"
    package = name if is_package else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".")
                if node.level > 1:
                    anchor = anchor[: -(node.level - 1)]
                base = ".".join(anchor)
                if node.module:
                    base = f"{base}.{node.module}"
            else:
                base = node.module or ""
            if base not in modules:
                continue
            for alias in node.names:
                sub = f"{base}.{alias.name}"
                found.add(sub if sub in modules else base)
    found.discard(name)
    return found


def _graph() -> dict[str, set[str]]:
    modules = _modules()
    return {
        name: _targets(name, path, ast.parse(path.read_text()), modules)
        for name, path in modules.items()
    }


def _shortest_path(graph: dict[str, set[str]], start: str, goal: str) -> list[str] | None:
    queue: deque[list[str]] = deque([[start]])
    seen = {start}
    while queue:
        path = queue.popleft()
        for nxt in sorted(graph[path[-1]]):
            if nxt == goal:
                return [*path, nxt]
            if nxt not in seen:
                seen.add(nxt)
                queue.append([*path, nxt])
    return None


def _cycles(graph: dict[str, set[str]]) -> list[str]:
    found: dict[tuple[str, ...], str] = {}
    for a in sorted(graph):
        for b in sorted(graph[a]):
            back = _shortest_path(graph, b, a)
            if back is None:
                continue
            cycle = [a, *back]
            ring = cycle[:-1]
            pivot = ring.index(min(ring))
            ring = ring[pivot:] + ring[:pivot]
            found[tuple(ring)] = " -> ".join([*ring, ring[0]])
    return sorted(found.values())


def test_import_graph_has_no_cycles() -> None:
    cycles = _cycles(_graph())
    assert not cycles, "import cycles in src/ontary:\n" + "\n".join(cycles)
