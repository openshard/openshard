"""The one exception type the benchmark raises for a loud, classified failure."""

from __future__ import annotations

from typing import Any


class BenchmarkError(RuntimeError):
    """A benchmark precondition or stage failed and nothing was substituted.

    ``code`` is a stable, machine-readable token (``clone_failed``,
    ``commit_unavailable``, ``claude_cli_missing``, ...) written into
    ``benchmark.json`` so a reader can tell *why* a run has no result.
    """

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}
