"""Connector and parser registry.

Adding a source (a new agent history, an upload, a blob store) means adding
an entry here; the pipeline and job runner never change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from openshard.ingest.connectors.base import SourceConnector
from openshard.ingest.connectors.local_agent_history import (
    SOURCE_CLAUDE_CODE,
    SOURCE_CODEX,
    claude_code_connector,
    codex_connector,
)
from openshard.ingest.model import SourceObject
from openshard.ingest.parsers.base import Parser
from openshard.ingest.parsers.claude_code import ClaudeCodeParser
from openshard.ingest.parsers.codex import CodexParser

PARSERS: dict[str, Parser] = {p.name: p for p in (ClaudeCodeParser(), CodexParser())}

# Parser name -> receipt executor (history/shard.py HISTORICAL_IMPORT_LABELS)
# and the live-capture executor whose records describe the same sessions.
EXECUTOR_FOR_PARSER: dict[str, str] = {
    "claude_code_jsonl": "claude_code_history_import",
    "codex_rollout": "codex_history_import",
}
LIVE_EXECUTOR_FOR_AGENT: dict[str, str] = {"claude_code": "claude_code_hooks", "codex": "codex_hooks"}


@dataclass(frozen=True)
class SourceSpec:
    name: str  # CLI name: "claude-code" | "codex"
    label: str
    connector: Callable[[Mapping[str, str] | None], SourceConnector]


SOURCES: dict[str, SourceSpec] = {
    SOURCE_CLAUDE_CODE: SourceSpec(SOURCE_CLAUDE_CODE, "Claude Code", claude_code_connector),
    SOURCE_CODEX: SourceSpec(SOURCE_CODEX, "Codex", codex_connector),
}


def select_parser(head: bytes, obj: SourceObject) -> Parser | None:
    """The connector's hinted parser if it claims the bytes, else the best sniffer."""
    hinted = PARSERS.get(obj.hint or "")
    if hinted is not None and hinted.sniff(head, obj) >= 0.5:
        return hinted
    best, score = None, 0.5
    for parser in PARSERS.values():
        s = parser.sniff(head, obj)
        if s > score:
            best, score = parser, s
    return best
