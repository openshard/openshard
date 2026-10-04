# Cloud Receipt delivery retries

A merge push may contain commits that already have Receipts from their branch.
Re-running CI changes verification timestamps and duration; immutable ingestion
correctly rejects that changed payload with HTTP 409. Previously this stopped
the workflow before the new merge commit could receive its own Receipt.

Delivery now distinguishes three cases:

- HTTP 200/201 with the exact expected repository-and-commit Receipt ID: captured.
- HTTP 409 with `receipt_conflict` naming that exact Receipt: already recorded,
  retain the original evidence and continue to the next commit. The new check
  result is explicitly not appended to the immutable Receipt.
- Authentication, transport, server, other conflicts, malformed success bodies
  or a different ID: delivery failure. Required commits still fail the job.

The expected ID follows the hosted GitHub contract's SHA-256 mapping from
repository ID and commit SHA. Check failures retain the workflow's nonzero exit;
existing Receipt delivery never changes a failed result to passed. Temporary
response bodies are removed, and identity tokens are minted immediately before
delivery rather than before potentially long verification runs.

## Windows validation

The shell regression test now selects Git Bash explicitly from the Git
installation on Windows. PATH's WSL `bash.exe` launcher is not a substitute for
the shell that can run this GitHub Actions script. The test is added to the
curated Windows smoke set so the fast PR gate exercises it before merge.

This is a delivery and validation repair. It does not publish 0.4.10, recover a
lost cloud environment, or infer model identity, tokens or cost.
