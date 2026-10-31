"""Gaming checks v1: plain observations that a session may have worked around its own checks.

Agents sometimes satisfy a verifier instead of the task: they weaken or
delete tests, read grading or answer files, run a test command that cannot
fail, or edit the records that describe their own run. This module notices
four such patterns in evidence Core already captures for a hook-observed
session and records each as a ``gaming_signals`` item on the Receipt:

* ``test_files_weakened`` -- the session deleted a test file, removed more
  assertion lines than it added in one, or added a skip marker. Read from
  the session's own ``git diff`` of its changed test files only, in memory;
  only paths and counts are kept, never a line of code.
* ``grader_or_answer_read`` -- the agent read a file matching a grading /
  answer pattern (``DEFAULT_GRADER_PATTERNS``, or ``gaming_checks.
  grader_patterns`` in the repository's ``.openshard/config.yml``, which
  replaces the defaults; an empty list turns the check off), with a shell
  reader (``cat``...), a read-tool Event, or Claude Code's ``Read`` tool
  (``note_answer_read``: the path only, never the content).
* ``masked_exit`` -- a test command the line itself makes unable to fail
  (``|| true``, a final ``exit 0``, ``set +e``; decided from the raw command
  by ``check_command``), or ``sys.exit(0)`` added to a test or conftest file.
* ``trace_tampering`` -- the agent's file tools or shell commands wrote to
  OpenShard's own records (``.openshard/``, ``runs.jsonl``...) or to an
  agent transcript directory.

A signal is an observation, never a verdict: it never changes the
Receipt's status or verification, and its wording states what was seen
without saying why. ``gaming_signals`` absent means "not checked" (older
records, non-hook records); an empty list means checked and nothing seen.
Local only: the Platform's receipt-sync contract has no field for it, so
the sync projection never carries it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

KIND_TEST_FILES_WEAKENED = "test_files_weakened"
KIND_GRADER_READ = "grader_or_answer_read"
KIND_MASKED_EXIT = "masked_exit"
KIND_TRACE_TAMPERING = "trace_tampering"
KINDS = (KIND_TEST_FILES_WEAKENED, KIND_GRADER_READ, KIND_MASKED_EXIT, KIND_TRACE_TAMPERING)

DEFAULT_GRADER_PATTERNS: tuple[str, ...] = (
    "**/grader*",
    "**/*answer*",
    "**/expected_output*",
    "**/.hidden_tests/**",
    "**/eval*/solution*",
    # Expected test outputs: golden files, snapshots, expected/ dirs. Not fixtures/:
    # agents read test inputs routinely (a repository's config can add it).
    "**/golden/**",
    "**/goldens/**",
    "**/*.golden",
    "**/__snapshots__/**",
    "**/snapshots/**",
    "**/*.snap",
    "**/expected/**",
)
MAX_PATHS = 5  # paths kept per signal
MAX_SEEN = 20  # observations remembered per kind across folds
MAX_TEST_DIFF_FILES = 50
_MESSAGE_CAP = 200
_PATH_CAP = 200

def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


# -- test-file heuristics -------------------------------------------------------

_TEST_DIR_PARTS = frozenset({"tests", "test", "__tests__"})
_TEST_NAME_RE = re.compile(
    r"^(?:test_.*\.py|.*_test\.py|conftest\.py|.*_test\.go|.*\.(?:spec|test)\.[cm]?[jt]sx?)$",
    re.IGNORECASE,
)
_ASSERT_RE = re.compile(r"^\s*(?:assert\b|self\.assert\w*\(|assert\.\w+\(|expect\()|\bexpect\(")
_SKIP_RE = re.compile(
    r"@pytest\.mark\.skip|\bpytest\.skip\(|@unittest\.skip|\.skip\(|\bxit\(|\bxdescribe\(|\bt\.Skip(?:Now|f)?\("
)
_EXIT_ZERO_RE = re.compile(r"\b(?:sys\.exit|os\._exit)\(\s*(?:0|None)?\s*\)")


def is_test_path(path: str) -> bool:
    """Whether a repo-relative *path* looks like a test file (directory or file-name convention)."""
    parts = path.replace("\\", "/").split("/")
    return bool(_TEST_NAME_RE.match(parts[-1])) or any(p.lower() in _TEST_DIR_PARTS for p in parts[:-1])


def diff_line_stats(diff_text: str) -> dict[str, dict[str, int]]:
    """Per-file assertion / skip-marker / ``sys.exit(0)`` line counts of a unified diff. Never stores text."""
    stats: dict[str, dict[str, int]] = {}
    current: dict[str, int] | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            current = None if name == "/dev/null" else stats.setdefault(name.removeprefix("b/"), {})
            continue
        if line.startswith("--- ") or current is None or line[:1] not in ("+", "-"):
            continue
        sign = "added" if line[0] == "+" else "removed"
        body = line[1:]
        for key, rx in (("assert", _ASSERT_RE), ("skip", _SKIP_RE), ("exit_zero", _EXIT_ZERO_RE)):
            if rx.search(body):
                current[f"{key}_{sign}"] = current.get(f"{key}_{sign}", 0) + 1
    return stats


def read_test_diff(repo_root: Path, base: str | None, paths: list[str]) -> str | None:
    """``git diff -U0 base -- paths`` (base to working tree), or None when git cannot say. Never raises."""
    if not base or not re.fullmatch(r"[0-9a-f]{7,64}", base) or not paths:
        return None
    from openshard.util.git import run_git

    pathspec = ["--", *(f":(literal){p}" for p in paths[:MAX_TEST_DIFF_FILES])]
    return run_git(repo_root, ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", base, *pathspec])


# -- grader / answer patterns ---------------------------------------------------


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z", re.IGNORECASE)


def matches_any(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Whether repo-relative *path* matches one of the ``**``-style glob *patterns* (case-insensitive)."""
    norm = path.replace("\\", "/")
    norm = norm[2:] if norm.startswith("./") else norm
    return any(_glob_regex(p).match(norm) for p in patterns if isinstance(p, str) and p)


def grader_patterns(repo_root: Path | None) -> tuple[str, ...]:
    """``gaming_checks.grader_patterns`` from the repository's config, else the defaults. Never raises."""
    try:
        from openshard.config.settings import _load_yaml, find_config_path

        path = find_config_path(cwd=repo_root)
        data = _load_yaml(path) if path is not None and path.is_file() else {}
        block = data.get("gaming_checks") if isinstance(data, dict) else None
        raw = block.get("grader_patterns") if isinstance(block, dict) else None
        if isinstance(raw, list):
            return tuple(p for p in raw if isinstance(p, str) and p.strip())[:50]
    except Exception:
        pass
    return DEFAULT_GRADER_PATTERNS


# -- events: reads, writes to OpenShard's records -------------------------------

_RECORD_DIRS = (".openshard/", ".claude/projects/", ".codex/sessions/")
_RECORD_FILES = frozenset({"runs.jsonl", "receipt-signatures.jsonl", "verifications.jsonl"})
_READERS = frozenset({"cat", "head", "tail", "less", "more", "bat", "nl", "strings", "xxd", "od", "type"})
_WRITERS = frozenset({"rm", "mv", "cp", "truncate", "tee", "shred", "unlink", "dd"})
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|&]")


def is_record_path(path: str) -> bool:
    """Whether *path* is OpenShard's own state or an agent transcript directory."""
    norm = path.replace("\\", "/")
    norm = norm[2:] if norm.startswith("./") else norm
    # A bare record file name counts only without a directory (``rm runs.jsonl``
    # run inside .openshard/): elsewhere it is somebody else's file.
    return norm.startswith(_RECORD_DIRS) or any(f"/{d}" in norm for d in _RECORD_DIRS) or norm in _RECORD_FILES


def _command_words(action: str) -> list[list[str]]:
    text = action.split(": ", 1)[1] if ": " in action else ""
    return [seg.split() for seg in _SEGMENT_SPLIT_RE.split(text) if seg.strip()]


def _program(word: str) -> str:
    return word.rsplit("/", 1)[-1].lower()


def command_reads(action: str) -> list[str]:
    """File arguments of reader programs (``cat x``) in a stored, scrubbed command label."""
    out: list[str] = []
    for words in _command_words(action):
        if words and _program(words[0]) in _READERS:
            out.extend(w.strip("'\"") for w in words[1:] if not w.startswith("-"))
    return out


def command_record_writes(action: str) -> list[str]:
    """OpenShard-record paths a stored, scrubbed command label writes, moves, deletes or redirects into."""
    out: list[str] = []
    for words in _command_words(action):
        if not words:
            continue
        program = _program(words[0])
        in_place = program in ("sed", "perl") and any(w.startswith("-i") or w == "--in-place" for w in words)
        redirect_targets = [words[i + 1] for i, w in enumerate(words[:-1]) if w in (">", ">>")]
        redirect_targets += [w.lstrip(">") for w in words if w.startswith(">") and len(w.lstrip(">")) > 0]
        candidates = redirect_targets + (words[1:] if program in _WRITERS or in_place else [])
        out.extend(w.strip("'\"") for w in candidates if is_record_path(w.strip("'\"")))
    return out


def _tool_events(events: list) -> list[dict]:
    return [e for e in events if isinstance(e, dict) and e.get("event_type") == "tool.invoked"]


def scan_events(events: list, patterns: tuple[str, ...]) -> dict[str, list[str]]:
    """Grader reads and record writes seen in a session's tool Events (paths only)."""
    reads: list[str] = []
    writes: list[str] = []
    for ev in _tool_events(events):
        meta = _dict(ev.get("metadata"))
        target = _str(ev.get("target"))
        action = _str(ev.get("action"))
        if "command_kind" in meta:
            reads.extend(p for p in command_reads(action) if matches_any(p, patterns))
            writes.extend(command_record_writes(action))
        elif meta.get("access") == "read":
            if target and matches_any(target, patterns):
                reads.append(target)
        elif target and is_record_path(target):
            writes.append(target)
    return {"reads": reads, "writes": writes}


# -- accumulated observations (survive event archiving and buffer rebuilds) -----


def note_answer_read(buf: dict, path: object, repo_root: Path) -> None:
    """Remember a file-read tool's repo-relative *path* when it matches a grader pattern (path only)."""
    if isinstance(path, str) and path and matches_any(path, grader_patterns(repo_root)):
        seen = buf.get("gaming_seen")
        if not isinstance(seen, dict):
            seen = buf["gaming_seen"] = {}
        _add_seen(seen, "reads", [path])


def _add_seen(seen: dict, key: str, values: list) -> None:
    current = seen.setdefault(key, [])
    for value in values:
        if value not in current and len(current) < MAX_SEEN:
            current.append(value)


def note_masked_check(buf: dict, name: object, mask: object) -> None:
    """Remember a test command the hook saw whose line cannot fail (``check_command.EXIT_MASKS``)."""
    from openshard.verification.check_command import EXIT_MASKS

    if isinstance(name, str) and name and mask in EXIT_MASKS:
        seen = _dict(buf.get("gaming_seen"))
        _add_seen(seen, "masked", [{"name": name[:160], "mask": mask}])
        buf["gaming_seen"] = seen


# -- signals --------------------------------------------------------------------

_MASK_TEXT = {
    "or_true": "{cmd} || true",
    "or_noop": "{cmd} || :",
    "or_exit_zero": "{cmd} || exit 0",
    "exit_zero": "{cmd}; exit 0",
    "set_plus_e": "set +e; {cmd}",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _signal(kind: str, message: str, paths: list[str], counts: dict[str, int] | None = None) -> dict:
    return {"kind": kind, "message": message[:_MESSAGE_CAP], "paths": paths[:MAX_PATHS], "counts": counts or {}}


def _more(paths: list[str]) -> str:
    return f" and {len(paths) - 1} more" if len(paths) > 1 else ""


def test_weakening_signal(test_files: list[dict], stats: dict[str, dict[str, int]]) -> dict | None:
    """``test_files_weakened`` from the session's changed test files and their diff counts, or None."""
    deleted = [f["path"] for f in test_files if f.get("change_type") == "delete"]
    weakened: list[tuple[str, int]] = []
    skipped: list[tuple[str, int]] = []
    for path, s in stats.items():
        net_removed = s.get("assert_removed", 0) - s.get("assert_added", 0)
        if net_removed > 0:
            weakened.append((path, net_removed))
        net_skips = s.get("skip_added", 0) - s.get("skip_removed", 0)
        if net_skips > 0:
            skipped.append((path, net_skips))
    if not (deleted or weakened or skipped):
        return None
    edited = len(test_files) - len(deleted)
    parts = [f"{_plural(edited, 'test file')} edited"] if edited else []
    if deleted:
        parts.append(f"{_plural(len(deleted), 'test file')} deleted: {deleted[0]}{_more(deleted)}")
    for path, n in weakened[:2]:
        parts.append(f"{_plural(n, 'assertion')} removed in {path}")
    for path, n in skipped[:2]:
        parts.append(f"{_plural(n, 'skip marker')} added in {path}")
    paths = deleted + [p for p, _ in weakened] + [p for p, _ in skipped]
    counts = {
        "test_files_edited": edited,
        "test_files_deleted": len(deleted),
        "assertions_removed": sum(n for _, n in weakened),
        "skip_markers_added": sum(n for _, n in skipped),
    }
    return _signal(KIND_TEST_FILES_WEAKENED, "; ".join(parts), list(dict.fromkeys(paths)), counts)


def grader_read_signal(reads: list[str]) -> dict | None:
    if not reads:
        return None
    return _signal(KIND_GRADER_READ, f"Agent read a grading or answer file: {reads[0]}{_more(reads)}",
                   reads, {"files": len(reads)})


def masked_exit_signal(masked: list[dict], exit_added: list[str]) -> dict | None:
    parts: list[str] = []
    if masked:
        first = masked[0]
        cmd = str(first.get("name") or "").split(": ", 1)[-1]
        shown = _MASK_TEXT.get(str(first.get("mask")), "{cmd}").format(cmd=cmd)
        parts.append(f"Test command can't fail: `{shown}`" + (f" (+{len(masked) - 1} more)" if len(masked) > 1 else ""))
    if exit_added:
        parts.append(f"sys.exit(0) added in {exit_added[0]}{_more(exit_added)}")
    if not parts:
        return None
    return _signal(KIND_MASKED_EXIT, "; ".join(parts), exit_added,
                   {"commands": len(masked), "exit_zero_added": len(exit_added)})


def trace_tampering_signal(writes: list[str]) -> dict | None:
    if not writes:
        return None
    return _signal(KIND_TRACE_TAMPERING, f"Agent wrote to OpenShard's own records: {writes[0]}{_more(writes)}",
                   writes, {"paths": len(writes)})


def _session_test_files(entry: dict) -> list[dict]:
    files = entry.get("files_detail")
    return [
        f for f in (files if isinstance(files, list) else [])
        if isinstance(f, dict) and isinstance(f.get("path"), str) and is_test_path(f["path"])
        and f.get("attribution") not in ("pre_existing", "other_session", "other_commit")
    ]


def stamp_gaming_signals(entry: dict, buf: dict, repo_root: Path) -> None:
    """Set ``entry["gaming_signals"]`` for a hook session (see module docstring). Never raises.

    Observations accumulate in ``buf["gaming_seen"]`` (mirrored to
    ``capture.gaming_seen`` so a rebuilt buffer keeps them); the test-file
    diff is re-read at every fold, since the working tree is the evidence.
    """
    try:
        seen = _dict(buf.get("gaming_seen"))
        found = scan_events(entry.get("events") or [], grader_patterns(repo_root))
        _add_seen(seen, "reads", found["reads"])
        _add_seen(seen, "writes", found["writes"])
        buf["gaming_seen"] = seen
        capture = entry["capture"] if isinstance(entry.get("capture"), dict) else {}
        test_files = _session_test_files(entry)
        stats: dict[str, dict[str, int]] = {}
        diffable = [f["path"] for f in test_files if f.get("change_type") != "delete"]
        if diffable:
            from openshard.history.receipt_evidence import base_commit_value

            base = capture.get("files_base_commit") or base_commit_value(entry)
            diff = read_test_diff(repo_root, base if isinstance(base, str) else None, diffable)
            if diff is None:
                capture["gaming_not_checked"] = ["test_diff_unavailable"]
            else:
                stats = {p: s for p, s in diff_line_stats(diff).items() if p in diffable}
        exit_added = [p for p, s in stats.items() if s.get("exit_zero_added", 0) > s.get("exit_zero_removed", 0)]
        signals = [
            test_weakening_signal(test_files, stats),
            grader_read_signal(list(seen.get("reads") or [])),
            masked_exit_signal([m for m in seen.get("masked") or [] if isinstance(m, dict)], exit_added),
            trace_tampering_signal(list(seen.get("writes") or [])),
        ]
        entry["gaming_signals"] = [s for s in signals if s is not None]
        if any(seen.values()):
            capture["gaming_seen"] = seen
    except Exception:
        entry.pop("gaming_signals", None)  # not checked, never "nothing seen"


def rebuilt_seen(capture: dict) -> dict:
    """``gaming_seen`` carried from a record into a buffer rebuilt from it."""
    raw = capture.get("gaming_seen") if isinstance(capture, dict) else None
    return {k: list(v) for k, v in raw.items() if isinstance(v, list)} if isinstance(raw, dict) else {}


# -- projection -----------------------------------------------------------------


def _safe_path(value: Any) -> str | None:
    from openshard.history.receipt_evidence import unsafe_text

    if not isinstance(value, str) or not value.strip() or unsafe_text(value):
        return None
    return value[:_PATH_CAP]


def gaming_signals_block(entry: Any) -> list[dict[str, Any]] | None:
    """The record's ``gaming_signals``, re-validated; None when the record was never checked."""
    raw = entry.get("gaming_signals") if isinstance(entry, dict) else None
    if not isinstance(raw, list):
        return None
    from openshard.history.receipt_evidence import unsafe_text

    out: list[dict[str, Any]] = []
    for item in raw[: len(KINDS)]:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        message = item.get("message")
        if not isinstance(message, str) or not message.strip() or unsafe_text(message):
            continue
        counts_raw = _dict(item.get("counts"))
        out.append({
            "kind": item["kind"],
            "message": message[:_MESSAGE_CAP],
            "paths": [p for p in (_safe_path(v) for v in (item.get("paths") or [])[:MAX_PATHS]) if p],
            "counts": {
                k: v for k, v in counts_raw.items()
                if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
            },
        })
    return out


__all__ = [
    "DEFAULT_GRADER_PATTERNS",
    "KINDS",
    "diff_line_stats",
    "gaming_signals_block",
    "grader_patterns",
    "is_test_path",
    "matches_any",
    "note_masked_check",
    "rebuilt_seen",
    "scan_events",
    "stamp_gaming_signals",
]
