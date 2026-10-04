import copy
import json

import pytest
from click.testing import CliRunner

from openshard.history.correlation import (
    CONTEXT_ENV,
    correlation_block,
    stamp_launch_correlation,
    workflow_timeline,
)
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_hash import verify_shard_hash
from openshard.history.views import receipt_to_dict

TASK = "task_018f4d2a-1c3e-7000-8b1a-0242ac120002"
TRACE = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
CONTEXT = {
    "parent_run_id": "parent-1", "source": "github", "trigger": "issue",
    "traceparent": TRACE,
    "external_ids": [{"namespace": "github.workflow", "id": "openshard/openshard/123"}],
}


@pytest.mark.parametrize("trace", [
    TRACE.upper(), "00-" + "0" * 32 + "-b7ad6b7169203331-01",
    "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
    TRACE + "-extra", "ff" + TRACE[2:], "01" + TRACE[2:], None, [],
])
def test_bad_trace_does_not_discard_other_links(trace):
    assert correlation_block({"source": "github", "traceparent": trace}) == {
        "evidence": "declared", "source": "github",
    }


def test_boundary_is_bounded_and_does_not_upgrade_evidence():
    raw = {**CONTEXT, "evidence": "independently_verified", "raw_prompt": "private",
           "tracestate": "vendor=private", "baggage": "token=secret"}
    before = copy.deepcopy(raw)
    assert correlation_block(raw) == {"evidence": "declared", **CONTEXT}
    assert raw == before
    links = [{"namespace": "ci", "id": "job-" + str(i)} for i in range(30)]
    assert len(correlation_block({"external_ids": links})["external_ids"]) == 16
    assert correlation_block({"external_ids": links[:1] * 2})["external_ids"] == links[:1]


@pytest.mark.parametrize("bad", ["/home/me/private", "C:/private", "sk-abcdefghijk", "a" * 257, "hello\nworld", {}, 0])
def test_unsafe_ids_are_dropped_not_truncated(bad):
    assert correlation_block({"parent_run_id": bad}) is None


def test_environment_capture_is_explicit_once_and_not_read_time(monkeypatch):
    entry = {}
    stamp_launch_correlation(entry, {CONTEXT_ENV: json.dumps(CONTEXT)})
    first = copy.deepcopy(entry)
    stamp_launch_correlation(entry, {CONTEXT_ENV: '{"source":"other"}'})
    assert entry == first
    monkeypatch.setenv(CONTEXT_ENV, json.dumps(CONTEXT))
    old = {"timestamp": "2026-10-04T01:00:00Z", "task": "Old run"}
    assert "correlation" not in receipt_to_dict(build_shard_receipt(old), extended=True)
    assert "correlation" not in old


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", " " * 8193])
def test_bad_launch_context_is_noop(raw):
    entry = {}
    stamp_launch_correlation(entry, {CONTEXT_ENV: raw})
    assert entry == {}


def test_real_wrap_producer_seals_and_projects_context(tmp_path, monkeypatch):
    from openshard.adapters.wrap_exec import build_wrap_entry

    monkeypatch.setenv(CONTEXT_ENV, json.dumps(CONTEXT))
    entry = build_wrap_entry("Correlation dogfood test", pre_state={}, exit_code=0,
                             repo_path=tmp_path, task_id=TASK)
    assert verify_shard_hash(entry)["matches"] is True
    projected = receipt_to_dict(build_shard_receipt(entry), extended=True)
    assert projected["correlation"] == {"evidence": "declared", **CONTEXT}
    assert projected["task_id"] == TASK
    assert projected["receipt_id"] == entry["receipt_id"]
    assert "correlation" not in receipt_to_dict(build_shard_receipt(entry))


def test_timeline_joins_only_explicit_task_and_never_mutates_history():
    entries = [
        {"task_id": TASK, "shard_id": "shard-1", "run_id": "child", "timestamp": "2026-10-04T02:00:00Z", "correlation": CONTEXT},
        {"task_id": TASK, "shard_id": "shard-1", "run_id": "parent-1", "timestamp": "2026-10-04T01:00:00Z"},
        {"shard_id": "shard-1", "run_id": "unrelated", "correlation": CONTEXT},
    ]
    before = copy.deepcopy(entries)
    result = workflow_timeline(entries, task_id=TASK)
    assert [row["run_id"] for row in result] == ["parent-1", "child"]
    assert result[0]["correlation"] is None
    assert entries == before
    with pytest.raises(ValueError):
        workflow_timeline(entries, task_id="guess")


def test_timeline_cli_reads_existing_history(tmp_path):
    import subprocess

    from openshard.cli.workflow_cmd import workflow_group

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".openshard").mkdir()
    history = tmp_path / ".openshard" / "runs.jsonl"
    history.write_text(json.dumps({"task_id": TASK, "run_id": "run-1"}) + "\n")
    before = history.read_bytes()
    result = CliRunner().invoke(workflow_group, ["timeline", "--task-id", TASK, "--repo-path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["runs"][0]["run_id"] == "run-1"
    assert history.read_bytes() == before
