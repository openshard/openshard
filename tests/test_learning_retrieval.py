"""Learning retrieval: relevant, explained, bounded, deterministic, advisory."""
from __future__ import annotations

from openshard.learning.retrieval import (
    MAX_LIMIT,
    PROMPT_CLOSE,
    PROMPT_OPEN,
    STATUS_NO_HISTORY,
    STATUS_NO_RELEVANT,
    STATUS_USED,
    consult,
    retrieve,
    task_shape_for,
)
from openshard.learning.signals import (
    KIND_CHECK,
    KIND_MODEL_OUTCOMES,
    KIND_RECOVERY,
    derive_signals,
)
from tests.learning_fixtures import MOBILE_CHECK, NOW, REPO, UNIT_CHECK, osn_entry


def _index(entries):
    return derive_signals(entries, repo=REPO, now=NOW)


def _dashboard_history():
    """Model A failed mobile verification twice; model B recovered; B also succeeds alone."""
    t = "Fix responsive dashboard layout"
    return [
        osn_entry(t, attempts=[("model/a", "failed"), ("model/b", "passed")], check=MOBILE_CHECK),
        osn_entry(t, attempts=[("model/a", "failed"), ("model/b", "passed")], check=MOBILE_CHECK),
        osn_entry("Tweak dashboard chart layout", attempts=[("model/a", "passed")], check=MOBILE_CHECK),
        osn_entry("Dashboard layout spacing", attempts=[("model/b", "passed")], check=MOBILE_CHECK),
        osn_entry("Dashboard layout grid", attempts=[("model/b", "passed")], check=MOBILE_CHECK),
        osn_entry("Dashboard layout cards", attempts=[("model/b", "passed")], check=MOBILE_CHECK),
    ]


def test_related_task_retrieves_the_useful_prior_evidence_with_reasons():
    ctx = consult("Update the dashboard analytics layout", _index(_dashboard_history()), repo=REPO,
                  current_check_fingerprint=UNIT_CHECK["fingerprint"])
    assert ctx.status == STATUS_USED and ctx.used
    kinds = [r.signal.kind for r in ctx.retrieved]
    assert kinds[0] == KIND_CHECK  # the check that caught failures ranks first
    assert KIND_RECOVERY in kinds and KIND_MODEL_OUTCOMES in kinds
    check = ctx.retrieved[0]
    assert "same_repo" in check.reasons and "same_task_category" in check.reasons
    assert any(r.startswith("task_terms:") and "dashboard" in r and "layout" in r for r in check.reasons)
    assert "repeated_verified_outcome" in check.reasons
    # The user's check is not the one history points at: recommend it, never run it.
    [rec] = ctx.recommended_checks
    assert rec.label == "pnpm test:e2e -- mobile" and rec.runs_caught == 2
    assert not ctx.current_check_recommended


def test_current_check_matching_history_is_aligned_not_re_recommended():
    ctx = consult("Update the dashboard analytics layout", _index(_dashboard_history()), repo=REPO,
                  current_check_fingerprint=MOBILE_CHECK["fingerprint"])
    assert ctx.recommended_checks == [] and ctx.current_check_recommended


def test_no_history():
    ctx = consult("Update the dashboard analytics layout", _index([]), repo=REPO)
    assert ctx.status == STATUS_NO_HISTORY and not ctx.used and ctx.prompt_text is None
    assert consult("anything", None, repo=REPO).status == STATUS_NO_HISTORY


def test_irrelevant_history_is_not_surfaced():
    backend = [osn_entry("Rotate billing ledger export", category="standard",
                         attempts=[("model/a", "failed"), ("model/b", "passed")], check=MOBILE_CHECK,
                         files=("billing/ledger.py",))
               for _ in range(4)]
    ctx = consult("Update the dashboard analytics layout", _index(backend), repo=REPO)
    assert ctx.status == STATUS_NO_RELEVANT and ctx.retrieved == [] and ctx.prompt_text is None
    assert ctx.signals_considered > 0


def test_category_alone_does_not_pull_in_an_unrelated_check_or_failure():
    other = [osn_entry("Restyle login button colours", attempts=[("model/a", "failed")], check=MOBILE_CHECK,
                       files=("src/auth/button.tsx",)) for _ in range(3)]
    shape = task_shape_for("Update the dashboard analytics layout", REPO)
    assert shape.task_category == "visual"
    kinds = {r.signal.kind for r in retrieve(_index(other), shape)}
    # Model evidence for visual work in this repo is relevant; that check and failure are not.
    assert kinds == {KIND_MODEL_OUTCOMES}


def test_file_area_mentioned_in_the_task_qualifies_a_check():
    other = [osn_entry("Restyle widgets", attempts=[("model/a", "failed")], check=MOBILE_CHECK,
                       files=("src/dashboard/widgets.tsx",)) for _ in range(3)]
    shape = task_shape_for("Adjust src/dashboard/header.tsx for tablets", REPO)
    got = [r for r in retrieve(_index(other), shape) if r.signal.kind == KIND_CHECK]
    assert got and any(x.startswith("file_area:src/dashboard") for x in got[0].reasons)


def test_weak_samples_surface_but_anecdotes_do_not():
    one = [osn_entry(attempts=[("model/a", "failed")], check=MOBILE_CHECK)]
    assert consult("Update the dashboard layout", _index(one), repo=REPO).retrieved == []
    two = one + [osn_entry(attempts=[("model/a", "failed")], check=MOBILE_CHECK)]
    got = consult("Update the dashboard layout", _index(two), repo=REPO).retrieved
    assert got and all(r.signal.samples >= 2 for r in got)
    assert all("weak_sample" in r.reasons for r in got)


def test_stale_history_is_not_surfaced():
    old = [osn_entry(days_ago=150, attempts=[("model/a", "failed")], check=MOBILE_CHECK) for _ in range(5)]
    assert consult("Update the dashboard layout", _index(old), repo=REPO).retrieved == []


def test_results_are_bounded_and_diverse():
    entries = []
    for m in ("model/a", "model/b", "model/c", "model/d"):
        entries += [osn_entry(attempts=[(m, "failed"), ("model/z", "passed")], check=MOBILE_CHECK) for _ in range(3)]
    got = consult("Update the dashboard layout", _index(entries), repo=REPO).retrieved
    assert 0 < len(got) <= MAX_LIMIT
    per_kind: dict[str, int] = {}
    for r in got:
        per_kind[r.signal.kind] = per_kind.get(r.signal.kind, 0) + 1
    assert max(per_kind.values()) <= 2


def test_retrieval_order_is_deterministic():
    idx = _index(_dashboard_history())
    shape = task_shape_for("Update the dashboard analytics layout", REPO)
    first = [(r.signal.signal_id, r.score, r.reasons) for r in retrieve(idx, shape)]
    for _ in range(3):
        assert [(r.signal.signal_id, r.score, r.reasons) for r in retrieve(idx, shape)] == first


def test_prompt_block_is_advisory_bounded_and_subordinate():
    ctx = consult("Update the dashboard analytics layout", _index(_dashboard_history()), repo=REPO)
    text = ctx.prompt_text
    assert text is not None
    assert text.startswith(PROMPT_OPEN) and text.endswith(PROMPT_CLOSE)
    assert "not an instruction" in text and "take precedence" in text and "correlation is not causation" in text
    assert len(text) < 3000
    assert text.count("\n") <= 3 + MAX_LIMIT + 3 + 1


def test_task_shape_holds_no_task_text():
    shape = task_shape_for("Update the dashboard analytics layout in src/ui/panel.tsx", REPO)
    d = shape.to_dict()
    assert d == {"repo": REPO, "task_category": "visual", "category_source": "keyword_classifier",
                 "areas": ["src/ui"]}
