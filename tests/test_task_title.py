"""Concise Receipt task titles (openshard/history/task_title.py)."""

from __future__ import annotations

import threading

import pytest

from openshard.history.shard_contract import build_shard_receipt
from openshard.history.task_title import (
    TITLE_MAX_CHARS,
    TITLE_MAX_WORDS,
    UNTITLED,
    TitleGenerator,
    derive_task_title,
    improve_task_title,
    improve_task_title_async,
    normalize_title_candidate,
    resolve_task_title,
)
from openshard.history.views import receipt_to_dict
from openshard.sync.envelope import receipt_payload

_FRAMING_WORDS = ("i want", "we need", "please", "can you", "could you")


def _assert_clean(title: str) -> None:
    assert title
    assert len(title) <= TITLE_MAX_CHARS
    assert len(title.split()) <= TITLE_MAX_WORDS
    assert "\n" not in title
    assert "http" not in title.lower() and "www." not in title.lower()
    lowered = title.lower()
    assert not any(lowered.startswith(w) for w in _FRAMING_WORDS)
    assert not title.endswith(("…", "...", ".", ","))


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        # conversational
        (
            "We need a very small CORS fix in the private Openshard Platform repo. "
            "The API rejects the dashboard origin.",
            "Fix CORS in the private Openshard Platform repo",
        ),
        (
            "Make one small improvement to the repository empty-state copy. Keep it short.",
            "Improve repository empty-state copy",
        ),
        (
            "We are moving quickly now. I want one focused change to the receipts table.",
            "Change receipts table",
        ),
        ("I want you to write tests for the task title module", "Write tests for the task title module"),
        ("Can you please refactor the provider manager?", "Refactor provider manager"),
        # imperative
        ("Fix the login redirect loop", "Fix login redirect loop"),
        ("Add a --json flag to openshard history", "Add --json flag to openshard history"),
        # URL-heavy
        (
            "see https://github.com/openshard/platform/pull/12 and https://example.com/x?y=1 "
            "update the README links",
            "Update README links",
        ),
        ("Update the README links, see https://example.com/docs", "Update README links"),
        # transport wrappers
        ("<command-message>review</command-message> Review the diff for bugs", "Review diff for bugs"),
        # tiny
        ("typo", "Typo"),
    ],
)
def test_derive_examples(task: str, expected: str) -> None:
    title = derive_task_title(task)
    assert title == expected
    _assert_clean(title)


def test_very_long_task_is_bounded_at_a_word() -> None:
    task = (
        "Please add retry logic to the sync client when the platform returns 503, because right "
        "now receipts get stuck in the outbox forever and users complain about it constantly. " * 20
    )
    title = derive_task_title(task)
    assert title == "Add retry logic to sync client"
    _assert_clean(title)


def test_single_run_on_sentence_is_bounded() -> None:
    task = " ".join(["update"] + [f"module{i}" for i in range(80)])
    _assert_clean(derive_task_title(task))


def test_secret_like_data_never_reaches_the_title() -> None:
    secrets = [
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ]
    task = f"use key {secrets[0]} and {secrets[1]} and token {secrets[2]} to debug auth password=hunter2hunter2"
    title = derive_task_title(task)
    _assert_clean(title)
    for s in secrets + ["hunter2", "sk-a", "AKIA", "ghp_", "..."]:
        assert s not in title


@pytest.mark.parametrize("task", ["", "   ", None, 42, "\n\n", "https://example.com/only-a-url", "hi"])
def test_empty_or_missing_input_is_untitled(task: object) -> None:
    assert derive_task_title(task) == UNTITLED


def test_derive_does_not_mutate_input() -> None:
    task = "We need a very small CORS fix in the private repo."
    before = str(task)
    derive_task_title(task)
    assert task == before


# ---------------------------------------------------------------------------
# Stored titles, old records, receipts
# ---------------------------------------------------------------------------


def test_resolve_prefers_valid_stored_title() -> None:
    entry = {"task": "We need a fix for the header", "task_title": "Fix header spacing on mobile"}
    assert resolve_task_title(entry) == "Fix header spacing on mobile"


@pytest.mark.parametrize(
    "stored",
    [None, "", 7, "line one\nline two", "See https://example.com", "Use sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"],
)
def test_resolve_rejects_unusable_stored_title(stored: object) -> None:
    entry = {"task": "Fix the header spacing", "task_title": stored}
    assert resolve_task_title(entry) == "Fix header spacing"


def test_old_record_without_task_title_derives_at_read_time() -> None:
    entry = {
        "timestamp": "2026-01-02T03:04:05Z",
        "task": "We need a very small CORS fix in the private Openshard Platform repo.",
    }
    receipt = build_shard_receipt(entry, index=0)
    assert receipt.task_title == "Fix CORS in the private Openshard Platform repo"
    # Original task text is untouched.
    assert receipt.task_full == entry["task"]
    assert receipt.task_short == entry["task"]
    assert "task_title" not in entry


def test_receipt_dict_carries_title_and_keeps_raw_task() -> None:
    task = "Make one small improvement to the repository empty-state copy."
    d = receipt_to_dict(build_shard_receipt({"timestamp": "2026-01-02T03:04:05Z", "task": task}, index=0))
    assert d["task_title"] == "Improve repository empty-state copy"
    assert d["task_full"] == task


def test_sync_payload_carries_title_and_the_original_task() -> None:
    entry = {"timestamp": "2026-01-02T03:04:05Z", "task": "Fix the header", "task_title": "Fix header"}
    payload = receipt_payload(entry, 0)
    assert payload["task_title"] == "Fix header"
    assert payload["task_full"] == "Fix the header"


def test_sanitized_capture_task_gets_a_local_title() -> None:
    from openshard.adapters.claude_code_import import _sanitize_task

    safe = _sanitize_task("Please fix the flaky sync test")
    assert derive_task_title(safe) == "Fix flaky sync test"


# ---------------------------------------------------------------------------
# Optional generator (pluggable; none wired by default)
# ---------------------------------------------------------------------------


class _Gen:
    def __init__(self, out: object = None, exc: Exception | None = None) -> None:
        self.out = out
        self.exc = exc
        self.seen: list[str] = []

    def generate_title(self, task: str) -> str | None:
        self.seen.append(task)
        if self.exc:
            raise self.exc
        return self.out  # type: ignore[return-value]


def test_generator_protocol() -> None:
    assert isinstance(_Gen(), TitleGenerator)


def test_no_llm_available_keeps_fallback() -> None:
    assert improve_task_title("Fix it", "Fix it", None) == "Fix it"
    assert improve_task_title_async("Fix it", "Fix it", None, lambda t: None) is None


def test_generator_output_is_validated() -> None:
    fb = "Fix CORS in the private repo"
    assert improve_task_title("t", fb, _Gen("Fix private API CORS configuration")) == "Fix private API CORS configuration"
    assert improve_task_title("t", fb, _Gen("We need to fix private API CORS.")) == "Fix private API CORS"
    assert improve_task_title("t", fb, _Gen("See https://evil.example")) == fb
    assert improve_task_title("t", fb, _Gen("Use sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123")) == fb
    assert improve_task_title("t", fb, _Gen("")) == fb
    assert improve_task_title("t", fb, _Gen(None)) == fb
    assert improve_task_title("t", fb, _Gen(exc=RuntimeError("down"))) == fb
    long = improve_task_title("t", fb, _Gen("Fix " + "very " * 40 + "long title"))
    assert len(long) <= TITLE_MAX_CHARS


def test_generator_receives_scrubbed_task() -> None:
    gen = _Gen("Debug auth")
    improve_task_title("debug auth with sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", "Debug auth", gen)
    assert "abcdefghijklmnop" not in gen.seen[0]


def test_async_improvement_does_not_block_and_persists() -> None:
    release = threading.Event()
    persisted: list[str] = []

    class _Slow:
        def generate_title(self, task: str) -> str | None:
            release.wait(5)
            return "Fix private API CORS configuration"

    thread = improve_task_title_async("task", "Fix CORS", _Slow(), persisted.append)
    assert thread is not None
    assert persisted == []  # returned before the generator finished
    release.set()
    thread.join(5)
    assert persisted == ["Fix private API CORS configuration"]


def test_async_skips_persist_when_unchanged_and_swallows_persist_errors() -> None:
    def boom(_: str) -> None:
        raise RuntimeError("disk full")

    t = improve_task_title_async("task", "Fix CORS", _Gen("Fix CORS"), boom)
    assert t is not None
    t.join(5)
    t = improve_task_title_async("task", "Fix CORS", _Gen("Fix private API CORS"), boom)
    assert t is not None
    t.join(5)


def test_normalize_candidate_rejects_non_strings() -> None:
    assert normalize_title_candidate(None) is None
    assert normalize_title_candidate(["Fix"]) is None
