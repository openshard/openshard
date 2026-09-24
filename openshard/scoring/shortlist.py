from __future__ import annotations

import re

from openshard.providers.manager import InventoryEntry

# Family substrings, deliberately version-free where possible: which models
# may be *routed* is decided by curation (scoring.filter.filter_unpromoted
# gates the inventory to curated/explicit ids), not by pinning a version here.
TRUSTED_FAMILIES = [
    "claude-opus",
    "claude-sonnet",
    "claude-haiku",
    "gpt-5",
    "grok",
    "gemini-3.1",
    "glm",
    "kimi-k2",
    "minimax",
    "deepseek",
    "qwen",
    "gemma",
    "gpt-oss",
]

_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

# Non-decimal version slugs that _VERSION_RE cannot match.
# Maps a substring of the model ID to its (major, minor) version.
_VERSION_ALIAS: dict[str, tuple[int, int]] = {
    "v4-flash": (4, 0),
    "v4-pro":   (4, 0),
}


def is_trusted_model(model_id: str) -> bool:
    mid = model_id.lower().lstrip("~")
    # strip provider prefix (e.g. "anthropic/", "openai/")
    if "/" in mid:
        mid = mid.split("/", 1)[1]
    return any(family in mid for family in TRUSTED_FAMILIES)


def is_alias(model_id: str) -> bool:
    return model_id.startswith("~")


def extract_version(model_id: str) -> tuple[int, ...] | None:
    for slug, ver in _VERSION_ALIAS.items():
        if slug in model_id:
            return ver
    m = _VERSION_RE.search(model_id)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _family_key(model_id: str) -> str:
    """Return the matched trusted family substring for grouping."""
    mid = model_id.lower().lstrip("~")
    if "/" in mid:
        mid = mid.split("/", 1)[1]
    for family in TRUSTED_FAMILIES:
        if family in mid:
            return family
    return mid


def build_shortlist(entries: list[InventoryEntry]) -> list[InventoryEntry]:
    trusted = [e for e in entries if is_trusted_model(e.model.id)]
    # If no trusted models found, don't discard the whole pool.
    if not trusted:
        return entries

    groups: dict[str, list[InventoryEntry]] = {}
    for entry in trusted:
        key = _family_key(entry.model.id)
        groups.setdefault(key, []).append(entry)

    result = []
    for group in groups.values():
        versioned = [e for e in group if not is_alias(e.model.id) and extract_version(e.model.id) is not None]
        pool = versioned if versioned else group
        best = max(pool, key=lambda e: (extract_version(e.model.id) or (0,), e.provider))
        result.append(best)

    return result
