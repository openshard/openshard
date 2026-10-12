"""Measure the maintainability signals ``tests/test_code_health_ratchet.py`` budgets.

Run ``python scripts/code_health.py`` for a Markdown report of the ``openshard``
package: module sizes, long functions, cyclomatic complexity and broad
``except Exception`` handlers. ``--budgets`` prints the current values as the
Python literals the ratchet test embeds, so a deliberate budget change is a
copy of a measurement, never a number typed from memory.

Only the standard library is used, so the numbers are the same on every
machine and in CI. The complexity count is the usual cyclomatic measure (one
plus every decision point); it is close to, but not necessarily identical
with, ruff's ``C901``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "openshard"


@dataclass(frozen=True)
class FunctionMeasure:
    module: str  # repository-relative POSIX path
    qualname: str
    line: int
    length: int
    complexity: int


def iter_modules(root: Path = REPO_ROOT, package: str = PACKAGE) -> Iterator[tuple[str, Path]]:
    for path in sorted((root / package).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path.relative_to(root).as_posix(), path


def module_lines(root: Path = REPO_ROOT, package: str = PACKAGE) -> dict[str, int]:
    return {rel: _line_count(path) for rel, path in iter_modules(root, package)}


def _line_count(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(1 for _ in fh)


# The decision points McCabe counts (and ruff's C901 follows): branches, loops,
# exception handlers, conditional expressions and comprehension generators.
_DECISIONS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.IfExp,
    ast.comprehension,
)


def _complexity(node: ast.AST) -> int:
    """Cyclomatic complexity of one function body: 1 + decision points.

    Nested function and class bodies are measured on their own, not folded
    into the enclosing function.
    """
    total = 1
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, _DECISIONS):
            total += 1
        elif isinstance(child, ast.Match):
            total += len(child.cases)
        stack.extend(ast.iter_child_nodes(child))
    return total


def _functions(tree: ast.Module) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    def walk(node: ast.AST, prefix: str) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                yield name, child
                yield from walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")
            else:
                yield from walk(child, prefix)

    return walk(tree, "")


def function_measures(root: Path = REPO_ROOT, package: str = PACKAGE) -> list[FunctionMeasure]:
    out: list[FunctionMeasure] = []
    for rel, path in iter_modules(root, package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for qualname, node in _functions(tree):
            end = node.end_lineno or node.lineno
            out.append(FunctionMeasure(rel, qualname, node.lineno, end - node.lineno + 1, _complexity(node)))
    return out


def broad_excepts(root: Path = REPO_ROOT, package: str = PACKAGE) -> dict[str, int]:
    """``except Exception`` / ``except BaseException`` / bare ``except`` handlers per module."""
    counts: dict[str, int] = {}
    for rel, path in iter_modules(root, package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _is_broad(node.type):
                n += 1
        if n:
            counts[rel] = n
    return counts


def _is_broad(expr: ast.expr | None) -> bool:
    if expr is None:
        return True
    names = expr.elts if isinstance(expr, ast.Tuple) else [expr]
    return any(isinstance(n, ast.Name) and n.id in {"Exception", "BaseException"} for n in names)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _table(headers: Iterable[str], rows: Iterable[Iterable[object]]) -> str:
    head = list(headers)
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def report(
    root: Path = REPO_ROOT,
    *,
    module_floor: int = 1000,
    function_floor: int = 100,
    complexity_floor: int = 15,
    except_floor: int = 5,
) -> str:
    lines = module_lines(root)
    functions = function_measures(root)
    excepts = broad_excepts(root)
    long_functions = sorted((f for f in functions if f.length >= function_floor), key=lambda f: -f.length)
    complex_functions = sorted((f for f in functions if f.complexity > complexity_floor), key=lambda f: -f.complexity)
    parts = [
        f"# Code health: `{PACKAGE}` ({len(lines)} modules, {sum(lines.values())} lines)",
        "",
        f"## Modules over {module_floor} lines",
        "",
        _table(["Module", "Lines"], sorted(((m, n) for m, n in lines.items() if n >= module_floor), key=lambda r: -r[1])),
        "",
        f"## Functions of {function_floor}+ lines ({len(long_functions)})",
        "",
        _table(["Lines", "Function", "Where"], [(f.length, f.qualname, f"{f.module}:{f.line}") for f in long_functions]),
        "",
        f"## Functions with cyclomatic complexity over {complexity_floor} ({len(complex_functions)})",
        "",
        _table(
            ["Complexity", "Function", "Where"],
            [(f.complexity, f.qualname, f"{f.module}:{f.line}") for f in complex_functions],
        ),
        "",
        f"## Modules with more than {except_floor} broad except handlers ({sum(excepts.values())} in total)",
        "",
        _table(["Module", "Handlers"], sorted(((m, n) for m, n in excepts.items() if n > except_floor), key=lambda r: -r[1])),
        "",
    ]
    return "\n".join(parts)


def budgets(
    root: Path = REPO_ROOT,
    *,
    module_ceiling: int,
    function_ceiling: int,
    complexity_ceiling: int,
    except_ceiling: int,
) -> str:
    """The current measurements above each general ceiling, as Python literals."""
    lines = module_lines(root)
    functions = function_measures(root)
    excepts = broad_excepts(root)
    out = ["MODULE_LINE_BUDGETS = {"]
    out += [f"    {m!r}: {n}," for m, n in sorted(lines.items(), key=lambda r: -r[1]) if n > module_ceiling]
    out += ["}", "", "FUNCTION_LINE_BUDGETS = {"]
    out += [
        f"    ({f.module!r}, {f.qualname!r}): {f.length},"
        for f in sorted(functions, key=lambda f: -f.length)
        if f.length > function_ceiling
    ]
    out += ["}", "", "COMPLEXITY_BUDGETS = {"]
    out += [
        f"    ({f.module!r}, {f.qualname!r}): {f.complexity},"
        for f in sorted(functions, key=lambda f: -f.complexity)
        if f.complexity > complexity_ceiling
    ]
    out += ["}", "", "BROAD_EXCEPT_BUDGETS = {"]
    out += [f"    {m!r}: {n}," for m, n in sorted(excepts.items(), key=lambda r: -r[1]) if n > except_ceiling]
    out += ["}"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budgets", action="store_true", help="print the ratchet budgets as Python literals")
    parser.add_argument("--module-ceiling", type=int, default=1500)
    parser.add_argument("--function-ceiling", type=int, default=150)
    parser.add_argument("--complexity-ceiling", type=int, default=25)
    parser.add_argument("--except-ceiling", type=int, default=5)
    args = parser.parse_args(argv)
    if args.budgets:
        print(
            budgets(
                module_ceiling=args.module_ceiling,
                function_ceiling=args.function_ceiling,
                complexity_ceiling=args.complexity_ceiling,
                except_ceiling=args.except_ceiling,
            )
        )
    else:
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
