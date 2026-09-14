"""Shared helpers for the contracts package. No I/O."""

from __future__ import annotations

import datetime
from dataclasses import asdict, is_dataclass
from typing import Any


def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def principal(kind: str, ident: str | None, display: str | None = None, source: str | None = None) -> dict:
    """A Receipt Contract v2 ``Principal`` dict."""
    return {"kind": kind, "id": ident, "display": display or ident, "source": source}


def to_dict(obj: Any) -> dict:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return dict(obj)
