"""Model stages for a session that was observed (not routed) across more than one model.

``capture.models_seen`` is namespaced metadata only the hook capture sets
(absent for every other producer). A session observed across more than one
model (a mid-session ``/model`` switch, or subagents on another model) must
not be presented as if one model ran it, so each observed model becomes an
"Observed ..." stage. The prefix matters: the Platform reads "Observed" as
observed, never routed.
"""

from __future__ import annotations

from collections.abc import Callable

_MAX_MODELS = 5
_ROLES = ("lead", "subagent")


def observed_model_stages(capture: object, display: Callable[[str], str]) -> list[tuple[str, str]]:
    """``[(stage, model display name)]`` for two or more observed models, else ``[]``.

    Stages are "Observed 1", "Observed 2"... in capture order, or "Observed
    lead" / "Observed subagent" (numbered when a role has several models)
    when ``capture.model_roles`` -- recorded only when the transcript proved
    it -- names a role for every model.
    """
    block = capture if isinstance(capture, dict) else {}
    models = [m for m in (block.get("models_seen") or []) if isinstance(m, str) and m][:_MAX_MODELS]
    if len(models) < 2:
        return []
    roles = block.get("model_roles")
    if not isinstance(roles, dict) or not all(roles.get(m) in _ROLES for m in models):
        return [(f"Observed {i + 1}", display(m)) for i, m in enumerate(models)]
    counts = {r: sum(1 for m in models if roles[m] == r) for r in _ROLES}
    seen: dict[str, int] = {}
    stages: list[tuple[str, str]] = []
    for m in models:
        role = roles[m]
        seen[role] = seen.get(role, 0) + 1
        stages.append((f"Observed {role}" + (f" {seen[role]}" if counts[role] > 1 else ""), display(m)))
    return stages


__all__ = ["observed_model_stages"]
