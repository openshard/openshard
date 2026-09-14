"""Opt-in local -> cloud receipt sync (v0.5).

Off unless an endpoint is configured; never runs on an agent hook path;
sends only the privacy-bounded projections in ``SyncEnvelope``. See
``docs/sync.md``.
"""

from openshard.sync.client import (
    ENDPOINT_ENV,
    TOKEN_ENV,
    HttpsSyncTransport,
    SyncConfig,
    SyncPushSummary,
    load_sync_state,
    push_entries,
    resolve_sync_config,
)

__all__ = [
    "ENDPOINT_ENV",
    "TOKEN_ENV",
    "HttpsSyncTransport",
    "SyncConfig",
    "SyncPushSummary",
    "load_sync_state",
    "push_entries",
    "resolve_sync_config",
]
