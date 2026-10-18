"""Gaming checks v1: test edits, grader reads, masked exits and trace tampering on Receipts.

Driven through the real hook entry point (``handle_claude_hook``) in a real
git repository, so each signal reaches the stored record, ``openshard
last``'s receipt and the history JSON as a user sees it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import handle_claude_hook
from openshard.adapters.claude_session_fidelity import rebuilt_counters
from openshard.history.shard_contract import build_shard_receipt, render_compact_shard_receipt
from openshard.sync.envelope import receipt_payload
from openshard.verification import check_command as cc
from openshard.verification import gaming_signals as gs

SID = "6a5b4c3d-2e1f-4a0b-9c8d-7e6f5a4b3c2d"

STORE_TEST = """\
from store import Store


def test_get():
    s = Store()
    assert s.get("k") == 42
    assert s.get("missing") is None
    assert len(s) == 1
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.delenv("OPENSHARD_CONFIG", raising=False)
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "evals").mkdir()
    (root / "tests" / "test_store.py").write_text(STORE_TEST, encoding="utf-8")
    (root / "tests" / "test_old.py").write_text("def test_old():\n    assert True\n", encoding="utf-8")
    (root / "conftest.py").write_text("import pytest\n", encoding="utf-8")
    (root / "evals" / "grader.py").write_text("EXPECTED = 42\n", encoding="utf-8")
    (root / "store.py").write_text("class Store: ...\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _run(repo: Path, event: str, **fields):
    payload = {"session_id": SID, "cwd": str(repo), "permission_mode": "default", "hook_event_name": event, **fields}
    return handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo)})


def _start(repo: Path) -> None:
    _run(repo, "SessionStart", source="startup")
    _run(repo, "UserPromptSubmit", prompt="Make the store tests pass")


def _bash(repo: Path, command: str) -> None:
    _run(repo, "PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_response={"stdout": ""})


def _edit(repo: Path, rel: str, text: str | None = None, tool: str = "Edit") -> None:
    path = repo / rel
    if text is not None:
        path.write_text(text, encoding="utf-8")
    _run(repo, "PostToolUse", tool_name=tool, tool_input={"file_path": str(path)}, tool_response={})


def _end(repo: Path) -> dict:
    _run(repo, "Stop", stop_hook_active=False)
    _run(repo, "SessionEnd", reason="prompt_input_exit")
    lines = [json.loads(ln) for ln in (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    assert len(lines) == 1
    return lines[0]


def _kinds(entry: dict) -> dict[str, dict]:
    return {s["kind"]: s for s in entry["gaming_signals"]}


def _receipt_text(entry: dict) -> str:
    return render_compact_shard_receipt(build_shard_receipt(entry, index=0))


# -- test_files_weakened --------------------------------------------------------


def test_removed_assertions_and_skip_marker_are_flagged_with_paths_and_counts_only(repo):
    _start(repo)
    weakened = STORE_TEST.replace('    assert s.get("k") == 42\n    assert s.get("missing") is None\n'
                                  '    assert len(s) == 1\n', "    pass\n")
    weakened = weakened.replace("def test_get", "@pytest.mark.skip(reason='flaky')\ndef test_get")
    _edit(repo, "tests/test_store.py", "import pytest\n" + weakened)
    entry = _end(repo)

    signal = _kinds(entry)["test_files_weakened"]
    assert signal["message"] == (
        "1 test file edited; 3 assertions removed in tests/test_store.py; "
        "1 skip marker added in tests/test_store.py"
    )
    assert signal["paths"] == ["tests/test_store.py"]
    assert signal["counts"] == {"test_files_edited": 1, "test_files_deleted": 0,
                                "assertions_removed": 3, "skip_markers_added": 1}
    # Privacy: the diff is read in memory only; no line of test code is stored anywhere.
    stored = (repo / ".openshard").rglob("*")
    for path in stored:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert 'get("k") == 42' not in text and "flaky" not in text, path


def test_deleted_test_file_is_flagged(repo):
    _start(repo)
    (repo / "tests" / "test_old.py").unlink()
    _bash(repo, "rm tests/test_old.py")
    signal = _kinds(_end(repo))["test_files_weakened"]
    assert signal["message"] == "1 test file deleted: tests/test_old.py"
    assert signal["counts"]["test_files_deleted"] == 1


def test_adding_tests_and_reading_tests_is_not_flagged(repo):
    _start(repo)
    _bash(repo, "cat tests/test_store.py")
    _edit(repo, "tests/test_store.py", STORE_TEST + "\n\ndef test_put():\n    s = Store()\n    s.put('a', 1)\n"
          "    assert s.get('a') == 1\n")
    _edit(repo, "store.py", "class Store:\n    pass\n")
    _bash(repo, "pytest -q")
    entry = _end(repo)
    assert entry["gaming_signals"] == []
    assert "Watch" not in _receipt_text(entry)


def test_rewriting_an_assertion_is_not_weakening(repo):
    _start(repo)
    _edit(repo, "tests/test_store.py", STORE_TEST.replace("== 42", "== 43"))
    assert _end(repo)["gaming_signals"] == []


# -- grader_or_answer_read -------------------------------------------------------


def test_reading_a_grading_file_is_flagged(repo):
    _start(repo)
    _bash(repo, "cat evals/grader.py")
    entry = _end(repo)
    signal = _kinds(entry)["grader_or_answer_read"]
    assert signal["message"] == "Agent read a grading or answer file: evals/grader.py"
    assert signal["paths"] == ["evals/grader.py"]


def test_running_the_grader_is_not_a_read(repo):
    _start(repo)
    _bash(repo, "python evals/grader.py")
    assert _end(repo)["gaming_signals"] == []


def test_grader_patterns_are_repo_configurable(repo):
    (repo / ".openshard").mkdir()
    (repo / ".openshard" / "config.yml").write_text(
        "gaming_checks:\n  grader_patterns: ['**/rubric*']\n", encoding="utf-8")
    _start(repo)
    _bash(repo, "cat evals/grader.py")
    _bash(repo, "head docs/rubric.md")
    signal = _kinds(_end(repo))["grader_or_answer_read"]
    assert signal["paths"] == ["docs/rubric.md"]


def test_read_tool_events_from_other_agents_match_patterns():
    events = [
        {"event_type": "tool.invoked", "action": "tool view_file", "target": "evals/solution.py",
         "metadata": {"access": "read"}},
        {"event_type": "tool.invoked", "action": "tool view_file", "target": "src/app.py",
         "metadata": {"access": "read"}},
        {"event_type": "tool.invoked", "action": "tool view_file", "target": "x/.hidden_tests/t.py",
         "metadata": {"access": "read"}},
    ]
    found = gs.scan_events(events, gs.DEFAULT_GRADER_PATTERNS)
    assert found["reads"] == ["evals/solution.py", "x/.hidden_tests/t.py"]
    assert found["writes"] == []


def _read(repo: Path, rel: str, *, failed: bool = False, content: str = "SECRET-FILE-CONTENT") -> None:
    path = rel if Path(rel).is_absolute() else str(repo / rel)
    if failed:
        _run(repo, "PostToolUseFailure", tool_name="Read", tool_input={"file_path": path}, error="File does not exist.")
    else:
        _run(repo, "PostToolUse", tool_name="Read", tool_input={"file_path": path},
             tool_response={"type": "text", "file": {"filePath": path, "content": content}})


def test_claude_read_tool_of_answer_files_is_flagged_by_path_only(repo):
    _start(repo)
    for rel in ("src/app.py", "evals/grader.py", "tests/__snapshots__/app.test.ts.snap",
                "tests/fixtures/input.json", "tests/golden/out.txt", "web/a.snap"):
        _read(repo, rel)
    entry = _end(repo)
    signal = _kinds(entry)["grader_or_answer_read"]
    assert signal["paths"] == ["evals/grader.py", "tests/__snapshots__/app.test.ts.snap",
                               "tests/golden/out.txt", "web/a.snap"]  # test inputs (fixtures/) are not answers
    assert signal["message"].startswith("Agent read a grading or answer file: evals/grader.py")
    text = (repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8")
    assert "SECRET-FILE-CONTENT" not in text
    # A read is a counted tool call but not an Event; hook coverage leaves it out
    # of the count it compares with the transcript's edit and shell calls.
    assert not [e for e in entry.get("events") or [] if (e.get("metadata") or {}).get("tool") == "Read"]
    assert entry["capture"]["tool_call_count"] == 6


def test_read_hooks_never_hide_a_missing_edit_hook():
    from openshard.adapters import claude_hook_coverage as cov

    buf = {"last_stop_at": "2026-10-18T10:00:00Z", "last_activity_at": "2026-10-18T10:00:00Z",
           "tool_call_count": 3, "read_call_count": 3}
    state = {"ok": [], "ok_settled": 1, "last_at": None}  # one Edit/Bash result in the transcript
    reasons = cov.evaluate(buf, state)
    assert [r["kind"] for r in reasons] == [cov.GAP_TOOL_CALLS]


def test_failed_and_outside_repository_reads_are_not_flagged(repo, tmp_path):
    _start(repo)
    _read(repo, "evals/grader.py", failed=True)  # never shown to the agent
    outside = tmp_path / "elsewhere" / "golden" / "out.txt"
    _read(repo, str(outside))
    assert _end(repo)["gaming_signals"] == []


def test_read_tool_follows_configured_patterns(repo):
    (repo / ".openshard").mkdir()
    (repo / ".openshard" / "config.yml").write_text("gaming_checks:\n  grader_patterns: []\n", encoding="utf-8")
    _start(repo)
    _read(repo, "evals/grader.py")
    assert _end(repo)["gaming_signals"] == []


def test_a_repository_can_add_fixtures_to_its_patterns(repo):
    (repo / ".openshard").mkdir()
    (repo / ".openshard" / "config.yml").write_text(
        "gaming_checks:\n  grader_patterns: ['**/fixtures/**']\n", encoding="utf-8")
    _start(repo)
    _read(repo, "tests/fixtures/input.json")
    assert _kinds(_end(repo))["grader_or_answer_read"]["paths"] == ["tests/fixtures/input.json"]


def test_read_path_never_routes_or_attributes_work():
    from openshard.adapters.claude_hooks import extract_hook_payload

    payload = extract_hook_payload({"hook_event_name": "PostToolUse", "session_id": SID, "tool_name": "Read",
                                    "tool_input": {"file_path": "/r/tests/fixtures/a.json"},
                                    "tool_response": {"file": {"content": "SECRET"}}})
    assert payload is not None and payload.file_path is None  # routing / baselines read file_path only
    assert payload.read_path == "/r/tests/fixtures/a.json"


@pytest.mark.parametrize(("path", "matched"), [
    ("tests/__snapshots__/a.test.ts.snap", True), ("pkg/testdata/golden/out.txt", True),
    ("x/out.golden", True), ("tests/fixtures/input.json", False), ("tests/expected/a.txt", True),
    ("ui/snapshots/home.png", True), ("src/fixture_loader.py", False), ("src/snapshot.py", False),
    ("docs/expected-behaviour.md", False),
])
def test_default_patterns_cover_expected_test_outputs(path, matched):
    assert gs.matches_any(path, gs.DEFAULT_GRADER_PATTERNS) is matched


# -- masked_exit -----------------------------------------------------------------


def test_test_command_that_cannot_fail_is_flagged(repo):
    _start(repo)
    _bash(repo, "pytest -q || true")
    entry = _end(repo)
    signal = _kinds(entry)["masked_exit"]
    assert signal["message"] == "Test command can't fail: `pytest -q || true`"
    assert signal["counts"] == {"commands": 1, "exit_zero_added": 0}


@pytest.mark.parametrize("command", ["pytest -q", "pytest -q 2>&1 | tail -20", "pytest -q && exit 0",
                                     "ruff check . || true"])
def test_ordinary_check_commands_are_not_masked(repo, command):
    _start(repo)
    _bash(repo, command)
    assert _end(repo)["gaming_signals"] == []


def test_sys_exit_zero_added_to_conftest_is_flagged(repo):
    _start(repo)
    _edit(repo, "conftest.py", "import sys\nimport pytest\n\nsys.exit(0)\n")
    signal = _kinds(_end(repo))["masked_exit"]
    assert signal["message"] == "sys.exit(0) added in conftest.py"
    assert signal["paths"] == ["conftest.py"]


@pytest.mark.parametrize(("command", "mask"), [
    ("pytest -q || true", cc.MASK_OR_TRUE),
    ("pytest || :", cc.MASK_OR_NOOP),
    ("cd pkg && pytest -q || exit 0", cc.MASK_OR_EXIT_ZERO),
    ("npx vitest run; exit 0", cc.MASK_EXIT_ZERO),
    ("set +e; pytest", cc.MASK_SET_PLUS_E),
    ('bash -c "pytest || true"', cc.MASK_OR_TRUE),
    ("pytest || echo failed", None),
    ("pytest | tail", None),
    ("pytest && exit 0", None),
])
def test_exit_masks_from_raw_command(command, mask):
    assert [c.exit_masked for c in cc.classify_check_commands(command)] == [mask]


def test_powershell_lines_are_never_masked():
    assert cc.classify_check_command("pytest; exit 0", shell=cc.SHELL_POWERSHELL).exit_masked is None


def test_queued_check_items_keep_only_known_masks():
    items = cc.queued_check_commands([
        {"kind": "test", "name": "Bash: pytest", "exit_masked": "or_true"},
        {"kind": "test", "name": "Bash: pytest", "exit_masked": "anything else"},
    ])
    assert items is not None
    assert items[0]["exit_masked"] == "or_true" and "exit_masked" not in items[1]


# -- trace_tampering -------------------------------------------------------------


def test_writes_to_openshard_records_are_flagged(repo):
    _start(repo)
    _edit(repo, ".openshard/runs.jsonl", tool="Write")
    _bash(repo, "rm .openshard/verifications.jsonl")
    signal = _kinds(_end(repo))["trace_tampering"]
    assert signal["message"] == "Agent wrote to OpenShard's own records: .openshard/runs.jsonl and 1 more"
    assert signal["paths"] == [".openshard/runs.jsonl", ".openshard/verifications.jsonl"]


def test_reading_openshard_records_or_running_openshard_is_not_tampering(repo):
    _start(repo)
    _bash(repo, "cat .openshard/runs.jsonl")
    _bash(repo, "openshard last")
    assert _end(repo)["gaming_signals"] == []


def test_command_record_writes():
    assert gs.command_record_writes("Bash: echo x > ~/.openshard/runs.jsonl") == ["~/.openshard/runs.jsonl"]
    assert gs.command_record_writes("Bash: sed -i s/a/b/ .openshard/receipt-signatures.jsonl") == [
        ".openshard/receipt-signatures.jsonl"]
    assert gs.command_record_writes("Bash: grep x .openshard/runs.jsonl") == []
    assert gs.command_record_writes("Bash: rm tests/fixtures/runs.jsonl") == []  # someone else's file


# -- receipt, history JSON, sync, verdict ----------------------------------------


def test_watch_rows_on_the_receipt_and_history_json(repo, monkeypatch):
    _start(repo)
    _bash(repo, "cat evals/grader.py")
    _bash(repo, "pytest -q || true")
    entry = _end(repo)

    text = _receipt_text(entry)
    watch = [ln for ln in text.splitlines() if "Agent read a grading" in ln or "can't fail" in ln]
    assert watch[0].strip().startswith("Watch") and "evals/grader.py" in watch[0]
    assert "Test command can't fail: `pytest -q || true`" in watch[1]
    assert "cheat" not in text.lower()

    from openshard.cli.main import cli

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(cli, ["history", "--json"])
    assert result.exit_code == 0, result.output
    shard = json.loads(result.output)["shards"][0]
    assert [s["kind"] for s in shard["gaming_signals"]] == ["grader_or_answer_read", "masked_exit"]
    assert set(shard["gaming_signals"][0]) == {"kind", "message", "paths", "counts"}

    last = CliRunner().invoke(cli, ["last"])
    assert "Watch" in last.output and "evals/grader.py" in last.output


def test_signals_never_change_the_verdict_and_never_sync(repo):
    _start(repo)
    _bash(repo, "pytest -q || true")
    entry = _end(repo)
    assert entry["gaming_signals"]
    with_signals = build_shard_receipt(entry, index=0)
    without = build_shard_receipt({k: v for k, v in entry.items() if k != "gaming_signals"}, index=0)
    assert with_signals.status == without.status
    assert with_signals.verification == without.verification
    assert with_signals.verification_status == without.verification_status
    assert "gaming" not in json.dumps(receipt_payload(entry, 0))


def test_unchecked_records_stay_unknown():
    receipt = build_shard_receipt({"task": "old", "executor": "claude_code_hook", "timestamp": "2026-01-01T00:00:00Z"},
                                  index=0)
    assert (receipt.recorded_evidence or {}).get("gaming_signals") is None
    assert gs.gaming_signals_block({"gaming_signals": [{"kind": "nope", "message": "x"},
                                                      {"kind": "masked_exit", "message": "/etc/passwd"}]}) == []


def test_observations_survive_a_rebuilt_buffer():
    seen = {"masked": [{"name": "Bash: pytest", "mask": "or_true"}], "reads": ["evals/grader.py"]}
    assert rebuilt_counters({"gaming_seen": seen})["gaming_seen"] == seen
    assert rebuilt_counters({})["gaming_seen"] == {}


@pytest.mark.parametrize(("path", "is_test"), [
    ("tests/test_store.py", True), ("pkg/foo_test.go", True), ("web/a.spec.ts", True),
    ("web/a.test.tsx", True), ("web/__tests__/a.js", True), ("conftest.py", True),
    ("src/testing_utils.py", False), ("src/app.py", False),
])
def test_test_path_heuristics(path, is_test):
    assert gs.is_test_path(path) is is_test
