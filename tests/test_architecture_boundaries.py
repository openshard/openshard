"""Architecture boundaries for the ``openshard`` package.

Each rule below names a dependency direction the codebase relies on, for
example "the evidence core never imports the agent runtimes that produce
evidence" or "MCP only reads context, it never executes". The test parses
every module (no imports are executed), collects every ``openshard`` import
it makes -- module level, inside functions, under ``TYPE_CHECKING`` and
``importlib.import_module("openshard...")`` -- and fails on any edge a rule
forbids.

Existing violations are recorded in ``KNOWN_EXCEPTIONS`` with the reason
they exist. The list is a ratchet: a new violation fails, and so does an
exception that no longer occurs, so fixing one forces the entry out and it
can never silently come back. Shrink the list; do not grow it without a
reason a reviewer can accept.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "openshard"

ROOT = "(root)"  # ``openshard/__init__.py`` itself
INTERFACES = frozenset({"cli", "tui"})
RUNTIMES = frozenset({"run", "osn", "native", "execution", "planning"})
FOUNDATIONS = frozenset({ROOT, "util", "safety", "security", "analysis"})


@dataclass(frozen=True)
class Rule:
    name: str
    sources: frozenset[str]
    why: str
    forbidden: frozenset[str] = frozenset()
    # When set, ``sources`` may import only these packages (plus themselves).
    allowed_only: frozenset[str] | None = None

    def forbids(self, source: str, target: str) -> bool:
        if source not in self.sources or source == target:
            return False
        if self.allowed_only is not None:
            return target not in self.allowed_only
        return target in self.forbidden


RULES: tuple[Rule, ...] = (
    Rule(
        "interfaces-are-leaves",
        sources=frozenset(),  # filled in below: every non-interface package
        forbidden=INTERFACES,
        why="The CLI and TUI present results. Nothing below them may depend on "
        "how a result is rendered or on Click/Textual being importable.",
    ),
    Rule(
        "foundations-are-leaves",
        sources=FOUNDATIONS - {ROOT},
        allowed_only=FOUNDATIONS,
        why="Path, secret and shell safety and shared utilities must stay "
        "importable from anywhere without pulling in the product.",
    ),
    Rule(
        "evidence-core-is-independent",
        sources=frozenset({"history"}),
        forbidden=INTERFACES
        | RUNTIMES
        | frozenset(
            {
                "providers",
                "adapters",
                "sync",
                "remote",
                "connected",
                "mcp",
                "learning",
                "insights",
                "routing",
                "evals",
            }
        ),
        why="Shards and Receipts are the system of record. Capture, runtimes, "
        "delivery and intelligence produce or consume them; the record must "
        "not depend on any of them.",
    ),
    Rule(
        "capture-does-not-execute",
        sources=frozenset({"adapters"}),
        forbidden=INTERFACES | RUNTIMES | frozenset({"providers", "scoring", "evals"}),
        why="Capture observes external agents. It never dispatches a model or "
        "runs an OSN task, so hooks stay cheap and observation is never "
        "mistaken for execution.",
    ),
    Rule(
        "mcp-is-read-only",
        sources=frozenset({"mcp"}),
        forbidden=INTERFACES
        | RUNTIMES
        | frozenset({"providers", "adapters", "remote", "connected", "evals"}),
        why="MCP supplies context to agents. It is not an evidence source and "
        "never executes or captures.",
    ),
    Rule(
        "intelligence-does-not-dispatch",
        sources=frozenset({"learning", "routing", "insights", "reflection", "policy"}),
        forbidden=INTERFACES | frozenset({"providers", "execution", "run", "native"}),
        why="Learning, routing and policy decide; only the execution layer "
        "talks to model providers, so every model call goes through the one "
        "dispatch path that records it.",
    ),
    Rule(
        "delivery-does-not-run-agents",
        sources=frozenset({"sync", "remote", "connected"}),
        forbidden=INTERFACES | RUNTIMES | frozenset({"providers"}),
        why="Cloud delivery moves finished evidence. It must not start or "
        "depend on an agent runtime.",
    ),
)

# (rule, importer module, imported module) -> why the edge exists today.
KNOWN_EXCEPTIONS: dict[tuple[str, str, str], str] = {
    (
        "interfaces-are-leaves",
        "openshard.run.pipeline",
        "openshard.cli.run_output",
    ): "RunPipeline renders its own progress and summary through cli.run_output.",
    (
        "evidence-core-is-independent",
        "openshard.history.jsonl_store",
        "openshard.learning.worker",
    ): "Appending to runs.jsonl schedules a learning refresh (best-effort, lazy import).",
    (
        "delivery-does-not-run-agents",
        "openshard.sync.policies",
        "openshard.osn.budget",
    ): "Synced organisation policy is parsed into OSN BudgetLimits.",
}

# Module-level import cycles that exist today. There are none; keep it that way.
KNOWN_CYCLES: frozenset[frozenset[str]] = frozenset()


# ---------------------------------------------------------------------------
# Import graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    importer: str
    imported: str
    path: str
    line: int
    eager: bool  # executed when the importer is imported


def _module_table(root: Path, package: str) -> dict[str, Path]:
    table: dict[str, Path] = {}
    for path in sorted((root / package).rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        table[".".join(parts)] = path
    return table


def _longest_known(name: str, modules: dict[str, Path]) -> str | None:
    while name and name not in modules:
        name = name.rpartition(".")[0]
    return name or None


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, path: Path, modules: dict[str, Path], package: str):
        self.module = module
        self.is_package = path.name == "__init__.py"
        self.modules = modules
        self.package = package
        self.rel = path.as_posix()
        self.edges: list[Edge] = []
        self._depth = 0  # >0 inside a function body or TYPE_CHECKING block

    def _add(self, names: list[str], line: int) -> None:
        for name in names:
            if name != self.package and not name.startswith(self.package + "."):
                continue
            target = _longest_known(name, self.modules)
            if target and target != self.module:
                self.edges.append(Edge(self.module, target, self.rel, line, self._depth == 0))

    def _nested(self, node: ast.AST) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _nested

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking(node.test):
            self.generic_visit(node)
            return
        self._depth += 1
        for child in node.body:
            self.visit(child)
        self._depth -= 1
        for child in node.orelse:
            self.visit(child)

    def visit_Import(self, node: ast.Import) -> None:
        self._add([alias.name for alias in node.names], node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            base = self.module.split(".")
            if not self.is_package:
                base = base[:-1]
            base = base[: len(base) - (node.level - 1)]
            target = ".".join(base + ([node.module] if node.module else []))
        else:
            target = node.module or ""
        # ``from openshard import cli`` names a submodule, not an attribute of
        # the package, so the edge goes to the submodule alone.
        submodules = [
            f"{target}.{a.name}" for a in node.names if f"{target}.{a.name}" in self.modules
        ]
        self._add(submodules + ([target] if len(submodules) < len(node.names) else []), node.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if (
            name in {"import_module", "__import__"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self._add([node.args[0].value], node.lineno)
        self.generic_visit(node)


def collect_edges(root: Path = REPO_ROOT, package: str = PACKAGE) -> list[Edge]:
    modules = _module_table(root, package)
    edges: list[Edge] = []
    for module, path in modules.items():
        collector = _Collector(module, path, modules, package)
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        # One edge per (importer, imported, line): ``from x import a, b`` is one import.
        edges.extend(dict.fromkeys(collector.edges))
    return edges


def top_level(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else ROOT


def find_violations(
    edges: list[Edge], rules: tuple[Rule, ...]
) -> dict[tuple[str, str, str], list[Edge]]:
    found: dict[tuple[str, str, str], list[Edge]] = defaultdict(list)
    for edge in edges:
        source, target = top_level(edge.importer), top_level(edge.imported)
        for rule in rules:
            if rule.forbids(source, target):
                found[(rule.name, edge.importer, edge.imported)].append(edge)
    return found


def find_cycles(edges: list[Edge]) -> set[frozenset[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.eager:
            graph[edge.importer].add(edge.imported)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: set[frozenset[str]] = set()

    def visit(node: str) -> None:  # Tarjan's strongly connected components
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for nxt in graph.get(node, ()):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component = set()
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.add(member)
                if member == node:
                    break
            if len(component) > 1:
                cycles.add(frozenset(component))

    for node in list(graph):
        if node not in index:
            visit(node)
    return cycles


def _resolved_rules(packages: set[str]) -> tuple[Rule, ...]:
    first, *rest = RULES
    everyone_but_interfaces = frozenset(packages - INTERFACES)
    return (Rule(first.name, everyone_but_interfaces, first.why, first.forbidden), *rest)


@pytest.fixture(scope="module")
def edges() -> list[Edge]:
    return collect_edges()


@pytest.fixture(scope="module")
def packages() -> set[str]:
    return {top_level(m) for m in _module_table(REPO_ROOT, PACKAGE)}


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_every_rule_names_real_packages(packages: set[str]) -> None:
    # A renamed package must not silently switch a rule off.
    for rule in RULES:
        named = rule.sources | rule.forbidden | (rule.allowed_only or frozenset())
        missing = named - packages
        assert not missing, f"rule {rule.name!r} names unknown packages {sorted(missing)}"


def test_no_new_boundary_violations(edges: list[Edge], packages: set[str]) -> None:
    violations = find_violations(edges, _resolved_rules(packages))
    new = {key: hits for key, hits in violations.items() if key not in KNOWN_EXCEPTIONS}
    if new:
        why = {rule.name: rule.why for rule in RULES}
        lines = []
        for (rule, importer, imported), hits in sorted(new.items()):
            where = ", ".join(f"{h.path}:{h.line}" for h in hits)
            lines.append(f"[{rule}] {importer} -> {imported} ({where})\n    {why[rule]}")
        pytest.fail(
            "New architecture boundary violations. Move the code to the layer that "
            "owns it, or invert the dependency:\n" + "\n".join(lines)
        )


def test_known_exceptions_still_occur(edges: list[Edge], packages: set[str]) -> None:
    violations = find_violations(edges, _resolved_rules(packages))
    stale = sorted(set(KNOWN_EXCEPTIONS) - set(violations))
    assert not stale, (
        "These boundary exceptions no longer occur. Delete them from "
        f"KNOWN_EXCEPTIONS so they cannot come back: {stale}"
    )


def test_no_new_import_cycles(edges: list[Edge]) -> None:
    cycles = find_cycles(edges)
    new = cycles - KNOWN_CYCLES
    stale = KNOWN_CYCLES - cycles
    assert not new, f"New module-level import cycles: {[sorted(c) for c in new]}"
    assert not stale, f"Cycles fixed; delete them from KNOWN_CYCLES: {[sorted(c) for c in stale]}"


# ---------------------------------------------------------------------------
# The scanner itself: each way of spelling an import must be seen.
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "source, eager",
    [
        ("import pkg.cli.out\n", True),
        ("from pkg.cli import out\n", True),
        ("from pkg import cli\n", True),
        ("from ..cli import out\n", True),
        ("from ..cli.out import render as r\n", True),
        ("def f():\n    from pkg.cli.out import render\n", False),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg.cli import out\n",
            False,
        ),
        ("import importlib\nimportlib.import_module('pkg.cli.out')\n", True),
        ("try:\n    from pkg.cli import out\nexcept ImportError:\n    out = None\n", True),
    ],
)
def test_scanner_sees_every_import_spelling(tmp_path: Path, source: str, eager: bool) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/cli/__init__.py", "")
    _write(tmp_path, "pkg/cli/out.py", "def render(): ...\n")
    _write(tmp_path, "pkg/history/__init__.py", "")
    _write(tmp_path, "pkg/history/store.py", source)

    found = [e for e in collect_edges(tmp_path, "pkg") if e.importer == "pkg.history.store"]

    assert found, f"scanner missed: {source!r}"
    assert {top_level(e.imported) for e in found} == {"cli"}
    assert all(e.eager is eager for e in found)
    rule = Rule("t", frozenset({"history"}), "t", forbidden=frozenset({"cli"}))
    assert find_violations(found, (rule,))


def test_cycle_detection_ignores_function_level_imports(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "from pkg import b\n")
    _write(tmp_path, "pkg/b.py", "def f():\n    from pkg import a\n")
    _write(tmp_path, "pkg/c.py", "from pkg import d\n")
    _write(tmp_path, "pkg/d.py", "from pkg import c\n")

    assert find_cycles(collect_edges(tmp_path, "pkg")) == {frozenset({"pkg.c", "pkg.d"})}
