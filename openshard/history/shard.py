"""Canonical Shard identity: the durable task, distinct from its Receipt.

A Shard answers "what task, by what agent, observed how completely" —
identity and origin only. Proof, evidence, and verification detail live on
the derived ``ShardReceipt`` (see ``shard_contract.py``), which embeds a
``Shard`` via ``build_shard()``.

v0 limitation: a Shard here still corresponds to one persisted run record —
``shard_id`` is derived per-run (see ``_make_shard_id`` in
``shard_contract.py``). This module does not yet give a task a stable
identity that persists across retries/attempts; a retried run is still a
sibling JSONL entry with its own fresh ``shard_id``. Persistent Shard
identity spanning multiple Run/Attempts is future work.
"""

from __future__ import annotations

from dataclasses import dataclass

ORIGIN_OPENSHARD_ROUTED = "openshard_routed"
ORIGIN_EXTERNAL_OBSERVED = "external_observed"
ORIGIN_UNKNOWN = "unknown"
VALID_ORIGINS = frozenset({ORIGIN_OPENSHARD_ROUTED, ORIGIN_EXTERNAL_OBSERVED, ORIGIN_UNKNOWN})

CAPTURE_FULL = "full"
CAPTURE_PARTIAL = "partial"
CAPTURE_UNKNOWN = "unknown"
VALID_CAPTURE_DEPTHS = frozenset({CAPTURE_FULL, CAPTURE_PARTIAL, CAPTURE_UNKNOWN})

# Executors OpenShard did not run itself — it only observed the git state an
# external coding agent left behind, or (for the ``*_hooks`` / ``*_plugin``
# executors) the lifecycle evidence the agent's own hooks/plugin handed it.
# Never invents verification, cost, or approval for these (see
# openshard/adapters/claude_code_import.py, openshard/adapters/wrap_exec.py,
# openshard/adapters/claude_hooks.py and openshard/adapters/capture_agents.py).
# The label names the *agent that did the work* — never the model provider:
# an OpenCode session observed through the plugin stays "OpenCode" whatever
# provider/model it used (PR12), and is distinct from ``executor ==
# "opencode"`` below, which is OpenShard *routing* work to OpenCode itself.
_EXTERNAL_AGENT_LABELS: dict[str, str] = {
    "claude_code_import": "Claude Code (external)",
    "claude_code_wrap": "Claude Code (external)",
    "claude_code_hooks": "Claude Code (external)",
    "codex_hooks": "Codex (external)",
    "opencode_plugin": "OpenCode (external)",
}
_EXTERNAL_ADAPTER_EXECUTORS = frozenset(_EXTERNAL_AGENT_LABELS)

_EXTERNAL_AGENT_LABEL = _EXTERNAL_AGENT_LABELS["claude_code_hooks"]


@dataclass
class Shard:
    """The durable task identity. No proof/evidence fields — see ShardReceipt."""

    shard_id: str
    created_at: str
    task_short: str
    task_full: str
    agent: str
    origin: str
    capture_depth: str


def derive_shard_identity(entry: dict) -> tuple[str, str, str]:
    """Return ``(agent_display, origin, capture_depth)`` for a raw run entry.

    Pure, never raises. ``origin``/``capture_depth`` are only ever
    ``ORIGIN_OPENSHARD_ROUTED``/``CAPTURE_FULL`` when there is a positive
    signal that OpenShard itself executed or directly controlled the run —
    never inferred merely from the absence of contrary evidence.
    """
    executor = entry.get("executor") or ""
    workflow = entry.get("workflow") or ""
    adapter = entry.get("adapter") or ""

    if executor in _EXTERNAL_ADAPTER_EXECUTORS:
        return _EXTERNAL_AGENT_LABELS[executor], ORIGIN_EXTERNAL_OBSERVED, CAPTURE_PARTIAL

    if workflow == "native" or executor == "native":
        return "OpenShard Native", ORIGIN_OPENSHARD_ROUTED, CAPTURE_FULL

    if workflow == "opencode" or executor == "opencode" or adapter == "opencode":
        return "OpenCode", ORIGIN_OPENSHARD_ROUTED, CAPTURE_FULL

    # _log_run (openshard/run/_pipeline_helpers.py) stamps a "retry_triggered"
    # key unconditionally into every entry the OpenShard run pipeline itself
    # writes, regardless of which workflow ran. Its presence is positive
    # evidence OpenShard executed the run even when the workflow has no
    # dedicated label above; its absence means this code cannot make that
    # claim, so origin/capture_depth stay unknown rather than assumed.
    if "retry_triggered" in entry:
        return "OpenShard", ORIGIN_OPENSHARD_ROUTED, CAPTURE_FULL

    return "OpenShard", ORIGIN_UNKNOWN, CAPTURE_UNKNOWN


def build_shard(
    entry: dict,
    *,
    shard_id: str,
    created_at: str,
    task_short: str,
    task_full: str,
) -> Shard:
    """Build the canonical Shard for a raw run entry. Never raises.

    Takes the identity fields already derived by the caller
    (``build_shard_receipt``) so ``shard_id``/``created_at``/task stay in
    sync with the receipt built from the same entry.
    """
    agent, origin, capture_depth = derive_shard_identity(entry)
    return Shard(
        shard_id=shard_id,
        created_at=created_at,
        task_short=task_short,
        task_full=task_full,
        agent=agent,
        origin=origin,
        capture_depth=capture_depth,
    )
