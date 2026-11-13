from collections import Counter, defaultdict
from dataclasses import dataclass
from math import cos, hypot, pi, sin

from repopilot.python_facts import PythonFacts, Span

NEIGHBORS_PER_PAGE = 8


@dataclass(frozen=True, order=True)
class Node:
    name: str
    path: str


@dataclass(frozen=True)
class Edge:
    source: Node
    target: Node
    sites: tuple[Span, ...]


@dataclass(frozen=True)
class Neighborhood:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    page: int
    pages: int

    def positions(self) -> dict[Node, tuple[int, int]]:
        if len(self.nodes) == 1:
            return {self.nodes[0]: (300, 150)}
        positions = {}
        for index, node in enumerate(self.nodes):
            angle = 2 * pi * index / len(self.nodes) - pi / 2
            positions[node] = (
                round(300 + 155 * cos(angle)),
                round(150 + 103 * sin(angle)),
            )
        return positions


class ModuleGraph:
    def __init__(self, facts: PythonFacts) -> None:
        self.nodes = {m.span.path: Node(m.name, m.span.path) for m in facts.modules}
        counts = Counter(n.name for n in self.nodes.values())
        targets = {n.name: n for n in self.nodes.values() if counts[n.name] == 1}
        sites: dict[tuple[Node, Node], set[Span]] = defaultdict(set)
        for module in facts.modules:
            source = self.nodes[module.span.path]
            for item in module.imports:
                if item.kind == "local" and not item.dynamic and item.target in targets:
                    sites[source, targets[item.target]].add(item.span)
        self.edges = tuple(
            Edge(source, target, tuple(sorted(spans, key=lambda s: (s.start, s.end))))
            for (source, target), spans in sorted(sites.items())
        )

    def neighborhood(self, path: str, page: int = 1) -> Neighborhood:
        selected = self.nodes[path]
        if len(self.nodes) <= NEIGHBORS_PER_PAGE + 1:
            return Neighborhood(tuple(sorted(self.nodes.values())), self.edges, 1, 1)
        neighbors = sorted(
            {e.target for e in self.edges if e.source == selected}.union(
                e.source for e in self.edges if e.target == selected
            ).difference({selected})
        )
        pages = max(1, (len(neighbors) + NEIGHBORS_PER_PAGE - 1) // NEIGHBORS_PER_PAGE)
        page = min(max(1, page), pages)
        offset = (page - 1) * NEIGHBORS_PER_PAGE
        visible = {selected, *neighbors[offset : offset + NEIGHBORS_PER_PAGE]}
        edges = tuple(
            e for e in self.edges if e.source in visible and e.target in visible
        )
        return Neighborhood(tuple(sorted(visible)), edges, page, pages)


def arrow(start: tuple[int, int], end: tuple[int, int]) -> str:
    x, y = start
    if start == end:
        return f"M{x - 6},{y - 6} c-23,-30 23,-30 12,-1"
    dx, dy = end[0] - x, end[1] - y
    length = hypot(dx, dy)
    ux, uy = dx / length, dy / length
    # Bending each directed edge to its left keeps reciprocal imports distinct.
    cx, cy = x + dx / 2 - uy * 13, y + dy / 2 + ux * 13
    return (
        f"M{x + ux * 9:.1f},{y + uy * 9:.1f} Q{cx:.1f},{cy:.1f} "
        f"{end[0] - ux * 12:.1f},{end[1] - uy * 12:.1f}"
    )
