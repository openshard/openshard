"""``SourceConnector`` protocol (docs/architecture/historical-ingestion.md §4).

A connector knows *where* bytes live: local agent history today; uploads,
Drive, S3, Dropbox or GitHub later. It never parses and never imports a
storage module. The discovery cursor is opaque so paging connectors (Drive
page tokens, S3 continuation tokens) fit unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable

from openshard.ingest.model import SourceObject

ACCESS_OK = "ok"
ACCESS_MISSING = "missing"
ACCESS_DENIED = "denied"


@dataclass(frozen=True)
class AccessStatus:
    status: str  # ok | missing | denied
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == ACCESS_OK


class ConnectorIOError(OSError):
    """A transient read failure. The job runner retries these with backoff."""


class ConnectorFatalError(RuntimeError):
    """Access is gone (revoked, root removed). Fails the job; never degrades silently."""


@runtime_checkable
class SourceConnector(Protocol):
    kind: str

    def describe(self) -> str: ...

    def check_access(self) -> AccessStatus: ...

    def discover(self, cursor: dict | None = None) -> Iterator[SourceObject]: ...

    def open(self, obj: SourceObject) -> BinaryIO: ...

    def stat(self, obj: SourceObject) -> SourceObject: ...
