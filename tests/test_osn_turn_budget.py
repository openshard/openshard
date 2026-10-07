"""The executor is told when its turn budget is running out on inspection alone.

Running OSN on OpenShard's own repository, the executor spent all twelve
turns reading and never wrote: the harness owns the turn limit and the
model could not see it running out. Once half the turns are spent with
nothing written, every later turn says so; the last turn says only a
change or a finish can still count. The attempt still ends at max_turns
exactly as before.
"""
from __future__ import annotations

from pathlib import Path

from openshard.osn.agent_loop import TurnState
from openshard.osn.model_provider import build_turn_prompt, turn_budget_nudge


def _state(turn: int, max_turns: int = 12, writes: int = 0) -> TurnState:
    return TurnState(task="t", attempt=1, turn=turn, max_turns=max_turns, repo_files=["a.py"], observations=[],
                     changed_files=["a.py"] if writes else [], blocked_paths=[], previous_failure=None,
                     verifications_left=2, writes_applied=writes)


def test_no_nudge_while_half_the_turns_remain_or_once_something_was_written():
    assert turn_budget_nudge(_state(1)) is None
    assert turn_budget_nudge(_state(6)) is None  # exactly half: still the model's call
    assert turn_budget_nudge(_state(7, writes=1)) is None
    assert turn_budget_nudge(_state(10, writes=2)) is None


def test_after_half_the_turns_with_nothing_written_every_turn_says_so():
    seven = turn_budget_nudge(_state(7))
    assert seven is not None and seven.startswith("Turn budget: 7 of 12 turns are spent and you have written nothing")
    assert "5 turn(s) remain" in seven and "make the change now" in seven and "edit_file" in seven
    ten = turn_budget_nudge(_state(10))
    assert ten is not None and "2 turn(s) remain" in ten


def test_the_last_turn_says_only_a_change_or_a_finish_counts():
    last = turn_budget_nudge(_state(12))
    assert last is not None and last.startswith("Turn budget: this is your last turn (turn 12 of 12)")
    assert "written nothing" in last and "or finish and say what blocked you" in last
    written = turn_budget_nudge(_state(12, writes=1))
    assert written is not None and "Request verification if you have not, or finish" in written
    assert turn_budget_nudge(_state(3, max_turns=3)) is not None  # tiny budgets: the last turn is still named


def test_the_nudge_is_in_the_turn_prompt_right_after_the_turn_line(tmp_path: Path):
    prompt = build_turn_prompt(_state(9), tmp_path, [])
    assert "Turn 9 of 12." in prompt
    assert prompt.index("Turn 9 of 12.") < prompt.index("Turn budget: 9 of 12 turns are spent") < prompt.index("Repository files:")
    assert "Turn budget" not in build_turn_prompt(_state(2), tmp_path, [])
