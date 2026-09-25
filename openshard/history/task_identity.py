"""Task identity (``task_id``).

A ``task_id`` identifies one explicitly declared engineering task across
attempts, agents and potentially repositories. It is a third identity,
distinct from both:

* ``receipt_id`` -- identifies one persisted Receipt record.
* ``shard_id`` -- this repository's history-position/grouping key for
  attempts (see ``shard_contract._make_shard_id``).

``task_id`` is additive: it can be attached to a Shard/RunAttempt/Receipt
without changing what ``shard_id`` or ``receipt_id`` mean or how they are
minted.

Format: ``task_`` + a canonical UUIDv7 string (e.g.
``task_018f4d2a-1c3e-7000-8b1a-0242ac120002``). UUIDv7 is opaque and
globally unique; nothing about a repository, prompt, user or agent is
encoded into it, and no human-readable slug is ever treated as canonical
identity.

Never inferred
---------------
A ``task_id`` is never guessed or reconstructed from prompt text, prompt
similarity, timestamps, repository state or ``shard_id``. It exists only
when a caller explicitly supplies one.

``ensure_task_id`` never mints
-------------------------------
Unlike ``receipt_identity.ensure_receipt_id`` (which auto-mints at record
creation), ``ensure_task_id`` only stamps a **caller-supplied** id; called
with no id, it leaves the entry untouched. ``new_task_id`` exists as a
dedicated helper, but it must only be called from an explicit
task-creation path (``openshard task new``) -- never from a fold, replay,
import or attempt-linking path.

Old records
-----------
Records written before ``task_id`` existed simply have no ``task_id``
field. They remain fully valid; nothing back-fills or assigns them a task
after the fact.

Declared launch context (external-agent capture)
-------------------------------------------------
An external agent session (Claude Code, Codex, ...) may be launched with
``OPENSHARD_TASK_ID=<task id>`` in its environment. That is an explicit
*declaration by the person launching the agent* -- never an inference -- and
it is the only way a hook-captured session acquires a ``task_id``. Several
sessions (of one agent or of different agents) may declare the same id;
they stay separate Receipts, one per agent session. The declaration is
recorded as ``EVIDENCE_DECLARED`` launch context; what the hooks then
observe stays what it always was. See :func:`launch_task_id` and
``adapters/claude_hooks._bind_task_context``.

No retroactive assignment (v0.5.0 Platform/Sync constraint)
-------------------------------------------------------------
``task_id`` must be established before or at the creation of the work that
belongs to a task: mint with ``openshard task new``, then start new
work/attempts with that id via ``--task-id`` so the resulting Receipt
carries it from creation. There is deliberately no command or helper that
attaches or changes ``task_id`` on an already-persisted Receipt -- doing so
would change that Receipt's stored content (and therefore its
``content_hash``) after the fact, which is Receipt-revision semantics and
out of scope here, and would be invisible to any system (e.g. Platform)
that already synced the Receipt's original content.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping

TASK_ID_FIELD = "task_id"
TASK_ID_PREFIX = "task_"

# Declared launch context for external-agent capture. The environment
# variable is the only place a launcher declares the task; the capture
# header is how it reaches the capture service, separately from (and never
# read out of) the agent's own payload.
TASK_ID_ENV = "OPENSHARD_TASK_ID"
TASK_CONTEXT_SOURCE_LAUNCH_ENV = "launch_environment"
# Evidence label of the declaration itself: stated by the launcher, neither
# observed by a hook nor reported by the agent nor verified by OpenShard.
EVIDENCE_DECLARED = "declared"

# Canonical UUID string form: 8-4-4-4-12 lowercase hex. Version nibble (the
# first hex digit of the 3rd group) must be "7"; variant nibble (the first
# hex digit of the 4th group) must be one of 8/9/a/b per RFC 9562.
_TASK_ID_RE = re.compile(
    r"^task_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def new_task_id() -> str:
    """Mint a fresh, globally unique task id (``task_`` + UUIDv7).

    Only for an explicit task-creation path (``openshard task new``).
    Never call this to backfill, infer or auto-assign a task for an
    existing record -- see module docstring.
    """
    return f"{TASK_ID_PREFIX}{_uuid7()}"


def _uuid7() -> str:
    """Build a canonical-form UUIDv7 string (RFC 9562), no dependency.

    48-bit big-endian millisecond Unix timestamp, then 74 random bits with
    the version (7) and variant (RFC 9562, ``10``) bits set in place.
    """
    import time

    unix_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand = int.from_bytes(os.urandom(10), "big")

    # 128 bits total: 48 (timestamp) + 4 (version) + 12 (rand_a) + 2 (variant) + 62 (rand_b)
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & 0x3FFFFFFFFFFFFFFF

    value = unix_ms << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b

    return str(uuid.UUID(int=value))


def is_task_id(value: object) -> bool:
    """True when *value* is a well-formed task id: ``task_`` + canonical UUIDv7.

    Checks the literal version (7) and variant (RFC 9562) bits, not merely
    that the string is 36 hex-and-dashes characters.
    """
    if not isinstance(value, str) or not _TASK_ID_RE.match(value):
        return False
    try:
        parsed = uuid.UUID(value[len(TASK_ID_PREFIX) :])
    except ValueError:
        return False
    return parsed.version == 7


def stored_task_id(entry: object) -> str | None:
    """The task id persisted on *entry*, or None. Never mints, never infers."""
    if not isinstance(entry, dict):
        return None
    value = entry.get(TASK_ID_FIELD)
    return value if is_task_id(value) else None


def launch_task_id(env: Mapping[str, str] | None = None) -> str | None:
    """The well-formed task id declared in *env*'s ``OPENSHARD_TASK_ID``, or None.

    Unset, empty and malformed values all read as "no declaration": nothing
    is guessed, repaired or normalised (a padded or re-cased id is
    malformed). Never raises.
    """
    source = os.environ if env is None else env
    try:
        value = source.get(TASK_ID_ENV)
    except Exception:
        return None
    return value if is_task_id(value) else None


def ensure_task_id(entry: dict, task_id: str | None = None) -> str | None:
    """Attach an explicitly supplied *task_id* to *entry* being created. Returns it.

    Never mints: with no *task_id* argument, *entry* is left untouched and
    ``None`` is returned. When a *task_id* is supplied, it must be a
    well-formed ``task_`` + UUIDv7 id and is stored exactly as given
    (never normalized, never regenerated).

    Only for writers at creation/attachment time -- read/render paths must
    use :func:`stored_task_id` so an old record is never given an identity
    it did not have when it was written.

    Raises ``ValueError`` when *task_id* is supplied but malformed
    (fail closed, like ``run_attempt.UnknownShardError``).
    """
    if task_id is None:
        return None
    if not is_task_id(task_id):
        raise ValueError(
            f"Malformed task_id {task_id!r}: expected 'task_' followed by a "
            "canonical UUIDv7."
        )
    entry[TASK_ID_FIELD] = task_id
    return task_id
