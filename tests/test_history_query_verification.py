"""History readers must expose the latest OpenShard re-run for each receipt."""
from pathlib import Path

import pytest

from openshard.history.jsonl_store import write_jsonl
from openshard.history.query import get_receipt, list_receipts_by_task, recent_shards
from openshard.history.task_identity import new_task_id
from openshard.history.verification_truth import interpret_receipt
from openshard.verification import post_session as ps


@pytest.mark.parametrize('status,exit_code,state', [
    ('passed', 0, 'verified_passed'),
    ('failed', 1, 'verified_failed'),
])
def test_query_receipts_join_latest_attestation_without_rewriting_history(
    tmp_path: Path, status: str, exit_code: int, state: str,
):
    entry = {
        'schema_version': '1.2', 'timestamp': '2026-09-30T10:00:00Z',
        'run_id': 'run-query', 'shard_id': 'shard-query', 'receipt_id': 'rcpt_' + 'a' * 32,
        'task_id': new_task_id(), 'task': 'Verify graph integrations', 'executor': 'codex_hooks',
        'verification_attempted': False, 'verification_passed': None,
    }
    path = tmp_path / '.openshard/runs.jsonl'
    write_jsonl(path, [entry])
    original = path.read_bytes()
    planned = ps.plan_checks(tmp_path, {'verification_commands': [['python', '-m', 'pytest']]}, entry)
    tree = ps.TreeState(head='a' * 40, dirty=True, tracked_dirty=True)
    # A newer failed re-run must replace an earlier passing result as well.
    for outcome, code in [('passed', 0), (status, exit_code)]:
        attestation = ps.build_attestation(
            entry, [ps.CheckRun(planned[0], outcome, exit_code=code)], before=tree, after=tree,
            started_at='2026-09-30T10:01:00Z', completed_at='2026-09-30T10:02:00Z',
        )
        ps.record_attestation(tmp_path, attestation)
    receipts = [
        get_receipt(entry['receipt_id'], repo_path=tmp_path),
        get_receipt(run_id=entry['run_id'], repo_path=tmp_path),
        recent_shards(repo_path=tmp_path).items[0].receipt,
        list_receipts_by_task(entry['task_id'], repo_path=tmp_path)[0],
    ]
    for receipt in receipts:
        truth = interpret_receipt(receipt)
        assert truth.state == state
        assert truth.effective_status == status
        assert truth.basis == 'post_session'
    assert path.read_bytes() == original


def test_attestation_for_another_receipt_does_not_promote_verification(tmp_path: Path):
    entry = {
        'timestamp': '2026-09-30T10:00:00Z', 'run_id': 'run-target',
        'receipt_id': 'rcpt_' + 'b' * 32, 'task': 'Unverified target',
        'executor': 'codex_hooks', 'verification_attempted': False, 'verification_passed': None,
    }
    write_jsonl(tmp_path / '.openshard/runs.jsonl', [entry])
    other = dict(entry, receipt_id='rcpt_' + 'c' * 32, run_id='run-other')
    planned = ps.plan_checks(tmp_path, {'verification_commands': [['python', '-m', 'pytest']]}, other)
    tree = ps.TreeState(head='a' * 40, dirty=False, tracked_dirty=False)
    ps.record_attestation(tmp_path, ps.build_attestation(
        other, [ps.CheckRun(planned[0], 'passed', exit_code=0)], before=tree, after=tree,
        started_at='2026-09-30T10:01:00Z', completed_at='2026-09-30T10:02:00Z',
    ))
    receipt = get_receipt(entry['receipt_id'], repo_path=tmp_path)
    assert receipt.post_session_verification is None
    assert interpret_receipt(receipt).effective_status != 'passed'
