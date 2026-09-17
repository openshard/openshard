"""Core -> Platform receipt sync (hosted Receipt history).

The canonical Receipt stays in this repository's ``.openshard/runs.jsonl``.
Sync sends a *copy* of the privacy-bounded machine receipt (the same
projection ``openshard history --json`` prints, ``history/views.py``) to an
OpenShard Platform organisation, wrapped in the receipt sync envelope v1.
Nothing here changes a stored record, mints an identity, or infers a task.

Modules
-------
``config``    the user-global Platform link (endpoint, organisation, API key)
``envelope``  canonical record -> sync envelope, plus the "quiescent" rule
``outbox``    per-repository sync state keyed by ``receipt_id``
``transport`` one HTTPS POST per receipt, with the response classified
``client``    discovery of unsynced receipts and the retry-safe flush loop

See ``docs/platform-sync.md`` for the user-facing contract.
"""
