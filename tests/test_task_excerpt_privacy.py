"""A Receipt's task never carries a local path, and ordinary words are not secrets.

Both were seen live on 2026-10-12. A first prompt mentioning
``C:\\Users\\<name>\\...`` kept the user's name and layout in the task. A task
containing ``<task-notification>`` matched the OpenAI key pattern
(``sk-notification``), so the Platform refused the whole Receipt for good.
"""
from __future__ import annotations

import pytest

from openshard.adapters.claude_hooks import sanitize_task_excerpt
from openshard.safety.sanitize import looks_like_secret, sanitize_text


@pytest.mark.parametrize("prompt,expected", [
    (r"Fix the parser in C:\Users\Michael\project\src\parser.py please", "Fix the parser in .../parser.py please"),
    ("Fix /home/michael/project/src/parser.py now", "Fix .../parser.py now"),
    ("Read ~/notes/plan.md first", "Read .../plan.md first"),
    (r"<output-file>C:\Users\Michael\AppData\Local\x.output</output-file>", "<output-file>.../x.output</output-file>"),
    ("Clean C:/tmp/ then rerun", "Clean .../tmp then rerun"),
])
def test_absolute_local_paths_are_shortened_to_their_name(prompt, expected):
    assert sanitize_task_excerpt(prompt) == expected


@pytest.mark.parametrize("prompt", [
    "Review https://github.com/openshard/platform/pull/164",
    "Edit docs/plan.md and apps/web/src/a.tsx",
    "Split 1/2 of the work, 3/4 later",
    "Use the and/or operator",
])
def test_relative_paths_urls_and_ordinary_slashes_are_kept(prompt):
    assert sanitize_task_excerpt(prompt) == prompt


@pytest.mark.parametrize("text", [
    "Do the risk-assessment today",
    "<task-notification> done",
    "desk-research notes",
])
def test_words_ending_in_sk_are_not_keys(text):
    assert not looks_like_secret(text)
    assert sanitize_text(text, 100) == text


@pytest.mark.parametrize("text", [
    "sk-proj-abcdefghijklmnop",
    "key: sk-ant-api03-abcdefgh",
    "(sk-abcdefghij)",
])
def test_real_keys_are_still_secrets(text):
    assert looks_like_secret(text)
