"""Did learning help? The measurable foundation, with no causal claim.

Every OSN Receipt since Learning Loop V1 says whether learning was used
(``learning.used``) and carries its own outcome: verification, attempts,
retries, cost and duration. This module puts those side by side:

* cohorts: runs that used learning, runs that consulted it and found
  nothing relevant (or had it turned off), and OSN runs recorded before the
  block existed;
* per signal: the later runs that were given it, and how they ended.

Everything is counts over observed evidence with sample sizes attached. A
rate is shown only with ``MIN_COHORT_FOR_RATE`` runs behind it. Missing cost
is never zero: a cohort's cost per verified success is reported only when
every run in it has a known cost. Comparing cohorts is observational: the
tasks that found relevant history are not a random sample, so a difference
is a lead to investigate, never evidence that learning caused it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openshard.learning.signals import LearningIndex, Observation, median

MIN_COHORT_FOR_RATE = 5
MAX_SIGNAL_FOLLOWUPS = 10

COHORT_USED = "learning_used"
COHORT_NOT_USED = "learning_not_used"
COHORT_UNRECORDED = "before_learning_recorded"

DISCLAIMER = (
    "Observational comparison. Tasks that found relevant history are not a random sample; "
    "a difference is a lead to investigate, not evidence that learning caused it."
)


@dataclass(frozen=True)
class Cohort:
    name: str
    runs: int
    observed: int  # runs with an OpenShard-observed verification outcome
    verified_successes: int
    first_attempt_passed: int
    retried: int
    attempts_total: int
    cost_known: int
    cost_per_verified_success_usd: float | None
    median_duration_seconds: float | None

    @property
    def rates_shown(self) -> bool:
        return self.observed >= MIN_COHORT_FOR_RATE

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "cohort": self.name,
            "runs": self.runs,
            "observed_outcomes": self.observed,
            "verified_successes": self.verified_successes,
            "first_attempt_passed": self.first_attempt_passed,
            "runs_retried": self.retried,
            "attempts_total": self.attempts_total,
            "cost_known_runs": self.cost_known,
            "cost_per_verified_success_usd": self.cost_per_verified_success_usd,
            "median_duration_seconds": self.median_duration_seconds,
            "rates_shown": self.rates_shown,
        }
        if self.rates_shown:
            out["verified_success_rate"] = round(self.verified_successes / self.observed, 3)
            out["first_attempt_pass_rate"] = round(self.first_attempt_passed / self.observed, 3)
        return out


def _cohort(name: str, obs: list[Observation]) -> Cohort:
    observed = [o for o in obs if o.observed]
    successes = sum(1 for o in observed if o.verified_success)
    cost = None
    if obs and successes and all(o.cost_usd is not None for o in obs):
        cost = round(sum(o.cost_usd or 0.0 for o in obs) / successes, 6)
    return Cohort(
        name=name,
        runs=len(obs),
        observed=len(observed),
        verified_successes=successes,
        first_attempt_passed=sum(1 for o in observed if o.attempts and o.attempts[0].state == "passed"),
        retried=sum(1 for o in obs if o.retried),
        attempts_total=sum(len(o.attempts) for o in obs),
        cost_known=sum(1 for o in obs if o.cost_usd is not None),
        cost_per_verified_success_usd=cost,
        median_duration_seconds=median([o.duration_seconds for o in obs if o.duration_seconds is not None]),
    )


@dataclass(frozen=True)
class SignalFollowUp:
    signal_id: str
    later_runs: int
    observed: int
    verified_successes: int
    first_attempt_passed: int

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ImpactReport:
    repo: str | None
    task_category: str | None
    cohorts: list[Cohort]
    followups: list[SignalFollowUp] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def cohort(self, name: str) -> Cohort | None:
        return next((c for c in self.cohorts if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "task_category": self.task_category,
            "cohorts": [c.to_dict() for c in self.cohorts],
            "signal_followups": [f.to_dict() for f in self.followups],
            "min_cohort_for_rate": MIN_COHORT_FOR_RATE,
            "disclaimer": self.disclaimer,
        }


def measure(index: LearningIndex, *, task_category: str | None = None) -> ImpactReport:
    """Outcomes of OSN runs with and without learning, from *index*'s observations."""
    osn = [o for o in index.observations if o.harness == "osn_loop"
           and (task_category is None or o.task_category == task_category)]
    osn.sort(key=lambda o: (o.timestamp.timestamp() if o.timestamp else 0.0, o.receipt_id))
    used = [o for o in osn if o.learning_used is True]
    cohorts = [
        _cohort(COHORT_USED, used),
        _cohort(COHORT_NOT_USED, [o for o in osn if o.learning_used is False]),
        _cohort(COHORT_UNRECORDED, [o for o in osn if o.learning_used is None]),
    ]
    by_signal: dict[str, list[Observation]] = {}
    for o in used:
        for sid in o.learning_signal_ids:
            by_signal.setdefault(sid, []).append(o)
    followups = [
        SignalFollowUp(
            signal_id=sid,
            later_runs=len(runs),
            observed=sum(1 for o in runs if o.observed),
            verified_successes=sum(1 for o in runs if o.verified_success),
            first_attempt_passed=sum(1 for o in runs if o.observed and o.attempts and o.attempts[0].state == "passed"),
        )
        for sid, runs in by_signal.items()
    ]
    followups.sort(key=lambda f: (-f.later_runs, f.signal_id))
    return ImpactReport(index.repo, task_category, cohorts, followups[:MAX_SIGNAL_FOLLOWUPS])


__all__ = [
    "COHORT_NOT_USED",
    "COHORT_UNRECORDED",
    "COHORT_USED",
    "DISCLAIMER",
    "MIN_COHORT_FOR_RATE",
    "Cohort",
    "ImpactReport",
    "SignalFollowUp",
    "measure",
]
