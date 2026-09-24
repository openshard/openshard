"""``local_agent_history`` connector: agent session logs on this machine.

Read-only. Two layouts are supported, both as their agents write them:

* Claude Code: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``, then
  ``projects/<project-slug>/<session-id>.jsonl``. Subagent transcripts under
  ``<session-id>/subagents/`` are not separate sessions and are not listed.
* Codex: ``$CODEX_HOME`` or ``~/.codex``, then
  ``sessions/YYYY/MM/DD/rollout-*.jsonl``.

Objects are listed in a stable order (sorted by ``object_id``), so the
opaque cursor ``{"after": object_id}`` resumes discovery deterministically.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import BinaryIO

from openshard.ingest.connectors.base import (
    ACCESS_MISSING,
    ACCESS_OK,
    AccessStatus,
    ConnectorIOError,
)
from openshard.ingest.model import SourceObject

CONNECTOR_KIND = "local_agent_history"

SOURCE_CLAUDE_CODE = "claude-code"
SOURCE_CODEX = "codex"


def claude_home(env: Mapping[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    configured = env.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def codex_home(env: Mapping[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    configured = env.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


class LocalAgentHistoryConnector:
    """One local agent's history directory."""

    kind = CONNECTOR_KIND

    def __init__(self, source: str, root: Path, pattern: str, parser_hint: str, label: str) -> None:
        self.source = source
        self.root = root
        self.pattern = pattern
        self.parser_hint = parser_hint
        self.label = label

    def describe(self) -> str:
        return f"{self.label} history"

    def check_access(self) -> AccessStatus:
        try:
            if not self.root.is_dir():
                return AccessStatus(ACCESS_MISSING, f"{self.label} history directory not found")
        except OSError as exc:
            return AccessStatus(ACCESS_MISSING, type(exc).__name__)
        return AccessStatus(ACCESS_OK)

    def _object(self, path: Path) -> SourceObject | None:
        try:
            st = path.stat()
        except OSError:
            return None
        rel = path.relative_to(self.root).as_posix()
        return SourceObject(
            connector=self.kind,
            object_id=f"{self.source}:{rel}",
            locator=str(path),
            size=st.st_size,
            mtime=st.st_mtime,
            etag=f"{st.st_size}:{int(st.st_mtime_ns)}",
            hint=self.parser_hint,
        )

    def discover(self, cursor: dict | None = None) -> Iterator[SourceObject]:
        if not self.check_access().ok:
            return
        after = cursor.get("after") if isinstance(cursor, dict) else None
        try:
            paths = [p for p in self.root.glob(self.pattern) if p.is_file()]
        except OSError as exc:
            raise ConnectorIOError(f"listing failed: {type(exc).__name__}") from None
        objs = [o for o in (self._object(p) for p in paths) if o is not None]
        for obj in sorted(objs, key=lambda o: o.object_id):
            if isinstance(after, str) and obj.object_id <= after:
                continue
            yield obj

    def open(self, obj: SourceObject) -> BinaryIO:
        try:
            return open(obj.locator, "rb")  # noqa: SIM115 -- the caller owns the stream
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ConnectorIOError(f"open failed: {type(exc).__name__}") from None

    def stat(self, obj: SourceObject) -> SourceObject:
        fresh = self._object(Path(obj.locator))
        if fresh is None:
            raise FileNotFoundError(obj.object_id)
        return fresh


def claude_code_connector(env: Mapping[str, str] | None = None) -> LocalAgentHistoryConnector:
    return LocalAgentHistoryConnector(
        SOURCE_CLAUDE_CODE, claude_home(env) / "projects", "*/*.jsonl", "claude_code_jsonl", "Claude Code"
    )


def codex_connector(env: Mapping[str, str] | None = None) -> LocalAgentHistoryConnector:
    return LocalAgentHistoryConnector(
        SOURCE_CODEX, codex_home(env) / "sessions", "*/*/*/rollout-*.jsonl", "codex_rollout", "Codex"
    )
