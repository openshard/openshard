"""Guard files: format, parsing and validation.

One guard per file, ``.openshard/guards/<id>.yml``::

    id: billing-rounding
    version: 1
    status: active          # proposed | active | retired
    kind: test              # test | note
    title: Prorated seat charges round down
    scope:
      paths: ["billing/**"]
    check:
      command: "python -m pytest tests/guards/test_billing_rounding.py -q"
      cwd: ""               # optional, repository-relative
      timeout_s: 30         # optional
    origin: {...}           # optional: where the guard came from (kept as-is)
    qualification: {...}   # optional: written by ``openshard guard qualify``

Parsing never executes anything and never raises: a file that cannot be
read or does not validate becomes a :class:`GuardError` the caller reports,
and is never run. Unknown keys are ignored so later versions can add fields.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

GUARDS_DIR = PurePosixPath(".openshard") / "guards"

KINDS = frozenset({"test", "note"})
STATUSES = frozenset({"proposed", "active", "retired"})
MAX_GUARD_FILE_BYTES = 64 * 1024
MAX_GUARDS = 200
MAX_PATHS = 50
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SUFFIXES = (".yml", ".yaml")


@dataclass(frozen=True)
class Guard:
    """One parsed, valid guard."""

    id: str
    version: int
    status: str
    kind: str
    title: str
    paths: tuple[str, ...]
    command: tuple[str, ...] | None
    cwd: str
    timeout_s: float | None
    source_path: str  # repository-relative posix path of the guard file
    origin: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    qualification: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def runnable(self) -> bool:
        """Whether verification runs this guard: an active ``test`` guard with a command."""
        return self.status == "active" and self.kind == "test" and bool(self.command)

    @property
    def label(self) -> str:
        return f"guard {self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "version": self.version, "status": self.status, "kind": self.kind,
            "title": self.title, "paths": list(self.paths),
            "command": list(self.command) if self.command else None,
            "cwd": self.cwd, "timeout_s": self.timeout_s, "source_path": self.source_path,
        }


@dataclass(frozen=True)
class GuardError:
    """A guard file that was not loaded, and why."""

    source_path: str
    message: str


@dataclass
class GuardSet:
    guards: list[Guard] = field(default_factory=list)
    errors: list[GuardError] = field(default_factory=list)
    truncated: bool = False  # more than MAX_GUARDS files; the rest were not read

    def by_id(self) -> dict[str, Guard]:
        return {g.id: g for g in self.guards}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _safe_relative(value: str) -> bool:
    if not value:
        return True
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _command(raw: Any) -> tuple[str, ...] | None:
    from openshard.verification.plan import parse_command_to_argv

    if isinstance(raw, str) and raw.strip():
        argv = parse_command_to_argv(raw.strip())
        return tuple(argv) if argv else None
    if isinstance(raw, list) and raw and all(isinstance(t, str) and t for t in raw):
        return tuple(raw)
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_guard(text: str, source_path: str) -> Guard | GuardError:
    """Parse one guard file's *text*. Returns the guard, or the reason it is invalid."""
    import yaml

    def bad(message: str) -> GuardError:
        return GuardError(source_path, message)

    if len(text.encode("utf-8", "replace")) > MAX_GUARD_FILE_BYTES:
        return bad(f"file is larger than {MAX_GUARD_FILE_BYTES // 1024} KB")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return bad(f"not valid YAML ({exc.__class__.__name__})")
    if not isinstance(data, dict):
        return bad("expected a mapping of keys")

    stem = PurePosixPath(source_path).stem
    gid = data.get("id")
    if not isinstance(gid, str) or not _ID_RE.match(gid):
        return bad("'id' must be lowercase letters, digits and dashes (at most 64)")
    if gid != stem:
        return bad(f"'id' ({gid}) must match the file name ({stem})")

    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return bad("'version' must be a whole number of 1 or more")

    status = data.get("status", "proposed")
    if status not in STATUSES:
        return bad(f"'status' must be one of: {', '.join(sorted(STATUSES))}")

    kind = data.get("kind", "test")
    if kind not in KINDS:
        return bad(f"'kind' must be one of: {', '.join(sorted(KINDS))}")

    title = data.get("title", "")
    if not isinstance(title, str) or not title.strip():
        return bad("'title' is required: one line saying what the guard protects")

    scope = _mapping(data.get("scope"))
    paths = scope.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    if (not isinstance(paths, list) or not paths or len(paths) > MAX_PATHS
            or not all(isinstance(p, str) and p.strip() for p in paths)):
        return bad(f"'scope.paths' must list 1 to {MAX_PATHS} path patterns")
    clean_paths = tuple(p.strip().removeprefix("./") for p in paths)
    if not all(_safe_relative(p) for p in clean_paths):
        return bad("'scope.paths' must be repository-relative (no leading '/', no '..')")

    check = _mapping(data.get("check"))
    command = _command(check.get("command"))
    cwd = check.get("cwd", "") or ""
    if not isinstance(cwd, str) or not _safe_relative(cwd.strip("/")):
        return bad("'check.cwd' must be a repository-relative folder")
    timeout = check.get("timeout_s")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
        return bad("'check.timeout_s' must be a positive number of seconds")
    if kind == "test" and command is None:
        return bad("a 'test' guard needs 'check.command'")

    return Guard(
        id=gid, version=version, status=status, kind=kind, title=" ".join(title.split()),
        paths=clean_paths, command=command if kind == "test" else None, cwd=cwd.strip("/"),
        timeout_s=float(timeout) if timeout is not None else None, source_path=source_path,
        origin=_mapping(data.get("origin")),
        qualification=_mapping(data.get("qualification")),
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _collect(names: Iterable[str], read: Callable[[str], str | None]) -> GuardSet:
    out = GuardSet()
    files = sorted(n for n in names if n.endswith(_SUFFIXES))
    if len(files) > MAX_GUARDS:
        out.truncated = True
        files = files[:MAX_GUARDS]
    seen: set[str] = set()
    for rel in files:
        text = read(rel)
        if text is None:
            out.errors.append(GuardError(rel, "could not be read"))
            continue
        parsed = parse_guard(text, rel)
        if isinstance(parsed, GuardError):
            out.errors.append(parsed)
        elif parsed.id in seen:
            out.errors.append(GuardError(rel, f"duplicate guard id '{parsed.id}'"))
        else:
            seen.add(parsed.id)
            out.guards.append(parsed)
    return out


def load_guards(repo_root: Path) -> GuardSet:
    """The guards in the working tree's ``.openshard/guards/``. Never raises."""
    base = Path(repo_root) / GUARDS_DIR
    try:
        names = [f"{GUARDS_DIR.as_posix()}/{p.name}" for p in base.iterdir() if p.is_file()] if base.is_dir() else []
    except OSError:
        return GuardSet(errors=[GuardError(GUARDS_DIR.as_posix(), "folder could not be listed")])

    def read(rel: str) -> str | None:
        try:
            return (Path(repo_root) / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return _collect(names, read)


def load_guards_at(repo_root: Path, rev: str) -> GuardSet:
    """The guards committed at *rev* (read from git, not the working tree). Never raises."""
    from openshard.util.git import run_git

    listing = run_git(Path(repo_root), ["ls-tree", "--name-only", "-z", f"{rev}:{GUARDS_DIR.as_posix()}"])
    names = [f"{GUARDS_DIR.as_posix()}/{n}" for n in (listing or "").split("\0") if n]

    def read(rel: str) -> str | None:
        return run_git(Path(repo_root), ["show", f"{rev}:{rel}"])

    return _collect(names, read)
