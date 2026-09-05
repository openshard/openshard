"""OpenShard package root.

``__version__`` is resolved lazily (PEP 562 module ``__getattr__``): reading
installed-package metadata via ``importlib.metadata.version`` costs tens of
milliseconds, which is pure waste for the many callers (notably the Claude
Code hook/status-line entrypoints) that import ``openshard`` without ever
needing the version string.
"""

from __future__ import annotations

from typing import Any

_FALLBACK_VERSION = "0.4.0-dev"


def __getattr__(name: str) -> Any:
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("openshard")
        except PackageNotFoundError:
            return _FALLBACK_VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
