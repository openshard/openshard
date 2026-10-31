"""rules-v1 steering intent classifier (adapters/steering_intent.py)."""

from __future__ import annotations

import pytest

from openshard.adapters.steering_intent import CONFIDENCES, INTENTS, METHOD, classify_intent

LABELLED: list[tuple[str, str]] = [
    # The founder's examples.
    ("I don't like the preview, make it exactly like the real component", "correction"),
    ("No, that's not what I asked for", "correction"),
    ("looks good", "approval"),
    ("go ahead", "continue"),
    # Corrections.
    ("no", "correction"),
    ("nope, still broken", "correction"),
    ("don't touch the migrations", "correction"),
    ("Do not add a new dependency", "correction"),
    ("that's wrong, the total should be 42", "correction"),
    ("That is wrong.", "correction"),
    ("not what I asked", "correction"),
    ("redo it with the old layout", "correction"),
    ("revert the last change", "correction"),
    ("try again", "correction"),
    ("the tests still fail", "correction"),
    ("it doesn't work on mobile", "correction"),
    ("you forgot the dark mode styles", "correction"),
    ("the button is broken now", "correction"),
    ("make it match the design exactly", "correction"),
    ("looks good but the spacing is off", "correction"),
    ("It shouldn't log the token", "correction"),
    # Redirects.
    ("actually, use postgres", "redirect"),
    ("Instead, put it in a separate module", "redirect"),
    ("no, use the existing helper instead", "redirect"),
    ("wait, let's keep the API as is", "redirect"),
    ("scratch that, we'll do it server side", "redirect"),
    ("never mind the cache", "redirect"),
    ("let's go with the simpler approach", "redirect"),
    ("put the retry in the client rather than the server", "redirect"),
    # Clarifications.
    ("why did you change the config?", "clarification"),
    ("what does this function return", "clarification"),
    ("how does the retry work?", "clarification"),
    ("is it thread safe?", "clarification"),
    ("which file has the router", "clarification"),
    ("the second option?", "clarification"),
    # Approvals.
    ("yes", "approval"),
    ("lgtm", "approval"),
    ("LGTM!", "approval"),
    ("perfect, thanks", "approval"),
    ("great", "approval"),
    ("ship it", "approval"),
    ("Looks good to me 👍", "approval"),
    # Continue.
    ("continue", "continue"),
    ("keep going", "continue"),
    ("yes, continue", "continue"),
    ("proceed", "continue"),
    ("ok go ahead", "continue"),
    # New task.
    ("also add tests for the parser", "new_task"),
    ("now do the same for the settings page", "new_task"),
    ("looks good, now add the docs", "new_task"),
    ("Can you also update the changelog?", "new_task"),
    ("one more thing: bump the version", "new_task"),
    ("Build a CLI command that exports every receipt in the repository to CSV with columns for the "
     "timestamp, agent, model, cost and files changed, with a --since flag accepting ISO dates and a "
     "--output flag defaulting to stdout.", "new_task"),
    # Unknown.
    ("", "unknown"),
    ("   ", "unknown"),
    ("hmm", "unknown"),
    ("the header", "unknown"),
    ("Can you make the header sticky?", "unknown"),
]


def test_has_forty_or_more_labelled_examples():
    assert len(LABELLED) >= 40


@pytest.mark.parametrize(("prompt", "expected"), LABELLED)
def test_labelled_examples(prompt: str, expected: str):
    result = classify_intent(prompt)
    assert result.intent == expected, (prompt, result)
    assert result.intent in INTENTS
    assert result.confidence in CONFIDENCES


def test_results_are_explainable_and_hold_no_prompt_text():
    prompt = "I don't like the preview, make it exactly like the real component"
    result = classify_intent(prompt)
    assert (result.intent, result.confidence) == ("correction", "high")
    assert result.rule.replace(".", "").replace("_", "").isalpha()
    assert "preview" not in result.rule and "component" not in repr(result).split("rule=")[0]


def test_deterministic_and_never_raises():
    for value in (None, 42, b"no", ["no"], "x" * 50_000, "<pasted_content id=1>no</pasted_content>"):
        assert classify_intent(value) == classify_intent(value)
    assert METHOD == "rules-v1"


def test_pasted_and_code_content_is_ignored():
    assert classify_intent("<pasted_content id=1>this is wrong, revert it</pasted_content>").intent == "unknown"
    assert classify_intent("```\nno\n```").intent == "unknown"


def test_long_message_referring_back_is_not_a_new_task():
    text = "Thanks for that. " + "Please adjust the spacing so that it lines up with the grid in the mock. " * 4
    assert classify_intent(text).intent != "new_task"
