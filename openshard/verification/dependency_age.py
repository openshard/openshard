"""How recently a new dependency was published, from its public registry.

A package published days ago, or an old package with a release published
days ago, is where supply-chain compromises show up (a hijacked maintainer
account publishes a new version). For each package a change adds, this asks
the public registry (PyPI's JSON API, the npm registry) when the package
first appeared and when its newest release was published.

Off by default and network-bound: it runs only when a caller asks
(``openshard ci findings --registry-lookup``). Each new package's name is
sent to its public registry (https://pypi.org, https://registry.npmjs.org);
nothing else from the repository is. All lookups together get
``TOTAL_SECONDS`` (15 s); a lookup that fails, times out, is not reached in
time or returns something unexpected leaves the dates ``None`` and
``recent`` ``None`` (unknown), never "not recent".
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

RECENT_DAYS = 14
TIMEOUT_SECONDS = 5.0  # one lookup
TOTAL_SECONDS = 15.0  # every lookup for one change, together
MAX_LOOKUPS = 10
_SAFE_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]{0,213}$")

Fetch = Callable[[str], "Any | None"]  # url -> parsed JSON, or None


def fetch_json(url: str, timeout: float = TIMEOUT_SECONDS) -> Any | None:
    """GET *url* and parse JSON; None on any failure."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "openshard"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https hosts
            return json.loads(response.read(8_000_000))
    except Exception:
        return None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _pypi(data: Any) -> tuple[datetime | None, datetime | None]:
    releases = data.get("releases") if isinstance(data, dict) else None
    if not isinstance(releases, dict):
        return None, None
    stamps = [t for files in releases.values() if isinstance(files, list) for f in files
              if isinstance(f, dict) and (t := _when(f.get("upload_time_iso_8601")))]
    return (min(stamps), max(stamps)) if stamps else (None, None)


def _npm(data: Any) -> tuple[datetime | None, datetime | None]:
    times = data.get("time") if isinstance(data, dict) else None
    if not isinstance(times, dict):
        return None, None
    stamps = [t for k, v in times.items() if k not in ("created", "modified") and (t := _when(v))]
    created = _when(times.get("created"))
    first = min([*stamps, created] if created else stamps) if stamps or created else None
    return first, (max(stamps) if stamps else None)


def _url(ecosystem: str, name: str) -> str | None:
    if not _SAFE_NAME.match(name):
        return None
    if ecosystem == "pypi":
        return f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"
    if ecosystem == "npm":
        return "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@")
    return None


def _iso(stamp: datetime | None) -> str | None:
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else None


def with_ages(packages: Iterable[dict[str, str]], *, fetch: Fetch | None = None, now: datetime | None = None,
              recent_days: int = RECENT_DAYS, total_seconds: float = TOTAL_SECONDS,
              clock: Callable[[], float] = time.monotonic) -> list[dict[str, Any]]:
    """Each package with ``first_published``, ``latest_published`` and ``recent`` (True, False or None).

    Lookups stop once *total_seconds* have passed; the rest stay unknown.
    """
    deadline = clock() + total_seconds

    def remaining() -> float:
        return deadline - clock()

    def default_fetch(url: str) -> Any | None:
        return fetch_json(url, timeout=max(0.1, min(TIMEOUT_SECONDS, remaining())))

    fetch = fetch or default_fetch
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=recent_days)
    out: list[dict[str, Any]] = []
    for i, package in enumerate(packages):
        row: dict[str, Any] = {**package, "first_published": None, "latest_published": None, "recent": None}
        in_time = i < MAX_LOOKUPS and remaining() > 0
        url = _url(package.get("ecosystem", ""), package.get("name", "")) if in_time else None
        data = fetch(url) if url else None
        if data is not None:
            first, latest = (_pypi if package["ecosystem"] == "pypi" else _npm)(data)
            if first and latest:
                row.update(first_published=_iso(first), latest_published=_iso(latest),
                           recent=first > cutoff or latest > cutoff)
        out.append(row)
    return out


__all__ = ["MAX_LOOKUPS", "RECENT_DAYS", "TOTAL_SECONDS", "fetch_json", "with_ages"]
