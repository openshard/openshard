# External learning delivery — implementation log

## User experience

After `openshard learn install claude` or `openshard learn install codex`, the
native prompt hook automatically supplies relevant advisory history before each
task. Users do not need to remember an MCP call. Native hooks must be enabled
and reviewed in the runtime. The installer preserves other settings and hooks;
uninstall removes only this hook.

The hook reads a bounded local snapshot within 25 ms, with an 8 KiB context cap.
It never reads a transcript or live history synchronously, calls a model API,
blocks a prompt, executes recommended checks or changes routing/policy.

Receipts record the last prompt-hook response emitted in the current capture
segment, alongside signal IDs and snapshot provenance. Model consumption stays
unknown. The handoff never proves that the agent followed recommendations or
that learning caused improvement. Ended Receipts remain unchanged.

## Validation

- 207 focused tests passed: native protocol, capture folding, historical
  Receipt projection, learning CLI and existing entrypoint behaviour.
- Ruff passed for Core and the new tests.
- Mypy passed for 300 source files using a fresh cache. The existing local mypy
  cache was malformed; no application source was changed to suppress that error.
- Tests use synthetic native payloads, temporary repositories and local
  snapshots. They make no provider/model API call.
- Independent CI and a real Cloud Receipt are recorded on the PR before merge.

## Remaining boundaries

The previous v0.4.11 wheel does not include these new commands. A subsequent
package release is required for installed runtimes.

A fresh cloud checkout without local history/snapshot cannot learn from previous
hosted runs through this change alone. Hosted retrieval and explicit permission
for that history are separate work. Mobile surfaces without native hooks need
their own supported context delivery path. Native runtime acceptance must be
verified before claiming a particular cloud configuration works.

Claude's native session usage receiver is deployed by Platform PR #91, with
verified Receipt `rcpt_92542199a7f82b13043e508f7d6e3fca`. Full cloud export
coverage and task-level billing remain unestablished. Platform PR #92 adds the
bounded hook-emission evidence field and its honest hosted label.

## Traceability

Related issue: openshard/openshard#411. Setup and evidence semantics are in
`docs/learning.md`. This log is not evidence of a real Claude/Codex model run;
the tests establish protocol handoff and local integration only.
