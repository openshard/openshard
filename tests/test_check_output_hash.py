"""A check Openshard runs records an exit code and a hash of its whole output, never the output itself."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from openshard.history.verification import VerificationCheck, parse_verification_block
from openshard.verification import post_session as ps

CLEAN = ps.TreeState(head="2650366" + "a" * 33, dirty=False, tracked_dirty=False)
SECRET_LINE = "this output line must never be stored"


def _run(tmp_path: Path, body: str, *, stream: bool = False) -> ps.CheckRun:
    script = tmp_path / "check.py"
    script.write_text(
        "import sys\nsys.stdout.reconfigure(newline='\\n')\nsys.stderr.reconfigure(newline='\\n')\n" + body,
        encoding="utf-8",
    )
    check = ps.PlannedCheck(name="t", argv=[sys.executable, str(script)], kind="test", origin="change",
                            safety="safe", reason="")
    (run,) = ps.run_checks([check], tmp_path, stream=stream)
    return run


def _attest(results: list[ps.CheckRun]) -> dict:
    return ps.build_attestation({"receipt_id": "rcp_1"}, results, before=CLEAN, after=CLEAN,
                                started_at="2026-10-22T10:00:00Z", completed_at="2026-10-22T10:00:09Z",
                                commit=CLEAN.head)


def test_a_real_check_records_the_sha256_and_size_of_everything_it_printed(tmp_path):
    out = SECRET_LINE + "\n" + "x" * 200_000 + "\n"  # longer than the head and tail kept in memory
    run = _run(tmp_path, f"import sys\nsys.stdout.write({out!r})\nsys.stderr.write('err\\n')\n")
    assert (run.status, run.exit_code) == ("passed", 0)
    # stdout and stderr share one pipe; both streams count, whatever order they arrived in.
    assert run.output_bytes == len(out) + len("err\n")
    assert run.output_sha256 is not None and len(run.output_sha256) == 64
    assert run.output_sha256 in {hashlib.sha256((out + "err\n").encode()).hexdigest(),
                                 hashlib.sha256(("err\n" + out).encode()).hexdigest()}


def test_a_failing_check_is_hashed_too_and_the_hash_follows_its_output(tmp_path):
    a = _run(tmp_path, "print('1 failed')\nraise SystemExit(1)\n")
    b = _run(tmp_path, "print('2 failed')\nraise SystemExit(1)\n")
    assert (a.status, a.exit_code) == ("failed", 1)
    assert a.output_sha256 == hashlib.sha256(b"1 failed\n").hexdigest()
    assert a.output_sha256 != b.output_sha256


def test_the_hash_keeps_raw_windows_line_endings(tmp_path):
    run = _run(tmp_path, "sys.stdout.buffer.write(b'check passed\\r\\n')\n")
    raw = b"check passed\r\n"
    assert run.output_sha256 == hashlib.sha256(raw).hexdigest()
    assert run.output_bytes == len(raw)


def test_the_receipt_block_carries_the_hash_beside_the_exit_code_and_never_the_text(tmp_path):
    run = _run(tmp_path, f"print({SECRET_LINE!r})\n")
    attestation = _attest([run])
    (row,) = attestation["verification"]["checks"]
    assert row["exit_code"] == 0
    assert row["output_sha256"] == hashlib.sha256((SECRET_LINE + "\n").encode()).hexdigest()
    assert row["output_bytes"] == len(SECRET_LINE) + 1
    assert SECRET_LINE not in json.dumps(attestation)
    assert attestation["raw_output_stored"] is False


def test_a_timed_out_check_has_no_hash(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    check = ps.PlannedCheck(name="t", argv=[sys.executable, str(script)], kind="test", origin="change",
                            safety="safe", reason="")
    (run,) = ps.run_checks([check], tmp_path, stream=False, timeout=1)
    assert run.exit_code is None and run.output_sha256 is None and run.output_bytes is None


def test_a_skipped_check_has_no_hash(tmp_path):
    check = ps.PlannedCheck(name="t", argv=["x"], kind="test", origin="change", safety="needs_approval", reason="")
    (run,) = ps.run_checks([check], tmp_path)
    assert run.status == "skipped" and run.output_sha256 is None


def test_the_hash_round_trips_and_a_malformed_one_is_dropped():
    digest = hashlib.sha256(b"ok").hexdigest()
    block = {"status": "passed", "source": "directly_observed", "observation_mode": "openshard_executed",
             "checks": [{"name": "pytest", "kind": "test", "status": "passed", "exit_code": 0,
                         "output_sha256": digest, "output_bytes": 2},
                        {"name": "ruff", "kind": "lint", "status": "passed", "exit_code": 0,
                         "output_sha256": "not-a-hash", "output_bytes": 2}]}
    ev = parse_verification_block(block)
    good, bad = (c.to_dict() for c in ev.checks)
    assert (good["output_sha256"], good["output_bytes"]) == (digest, 2)
    assert "output_sha256" not in bad and "output_bytes" not in bad


def test_a_hash_without_an_exit_code_is_not_written():
    check = VerificationCheck(name="t", status="unknown", output_sha256="a" * 64, output_bytes=1)
    assert "output_sha256" not in check.to_dict()


def test_an_older_check_without_a_hash_keeps_its_exact_shape():
    check = VerificationCheck(name="t", status="passed", kind="test", exit_code=0)
    assert check.to_dict() == {"name": "t", "kind": "test", "status": "passed", "exit_code": 0}
