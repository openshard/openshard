"""A bounded map of the repository as observed: directories, file roles, top-level definitions.

The executor and the planner used to see a flat list of up to 200 paths and
had to spend turns reading files to learn what each one holds. The map gives
them structure the harness can actually observe: which directories hold how
many files, what role a file plays by its path (source, tests, docs, config,
CI, data, entrypoint), and the top-level ``def`` / ``class`` / ``function``
names of the source files (``openshard.osn.outline``'s patterns, top level
only). Nothing here is model output or an inferred architecture: every line
is derived from the files' paths and text by fixed rules, and the map says
when it was cut to fit.

Bounds: at most ``MAX_MAP_FILES`` files get a definitions line (source and
entrypoints first, then tests, in path order), ``MAX_MAP_SYMBOLS`` names per
file, ``MAX_MAP_CHARS`` characters in all; a file over ``MAX_MAP_FILE_BYTES``
is counted but not read. Vendored and generated trees are skipped.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from openshard.osn.outline import JS_SUFFIXES, PY_SUFFIXES, top_level_symbols

MAX_MAP_FILES = 60
MAX_MAP_SYMBOLS = 12
MAX_MAP_CHARS = 6000
MAX_MAP_DIRS = 30
MAX_MAP_FILE_BYTES = 262_144
_SYMBOL_COUNT_CAP = MAX_MAP_SYMBOLS * 20  # how far the overflow is counted before it is reported as a lower bound

ROLE_SOURCE = "source"
ROLE_ENTRYPOINT = "entrypoint"
ROLE_TESTS = "tests"
ROLE_DOCS = "docs"
ROLE_CONFIG = "config"
ROLE_CI = "ci"
ROLE_DATA = "data"
ROLE_OTHER = "other"

_SKIP_DIRS = frozenset({
    "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".git", ".openshard",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".eggs", "site-packages", "target", "vendor",
})
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_CONFIG_NAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "package.json", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "tsconfig.json", "requirements.txt", "requirements-dev.txt", "pipfile", "poetry.lock",
    "uv.lock", "makefile", "dockerfile", "docker-compose.yml", "docker-compose.yaml", ".editorconfig",
    ".gitignore", ".pre-commit-config.yaml", "tox.ini", "mypy.ini", "pytest.ini", "ruff.toml", ".env.example",
})
_CONFIG_SUFFIXES = (".toml", ".ini", ".cfg", ".yaml", ".yml")
_DATA_SUFFIXES = (".json", ".jsonl", ".csv", ".tsv", ".xml", ".sql", ".parquet")
_ENTRYPOINT_NAMES = frozenset({
    "main.py", "__main__.py", "app.py", "cli.py", "manage.py", "wsgi.py", "asgi.py", "server.py",
    "index.js", "index.ts", "main.js", "main.ts", "app.js", "app.ts", "server.js", "server.ts",
})
_SOURCE_SUFFIXES = PY_SUFFIXES + JS_SUFFIXES + (
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift",
    ".scala", ".sh", ".ps1", ".sql", ".vue", ".svelte",
)


def file_role(rel: str) -> str:
    """The role a path plays, by fixed path rules; ``other`` when none applies."""
    path = PurePosixPath(rel.replace("\\", "/"))
    parts = [p.lower() for p in path.parts]
    name = path.name.lower()
    suffix = path.suffix.lower()
    if any(p in (".github", ".gitlab", ".circleci", ".buildkite") for p in parts[:-1]) or name in (
        ".gitlab-ci.yml", ".travis.yml", "jenkinsfile", "azure-pipelines.yml",
    ):
        return ROLE_CI
    if any(p in ("tests", "test", "__tests__", "spec", "specs") for p in parts[:-1]) or name.startswith(
        ("test_", "tests_")
    ) or name.endswith(("_test.py", ".test.js", ".test.ts", ".test.tsx", ".spec.js", ".spec.ts", ".spec.tsx")):
        return ROLE_TESTS
    if any(p in ("docs", "doc") for p in parts[:-1]) or suffix in _DOC_SUFFIXES:
        return ROLE_DOCS
    if name in _CONFIG_NAMES or suffix in _CONFIG_SUFFIXES:
        return ROLE_CONFIG
    if name in _ENTRYPOINT_NAMES:
        return ROLE_ENTRYPOINT
    if suffix in _SOURCE_SUFFIXES:
        return ROLE_SOURCE
    if suffix in _DATA_SUFFIXES:
        return ROLE_DATA
    return ROLE_OTHER


def _skipped(rel: str) -> bool:
    return any(part in _SKIP_DIRS for part in PurePosixPath(rel.replace("\\", "/")).parts[:-1])


@dataclass
class RepoMap:
    """The rendered map and the counts the Receipt records (never the text)."""

    text: str
    files_total: int
    files_mapped: int  # files given a definitions line
    symbols: int
    truncated: bool
    roles: dict[str, int] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "files_total": self.files_total,
            "files_mapped": self.files_mapped,
            "symbols": self.symbols,
            "chars": len(self.text),
            "truncated": self.truncated,
            "roles": dict(sorted(self.roles.items())),
            "source": "observed_file_structure",
        }


def build_repo_map(root: Path, files: list[str]) -> RepoMap:
    """The map of *files* (repo-relative, as the loop lists them) under *root*. Never raises."""
    rels = [f.replace("\\", "/") for f in files if isinstance(f, str) and f.strip()]
    kept = [r for r in rels if not _skipped(r)]
    roles = Counter(file_role(r) for r in kept)
    dirs = Counter(str(PurePosixPath(r).parent) for r in kept)

    lines: list[str] = [f"Repository map (observed from the files; {len(rels)} files"
                        + (f", {len(rels) - len(kept)} in vendored/generated trees not mapped" if len(kept) != len(rels) else "")
                        + "):"]
    top = sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_MAP_DIRS]
    lines.append("Directories: " + ", ".join(
        f"{'.' if d == '.' else d + '/'} ({n})" for d, n in top
    ) + (f", ... {len(dirs) - len(top)} more" if len(dirs) > len(top) else ""))
    lines.append("Roles: " + ", ".join(f"{role} {n}" for role, n in sorted(roles.items(), key=lambda kv: (-kv[1], kv[0]))))

    order = {ROLE_ENTRYPOINT: 0, ROLE_SOURCE: 1, ROLE_TESTS: 2}
    candidates = sorted(
        (r for r in kept if file_role(r) in order and r.lower().endswith(PY_SUFFIXES + JS_SUFFIXES)),
        key=lambda r: (order[file_role(r)], r),
    )
    lines.append("Top-level definitions (name only; read_file for bodies):")
    mapped = symbols_total = 0
    truncated = False
    chars = sum(len(line) + 1 for line in lines)
    for rel in candidates:
        if mapped >= MAX_MAP_FILES:
            truncated = True
            break
        p = root / rel
        try:
            if p.stat().st_size > MAX_MAP_FILE_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names = top_level_symbols(text, rel, max_symbols=_SYMBOL_COUNT_CAP)
        if not names:
            continue
        extra = len(names) - MAX_MAP_SYMBOLS
        more = f" (+{extra}{'+' if len(names) >= _SYMBOL_COUNT_CAP else ''} more)" if extra > 0 else ""
        role = file_role(rel)
        tag = "" if role == ROLE_SOURCE else f" ({role})"
        line = f"{rel}{tag}: " + ", ".join(names[:MAX_MAP_SYMBOLS]) + more
        if chars + len(line) + 1 > MAX_MAP_CHARS:
            truncated = True
            break
        lines.append(line)
        chars += len(line) + 1
        mapped += 1
        symbols_total += min(len(names), MAX_MAP_SYMBOLS)
    if truncated:
        lines.append(f"... (map capped at {MAX_MAP_FILES} files / {MAX_MAP_CHARS} characters; use list_files and search_repo for the rest)")
    return RepoMap(
        text="\n".join(lines), files_total=len(rels), files_mapped=mapped, symbols=symbols_total,
        truncated=truncated, roles=dict(roles),
    )


__all__ = ["MAX_MAP_CHARS", "MAX_MAP_FILES", "MAX_MAP_SYMBOLS", "RepoMap", "build_repo_map", "file_role"]
