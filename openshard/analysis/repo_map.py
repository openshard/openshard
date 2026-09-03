"""Repo-map construction for the local repo-map cache (v1).

Pure, deterministic, side-effect-free metadata collection. Never calls models,
never makes network calls, never writes files (cache IO lives in
``analysis/repo_map_cache.py``). Mirrors the bounded-walk safety pattern of
``native/osn_observation.py``:

* relative forward-slash paths only - never absolute,
* never stores file contents, raw secrets, env values, or transcripts,
* skips generated/ignored directories and caps the file walk,
* degrades safely on permission errors (warn, never raise),
* treats git/branch/path strings as untrusted display metadata (sanitised).
"""
from __future__ import annotations

import datetime
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from openshard.analysis.repo import (
    _EXT_TO_LANG,
    _KNOWN_PACKAGE_FILES,
    _RISKY_KEYWORDS,
    _SKIP_DIRS,
    _detect_framework,
    _detect_package_files,
    _detect_test_command,
)

SCHEMA_VERSION = "1"
SOURCE = "repo_map_v1"

# Bounded-walk + output caps.
_WALK_FILE_CAP = 2000
MAX_IMPORTANT_FILES = 30
MAX_RISKY = 50
MAX_WARNINGS = 20

# Sanitisation length caps for untrusted display metadata.
_BRANCH_CAP = 128
_WARNING_CAP = 200
_PATH_DISPLAY_CAP = 200

# Fixed-literal warnings (never raw exception text - that could leak a local path).
_DIRTY_WARNING = "dirty git tree; repo-map cache was rebuilt instead of reused"
_NOT_GIT_WARNING = "not a git repository; fingerprint based on top-level layout"
_WALK_CAPPED_WARNING = f"file walk capped at {_WALK_FILE_CAP} files; map may be partial"
_PERMISSION_WARNING = "permission error while scanning; some paths were skipped"

# Present package file -> package manager label.
_PKG_TO_MANAGER: dict[str, str] = {
    "package.json": "npm",
    "pyproject.toml": "pip",
    "requirements.txt": "pip",
    "setup.py": "pip",
    "setup.cfg": "pip",
    "Cargo.toml": "cargo",
    "go.mod": "go",
    "Gemfile": "bundler",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "composer.json": "composer",
}


def _sanitize_meta(value: str | None, *, cap: int = _WARNING_CAP) -> str | None:
    """Make an untrusted git/path/warning string safe to store and display.

    Strips CR/LF (no log/JSON-line injection), reduces absolute-path-looking
    values to a bare name (no local path leak), and caps the length. Returns
    None unchanged.
    """
    if value is None:
        return None
    s = value.replace("\r", " ").replace("\n", " ").strip()
    norm = s.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        # Take the basename from the normalised path so Windows-style backslash
        # separators are handled the same on POSIX and Windows (Path(...).name
        # does not split on "\" under POSIX).
        s = norm.rstrip("/").rsplit("/", 1)[-1]
    if len(s) > cap:
        s = s[:cap]
    return s


def _dedup_capped(items: list[str], cap: int) -> list[str]:
    """Order-preserving dedup, capped at *cap* items."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
        if len(out) >= cap:
            break
    return out


@dataclass
class GitInfo:
    branch: str | None
    head_commit: str | None
    dirty: bool
    is_git: bool


# See the matching comment in adapters/claude_code_import.py: this git call
# can run from the console-less background capture-service worker, which
# would otherwise cause Windows to pop a new console per git.exe child.
# getattr sidesteps mypy's attr-defined error for CREATE_NO_WINDOW, which
# only exists in typeshed's Windows stubs (a sys.platform guard alone does
# not make a direct attribute access type-check on this cross-platform module).
_NO_WINDOW_KW: dict = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
)


def _run_git(root: Path, args: list[str], *, timeout: float = 5.0) -> str | None:
    """Run a git command under *root*; return stdout on success, else None. Never raises."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            **_NO_WINDOW_KW,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        return None
    return None


def collect_git_info(root: Path) -> GitInfo:
    """Collect branch / head commit / dirty / is_git for *root*. Never raises.

    branch is sanitised; detached HEAD yields branch=None. A commit hash is safe
    as-is (hex). dirty is derived from ``git status --porcelain``.
    """
    inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if inside is None or inside.strip() != "true":
        return GitInfo(branch=None, head_commit=None, dirty=False, is_git=False)

    branch_out = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch: str | None = None
    if branch_out is not None:
        raw = branch_out.strip()
        if raw and raw != "HEAD":
            branch = _sanitize_meta(raw, cap=_BRANCH_CAP)

    head_out = _run_git(root, ["rev-parse", "HEAD"])
    head_commit: str | None = head_out.strip() if head_out and head_out.strip() else None

    status_out = _run_git(root, ["status", "--porcelain"])
    dirty = _porcelain_is_dirty(status_out)

    return GitInfo(branch=branch, head_commit=head_commit, dirty=dirty, is_git=True)


def _porcelain_is_dirty(status_out: str | None) -> bool:
    """True if `git status --porcelain` reports changes outside ``.openshard/``.

    OpenShard's own ``.openshard/`` dir (run receipts, and this very cache) must
    not count as a repo change - otherwise writing the cache would dirty the tree
    and defeat cache hits. Mirrors the ``.openshard/`` filter in
    ``analysis/repo._detect_changed_files``.
    """
    if not status_out:
        return False
    for line in status_out.splitlines():
        if not line.strip():
            continue
        # porcelain v1: "XY <path>" (rename: "XY <orig> -> <new>").
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip('"').replace("\\", "/")
        if path.startswith(".openshard/"):
            continue
        return True
    return False


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def compute_repo_fingerprint(root: Path) -> tuple[str, GitInfo, list[str]]:
    """Return (fingerprint, git_info, warnings) for *root*.

    Git repos fingerprint cheaply from branch|head|dirty (no walk). Non-git repos
    fall back to a hash of sorted top-level entry names (documented v1 limitation).
    """
    warnings: list[str] = []
    git = collect_git_info(root)
    if git.is_git:
        raw = f"{git.branch}|{git.head_commit}|{git.dirty}"
    else:
        warnings.append(_NOT_GIT_WARNING)
        names = sorted(p.name for p in _safe_iterdir(root))
        raw = "|".join(names)
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return fingerprint, git, warnings


def _is_important(rel: str, name_lower: str, suffix: str, name: str) -> bool:
    if name in _KNOWN_PACKAGE_FILES:
        return True
    if name_lower in ("dockerfile", "makefile", "docker-compose.yml", "docker-compose.yaml"):
        return True
    if name_lower.startswith("readme"):
        return True
    if suffix == ".tf":
        return True
    if rel.startswith(".github/workflows/") and suffix in (".yml", ".yaml"):
        return True
    return False


def _walk_repo_map(root: Path) -> tuple[int, int, list[str], list[str], list[str], bool, bool]:
    """Single bounded walk.

    Returns (file_count, directory_count, languages, important_files, risky_areas,
    files_capped, had_permission_error). Skips ``_SKIP_DIRS``; caps at ``_WALK_FILE_CAP``
    files; degrades safely on permission errors.
    """
    file_count = 0
    dir_count = 0
    languages: set[str] = set()
    important: list[str] = []
    risky: list[str] = []
    files_capped = False
    perm_error = False

    try:
        for p in root.rglob("*"):
            try:
                rel_parts = p.relative_to(root).parts
            except ValueError:
                continue
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            try:
                if p.is_dir():
                    dir_count += 1
                    continue
                if not p.is_file():
                    continue
            except OSError:
                perm_error = True
                continue

            if file_count >= _WALK_FILE_CAP:
                files_capped = True
                break
            file_count += 1

            rel = str(p.relative_to(root)).replace("\\", "/")
            name = p.name
            name_lower = name.lower()
            suffix = p.suffix.lower()

            lang = _EXT_TO_LANG.get(suffix)
            if lang:
                languages.add(lang)

            if any(kw in name_lower for kw in _RISKY_KEYWORDS) and len(risky) < MAX_RISKY:
                risky.append(rel)

            if _is_important(rel, name_lower, suffix, name) and len(important) < MAX_IMPORTANT_FILES:
                important.append(rel)
    except OSError:
        perm_error = True

    return (
        file_count,
        dir_count,
        sorted(languages),
        sorted(important),
        sorted(risky),
        files_capped,
        perm_error,
    )


def _package_managers(package_files: list[str]) -> list[str]:
    managers = {_PKG_TO_MANAGER[name] for name in package_files if name in _PKG_TO_MANAGER}
    return sorted(managers)


@dataclass
class RepoMap:
    schema_version: str
    source: str
    created_at: str
    repo_fingerprint: str
    git: dict
    summary: dict
    important_files: list[str]
    risky_areas: list[str]
    ignored_directories: list[str]
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "created_at": self.created_at,
            "repo_fingerprint": self.repo_fingerprint,
            "git": {
                "branch": self.git.get("branch"),
                "head_commit": self.git.get("head_commit"),
                "dirty": bool(self.git.get("dirty", False)),
            },
            "summary": {
                "file_count": self.summary.get("file_count", 0),
                "directory_count": self.summary.get("directory_count", 0),
                "languages": list(self.summary.get("languages", [])),
                "frameworks": list(self.summary.get("frameworks", [])),
                "package_managers": list(self.summary.get("package_managers", [])),
                "test_commands": list(self.summary.get("test_commands", [])),
            },
            "important_files": list(self.important_files),
            "risky_areas": list(self.risky_areas),
            "ignored_directories": list(self.ignored_directories),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, d: dict) -> RepoMap:
        git = d.get("git", {}) or {}
        summary = d.get("summary", {}) or {}
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            source=d.get("source", SOURCE),
            created_at=d.get("created_at", ""),
            repo_fingerprint=d.get("repo_fingerprint", ""),
            git={
                "branch": git.get("branch"),
                "head_commit": git.get("head_commit"),
                "dirty": bool(git.get("dirty", False)),
            },
            summary={
                "file_count": summary.get("file_count", 0),
                "directory_count": summary.get("directory_count", 0),
                "languages": list(summary.get("languages", [])),
                "frameworks": list(summary.get("frameworks", [])),
                "package_managers": list(summary.get("package_managers", [])),
                "test_commands": list(summary.get("test_commands", [])),
            },
            important_files=list(d.get("important_files", [])),
            risky_areas=list(d.get("risky_areas", [])),
            ignored_directories=list(d.get("ignored_directories", [])),
            warnings=list(d.get("warnings", [])),
        )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_repo_map(root: str | Path, *, now_iso: str | None = None) -> RepoMap:
    """Build a bounded, safe repo map for *root*. Never raises; degrades with warnings."""
    p = Path(root)
    created_at = now_iso or _now_iso()

    fingerprint, git, warnings = compute_repo_fingerprint(p)

    (
        file_count,
        dir_count,
        languages,
        important,
        risky,
        files_capped,
        perm_error,
    ) = _walk_repo_map(p)

    package_files = _detect_package_files(p)
    framework = _detect_framework(p, package_files)
    test_command = _detect_test_command(p, package_files)

    # important_files: known package files (root) merged with walk-discovered ones.
    important_files = _dedup_capped(sorted(package_files) + important, MAX_IMPORTANT_FILES)

    ignored_directories = sorted(d for d in _SKIP_DIRS if (p / d).is_dir())

    if files_capped:
        warnings.append(_WALK_CAPPED_WARNING)
    if perm_error:
        warnings.append(_PERMISSION_WARNING)

    clean_warnings = _dedup_capped(
        [w for w in (_sanitize_meta(w) for w in warnings) if w],
        MAX_WARNINGS,
    )

    return RepoMap(
        schema_version=SCHEMA_VERSION,
        source=SOURCE,
        created_at=created_at,
        repo_fingerprint=fingerprint,
        git={"branch": git.branch, "head_commit": git.head_commit, "dirty": git.dirty},
        summary={
            "file_count": file_count,
            "directory_count": dir_count,
            "languages": languages,
            "frameworks": [framework] if framework else [],
            "package_managers": _package_managers(package_files),
            "test_commands": [test_command] if test_command else [],
        },
        important_files=important_files,
        risky_areas=risky,
        ignored_directories=ignored_directories,
        warnings=clean_warnings,
    )
