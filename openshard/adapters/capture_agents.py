"""Identity of the coding agents whose sessions OpenShard captures (PR12).

One small, static table -- not a framework. ``adapters/claude_hooks.py``
folds a session into a ``runs.jsonl`` record through exactly one code path
regardless of which agent produced the hook stream; the only things that
differ per agent are the labels and identifiers stamped on the record, and
those live here so the fold logic never branches on an agent name.

Canonical identity rules (see ``history/shard.py``):

* ``executor`` is what ``derive_shard_identity`` keys on: every value here
  is in ``_EXTERNAL_ADAPTER_EXECUTORS``, so a captured session is always
  ``external_observed`` / ``partial`` -- never OpenShard-controlled.
* ``import_source`` is the canonical Event ``actor``; ``event_source`` is
  the Event ``source`` (mechanical bookkeeping, never a truth claim).
* ``vendor`` is a fact about the *agent product*, not the model provider:
  Claude Code is Anthropic's, Codex is OpenAI's; OpenCode is vendor-neutral
  and the underlying provider/model are recorded only when OpenCode itself
  exposes them (``provider_id`` on the status observation). OpenCode is
  never collapsed into the model provider.
"""

from __future__ import annotations

from dataclasses import dataclass

AGENT_CLAUDE_CODE = "claude_code"
AGENT_CODEX = "codex"
AGENT_OPENCODE = "opencode"


@dataclass(frozen=True)
class AgentProfile:
    key: str
    label: str  # human label used in Event action text and summaries
    vendor: str | None  # vendor of the agent product (not the model provider)
    executor: str  # runs.jsonl ``executor``; keyed on by history/shard.py
    import_source: str  # Event actor / runs.jsonl ``import_source``
    import_method: str
    event_source: str  # Event ``source``; see history/event.py SOURCE_*
    capture_source: str  # runs.jsonl ``capture.source``
    hook_evidence_source: str  # metadata.evidence_source on hook-reported file events
    files_source_label: str  # runs.jsonl ``files_source`` when git is unavailable
    model_source: str  # capture.model_source when the hook stream itself names the model
    usage_provenance: str  # cost/tokens provenance stamped for this agent's usage reports
    task_placeholder: str
    import_note: str


CLAUDE_CODE_PROFILE = AgentProfile(
    key=AGENT_CLAUDE_CODE,
    label="Claude Code",
    vendor="Anthropic",
    executor="claude_code_hooks",
    import_source="claude_code",
    import_method="openshard_claude_hooks_v0",
    event_source="claude_code_hooks",
    capture_source="claude_code_hooks",
    hook_evidence_source="claude_hook",
    files_source_label="claude_hook_reported",
    model_source="status_line",
    usage_provenance="provider_reported",
    task_placeholder="Claude Code session (task not captured)",
    import_note=(
        "Captured automatically from Claude Code lifecycle hooks. "
        "Tool/file facts are as reported by Claude Code; files are inferred from git diff. "
        "Model/cost/tokens are read from Claude Code's status line when one is configured "
        "(see `openshard mcp install claude`); otherwise they stay Unknown/Not recorded. "
        "Verification is never recorded by OpenShard for this capture path."
    ),
)

CODEX_PROFILE = AgentProfile(
    key=AGENT_CODEX,
    label="Codex",
    vendor="OpenAI",
    executor="codex_hooks",
    import_source="codex",
    import_method="openshard_codex_hooks_v0",
    event_source="codex_hooks",
    capture_source="codex_hooks",
    hook_evidence_source="codex_hook",
    files_source_label="codex_hook_reported",
    model_source="codex_hook",
    usage_provenance="agent_reported",
    task_placeholder="Codex session (task not captured)",
    import_note=(
        "Captured automatically from Codex lifecycle hooks. "
        "Tool/file facts are as reported by Codex; files are inferred from git diff. "
        "The model slug is the one Codex reports in its hook payloads; cost and token "
        "counts are not exposed by Codex hooks and stay Not recorded. "
        "Verification is never recorded by OpenShard for this capture path."
    ),
)

OPENCODE_PROFILE = AgentProfile(
    key=AGENT_OPENCODE,
    label="OpenCode",
    vendor=None,
    executor="opencode_plugin",
    import_source="opencode",
    import_method="openshard_opencode_plugin_v0",
    event_source="opencode_plugin",
    capture_source="opencode_plugin",
    hook_evidence_source="opencode_plugin",
    files_source_label="opencode_plugin_reported",
    model_source="opencode_plugin",
    usage_provenance="agent_reported",
    task_placeholder="OpenCode session (task not captured)",
    import_note=(
        "Captured automatically from the OpenShard OpenCode plugin. "
        "Tool/file facts are as reported by OpenCode; files are inferred from git diff. "
        "Provider/model identity and cost/token counts are the values OpenCode reports "
        "on its own assistant messages, recorded only when present. "
        "Verification is never recorded by OpenShard for this capture path."
    ),
)

AGENT_PROFILES: dict[str, AgentProfile] = {
    p.key: p for p in (CLAUDE_CODE_PROFILE, CODEX_PROFILE, OPENCODE_PROFILE)
}
CAPTURE_EXECUTORS: frozenset[str] = frozenset(p.executor for p in AGENT_PROFILES.values())


def profile_for(agent: object) -> AgentProfile:
    """The profile for an agent key; unknown/absent keys mean Claude Code.

    Buffers and queue lines written before PR12 carry no ``agent`` field at
    all, and every one of them was a Claude Code session -- so the default
    is a compatibility rule, not a guess.
    """
    if isinstance(agent, str) and agent in AGENT_PROFILES:
        return AGENT_PROFILES[agent]
    return CLAUDE_CODE_PROFILE


def agent_for_executor(executor: object) -> str:
    """Agent key for a persisted ``executor`` value (default: Claude Code)."""
    for profile in AGENT_PROFILES.values():
        if profile.executor == executor:
            return profile.key
    return AGENT_CLAUDE_CODE


def is_known_agent(agent: object) -> bool:
    return isinstance(agent, str) and agent in AGENT_PROFILES
