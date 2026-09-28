"""Agent Budgets V1: hard per-run limits the OSN loop can enforce truthfully.

Experimental. A budget is only enforced when the Platform says the
``agent_budgets`` capability is on for the linked organisation
(``openshard.sync.capabilities``); otherwise ``openshard osn run`` behaves
exactly as before and a configured budget is recorded as *not enforced*.

Configuration lives in the repository's ``.openshard/config.yml``::

    agent_budgets:
      max_spend_usd: 0.50   # estimated model spend, summed over every call
      max_attempts: 3       # loop attempts (each = one model-driven proposal + verify)
      max_commands: 3       # verify-command launches by OpenShard
      max_writes: 10        # whole-file writes applied to the isolated copy

Each limit is checked at the last boundary before that work would happen:
before a model call, before an attempt starts, before the verify command is
launched, before a file is written. Once a limit is reached the run stops
with status ``budget_exhausted``; nothing retries past it.

What the numbers mean, honestly:

* Spend is an *estimate*: the provider's own figure when it reports one
  (OpenRouter), otherwise OpenShard's price table applied to the token
  counts. It is known only after a call answers, so the check runs *between*
  calls: one call can overshoot and the Receipt records the estimated total.
  A call with no usable cost makes spend unobservable; with a spend limit
  set, the run stops rather than pretending the cost was zero.
* Commands are the verify-command launches OpenShard itself performs. What
  that command spawns is not counted.
* Writes are files written into the isolated working copy. Promoting
  verified files afterwards is a separate, policy-gated step and is not
  counted again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CAPABILITY = "agent_budgets"
CONFIG_KEY = "agent_budgets"

LIMIT_SPEND = "max_spend_usd"
LIMIT_ATTEMPTS = "max_attempts"
LIMIT_COMMANDS = "max_commands"
LIMIT_WRITES = "max_writes"
LIMIT_KEYS = (LIMIT_SPEND, LIMIT_ATTEMPTS, LIMIT_COMMANDS, LIMIT_WRITES)

ACTION_NONE = "none"
ACTION_STOP_MODEL_CALL = "stopped_before_model_call"
ACTION_STOP_SPEND_UNOBSERVABLE = "stopped_spend_unobservable"
ACTION_STOP_ATTEMPT = "stopped_before_attempt"
ACTION_STOP_COMMAND = "stopped_before_command"
ACTION_STOP_WRITE = "stopped_before_write"

STATUS_BUDGET_EXHAUSTED = "budget_exhausted"
STOP_REASON_PREFIX = "budget_"

# Attempts, commands and writes are counted by OpenShard as it performs them;
# spend is the per-call estimate the provider layer reports (see module doc).
EVIDENCE = {"counts": "openshard_observed", "spend": "provider_usage_estimate"}


class BudgetExhausted(Exception):
    """A hard limit would be exceeded by the next unit of work; the loop stops."""

    def __init__(self, limit: str, action: str) -> None:
        super().__init__(f"{limit}: {action}")
        self.limit = limit
        self.action = action

    @property
    def stop_reason(self) -> str:
        if self.action == ACTION_STOP_SPEND_UNOBSERVABLE:
            return f"{STOP_REASON_PREFIX}spend_unobservable"
        return f"{STOP_REASON_PREFIX}{self.limit}"


@dataclass(frozen=True)
class BudgetLimits:
    max_spend_usd: float | None = None
    max_attempts: int | None = None
    max_commands: int | None = None
    max_writes: int | None = None

    @property
    def configured(self) -> bool:
        return any(getattr(self, k) is not None for k in LIMIT_KEYS)

    def to_dict(self) -> dict[str, float | int]:
        """Configured limits only, in a fixed order."""
        return {k: getattr(self, k) for k in LIMIT_KEYS if getattr(self, k) is not None}

    @classmethod
    def from_config(cls, block: Any) -> BudgetLimits:
        """Parse the ``agent_budgets`` config block. Raises ``ValueError`` on anything unclear.

        ``None`` / missing means no budget. Every key must be one of the four
        limits; spend is a number above zero, counts are whole numbers of at
        least one. A budget you cannot read is refused, not guessed.
        """
        if block is None:
            return cls()
        if not isinstance(block, dict):
            raise ValueError(f"'{CONFIG_KEY}' must be a mapping of limits")
        unknown = sorted(str(k) for k in block if k not in LIMIT_KEYS)
        if unknown:
            raise ValueError(
                f"'{CONFIG_KEY}' has unknown key(s) {', '.join(unknown)}; allowed: {', '.join(LIMIT_KEYS)}"
            )
        values: dict[str, Any] = {}
        for key, raw in block.items():
            if raw is None:
                continue
            if isinstance(raw, bool):
                raise ValueError(f"'{CONFIG_KEY}.{key}' must be a number, not a boolean")
            if key == LIMIT_SPEND:
                if not isinstance(raw, (int, float)) or raw != raw or raw <= 0 or raw == float("inf"):
                    raise ValueError(f"'{CONFIG_KEY}.{key}' must be a positive amount in USD")
                values[key] = float(raw)
            else:
                if isinstance(raw, float) and raw.is_integer():
                    raw = int(raw)
                if not isinstance(raw, int) or raw < 1:
                    raise ValueError(f"'{CONFIG_KEY}.{key}' must be a whole number of at least 1")
                values[key] = int(raw)
        return cls(**values)


@dataclass
class BudgetLedger:
    """Counts what actually happened and refuses the unit of work that would break a limit.

    Shared by the loop (attempts, commands, writes) and the model provider
    (spend), so a retry or a re-ask cannot bypass it.
    """

    limits: BudgetLimits
    spend_usd: float = 0.0
    spend_known: bool = True  # False once any call reported no cost
    model_calls: int = 0
    attempts: int = 0
    commands: int = 0
    writes: int = 0
    limit_reached: str | None = None
    action: str = ACTION_NONE

    # -- checks, each at the last boundary before the work ---------------------

    def start_attempt(self) -> None:
        """Refuse an attempt that is out of attempts, or that could never be verified or applied.

        Every attempt costs a model call before it can write or verify. When
        the command or write budget is already spent, that call would buy
        nothing, so the stop happens here, before the spend.
        """
        cap = self.limits.max_attempts
        if cap is not None and self.attempts >= cap:
            self._stop(LIMIT_ATTEMPTS, ACTION_STOP_ATTEMPT)
        cap_c = self.limits.max_commands
        if cap_c is not None and self.commands >= cap_c:
            self._stop(LIMIT_COMMANDS, ACTION_STOP_ATTEMPT)
        cap_w = self.limits.max_writes
        if cap_w is not None and self.writes >= cap_w:
            self._stop(LIMIT_WRITES, ACTION_STOP_ATTEMPT)
        self.attempts += 1
        if cap is not None and self.attempts >= cap and self.limit_reached is None:
            self.limit_reached = LIMIT_ATTEMPTS

    def would_stop_next_attempt(self) -> str | None:
        """The limit that would refuse another attempt (or its first model call), or None.

        A non-raising look-ahead for callers that must not pre-empt the budget's
        own stop: when this returns a limit, the next attempt cannot happen.
        """
        if self.limits.max_attempts is not None and self.attempts >= self.limits.max_attempts:
            return LIMIT_ATTEMPTS
        if self.limits.max_commands is not None and self.commands >= self.limits.max_commands:
            return LIMIT_COMMANDS
        if self.limits.max_writes is not None and self.writes >= self.limits.max_writes:
            return LIMIT_WRITES
        cap = self.limits.max_spend_usd
        if cap is not None and (not self.spend_known or self.spend_usd >= cap):
            return LIMIT_SPEND
        return None

    def before_model_call(self) -> None:
        cap = self.limits.max_spend_usd
        if cap is None:
            return
        if not self.spend_known:
            self._stop(LIMIT_SPEND, ACTION_STOP_SPEND_UNOBSERVABLE, reached=False)
        if self.spend_usd >= cap:
            self._stop(LIMIT_SPEND, ACTION_STOP_MODEL_CALL)

    def record_model_call(self, cost_usd: float | None) -> None:
        self.model_calls += 1
        if cost_usd is None:
            self.spend_known = False
        else:
            self.spend_usd += float(cost_usd)
        cap = self.limits.max_spend_usd
        if cap is not None and self.spend_known and self.spend_usd >= cap and self.limit_reached is None:
            self.limit_reached = LIMIT_SPEND

    def authorize_command(self) -> None:
        cap = self.limits.max_commands
        if cap is not None and self.commands >= cap:
            self._stop(LIMIT_COMMANDS, ACTION_STOP_COMMAND)
        self.commands += 1
        if cap is not None and self.commands >= cap and self.limit_reached is None:
            self.limit_reached = LIMIT_COMMANDS

    def authorize_write(self) -> None:
        cap = self.limits.max_writes
        if cap is not None and self.writes >= cap:
            self._stop(LIMIT_WRITES, ACTION_STOP_WRITE)
        self.writes += 1
        if cap is not None and self.writes >= cap and self.limit_reached is None:
            self.limit_reached = LIMIT_WRITES

    def _stop(self, limit: str, action: str, *, reached: bool = True) -> None:
        if reached and self.limit_reached is None:
            self.limit_reached = limit
        if self.action == ACTION_NONE:
            self.action = action
        raise BudgetExhausted(limit, action)

    # -- evidence -------------------------------------------------------------

    def to_record(self) -> dict[str, Any]:
        """What was configured, what was observed, what was reached, what OpenShard did."""
        return {
            "capability": CAPABILITY,
            "enforced": True,
            "limits": self.limits.to_dict(),
            "usage": {
                "spend_usd": round(self.spend_usd, 6) if self.spend_known else None,
                "spend_known": self.spend_known,
                "spend_is_estimate": True,
                "model_calls": self.model_calls,
                "attempts": self.attempts,
                "commands": self.commands,
                "writes": self.writes,
            },
            "limit_reached": self.limit_reached,
            "action": self.action,
            "evidence": dict(EVIDENCE),
        }


def not_enforced_record(limits: BudgetLimits, reason: str) -> dict[str, Any]:
    """A configured budget that did not apply, and why (capability off / unconfirmed)."""
    return {
        "capability": CAPABILITY,
        "enforced": False,
        "reason": reason,
        "limits": limits.to_dict(),
    }


__all__ = [
    "ACTION_NONE",
    "ACTION_STOP_ATTEMPT",
    "ACTION_STOP_COMMAND",
    "ACTION_STOP_MODEL_CALL",
    "ACTION_STOP_SPEND_UNOBSERVABLE",
    "ACTION_STOP_WRITE",
    "CAPABILITY",
    "CONFIG_KEY",
    "LIMIT_ATTEMPTS",
    "LIMIT_COMMANDS",
    "LIMIT_KEYS",
    "LIMIT_SPEND",
    "LIMIT_WRITES",
    "STATUS_BUDGET_EXHAUSTED",
    "BudgetExhausted",
    "BudgetLedger",
    "BudgetLimits",
    "not_enforced_record",
]
