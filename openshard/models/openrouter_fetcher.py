from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

OPENROUTER_MODELS_URL = "https://api.openrouter.ai/api/v1/models"
_DEFAULT_CACHE_PATH = Path.home() / ".openshard" / "openrouter-models.json"
SCHEMA_VERSION = "1"
_FETCH_TIMEOUT = 15


class OpenRouterFetchError(Exception):
    pass


class OpenRouterCacheError(Exception):
    pass


def fetch_openrouter_models() -> list[dict]:
    """Fetch model list from OpenRouter. Returns raw model dicts."""
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL,
        headers={"User-Agent": "openshard/1 (+https://github.com/openshard/openshard)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise OpenRouterFetchError(f"Network error — check your connection. ({exc})") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OpenRouterFetchError(f"Invalid JSON from OpenRouter: {exc}") from exc

    if not isinstance(payload, dict) or "data" not in payload:
        raise OpenRouterFetchError(
            "Unexpected response shape from OpenRouter (missing 'data' key)"
        )

    return payload["data"]


def normalize_model(raw: dict) -> dict:
    """Extract known fields from a raw OpenRouter model dict.

    Unknown fields are dropped. Missing fields default to None.
    Pricing values remain as strings (OpenRouter returns them that way).
    """
    arch_raw = raw.get("architecture") or {}
    pricing_raw = raw.get("pricing") or {}
    top_raw = raw.get("top_provider") or {}

    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "created": raw.get("created"),
        "description": raw.get("description"),
        "context_length": raw.get("context_length"),
        "architecture": {
            "modality": arch_raw.get("modality"),
            "input_modalities": arch_raw.get("input_modalities"),
            "output_modalities": arch_raw.get("output_modalities"),
            "tokenizer": arch_raw.get("tokenizer"),
            "instruct_type": arch_raw.get("instruct_type"),
        }
        if raw.get("architecture") is not None
        else None,
        "pricing": {
            "prompt": pricing_raw.get("prompt"),
            "completion": pricing_raw.get("completion"),
            "request": pricing_raw.get("request"),
            "image": pricing_raw.get("image"),
            "audio": pricing_raw.get("audio"),
            "input_cache_read": pricing_raw.get("input_cache_read"),
            "input_cache_write": pricing_raw.get("input_cache_write"),
            "web_search": pricing_raw.get("web_search"),
            "internal_reasoning": pricing_raw.get("internal_reasoning"),
        }
        if raw.get("pricing") is not None
        else None,
        "top_provider": {
            "context_length": top_raw.get("context_length"),
            "max_completion_tokens": top_raw.get("max_completion_tokens"),
            "is_moderated": top_raw.get("is_moderated"),
        }
        if raw.get("top_provider") is not None
        else None,
        "supported_parameters": raw.get("supported_parameters"),
        "per_request_limits": raw.get("per_request_limits"),
        "knowledge_cutoff": raw.get("knowledge_cutoff"),
        "expiration_date": raw.get("expiration_date"),
        "canonical_slug": raw.get("canonical_slug"),
    }


def load_openrouter_cache(path: Path | None = None) -> dict | None:
    """Load the OpenRouter cache file.

    Returns None if the file does not exist.
    Raises OpenRouterCacheError if the file exists but contains invalid JSON.
    """
    cache_path = path if path is not None else _DEFAULT_CACHE_PATH
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenRouterCacheError(
            f"Cache file is corrupt ({cache_path}): {exc}"
        ) from exc


def save_openrouter_cache(models: list[dict], path: Path | None = None) -> None:
    """Write normalized models to the cache file atomically."""
    cache_path = path if path is not None else _DEFAULT_CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    synced_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "synced_at": synced_at,
        "source": OPENROUTER_MODELS_URL,
        "model_count": len(models),
        "models": models,
    }

    fd, tmp = tempfile.mkstemp(dir=cache_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, cache_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
