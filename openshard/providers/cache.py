from __future__ import annotations

import json
import time
from pathlib import Path

from openshard.providers.base import ModelInfo
from openshard.util.home import openshard_home

CACHE_TTL_HOURS: int = 24
# Resolved once at import (``OPENSHARD_HOME`` honoured); tests repoint it.
CACHE_PATH: Path = Path(openshard_home()) / "model_cache.json"


def load_cache() -> dict | None:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_stale(cached_at: float, ttl_hours: int = CACHE_TTL_HOURS) -> bool:
    return time.time() - cached_at > ttl_hours * 3600


def build_cache_entry(provider_name: str, models: list[ModelInfo]) -> dict:
    return {
        provider_name: [
            {
                "id": m.id,
                "name": m.name,
                "pricing": m.pricing,
                "context_window": m.context_window,
                "max_output_tokens": m.max_output_tokens,
                "supports_vision": m.supports_vision,
                "supports_tools": m.supports_tools,
            }
            for m in models
        ]
    }
