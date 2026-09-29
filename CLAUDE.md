\# OpenShard Engineering Instructions



\## Required context



Before non-trivial implementation work:



1\. Read CONTRIBUTING.md.

2\. Read docs/ci.md when changing tested behaviour.

3\. Read docs/release-checklist.md for release-related work.

4\. Inspect the existing implementation before proposing new architecture.

5\. Treat existing tests and contracts as evidence of intended behaviour, but verify them against the current implementation.



\## Engineering workflow



Think -> Plan -> Build -> Review -> Verify -> Deliver.



For substantial work:



1\. Understand the existing architecture.

2\. Identify the smallest correct change.

3\. Implement it.

4\. Run focused tests.

5\. Review the diff.

6\. Run broader verification.

7\. Report what is actually proven.



\## OpenShard truth rules



Evidence before appearance.



Unknown stays unknown.



Missing does not mean zero.



Agent-reported success is weaker than independently verified success.



Observation is not control.



Never claim OpenShard blocked, allowed, approved, verified or controlled something unless the underlying execution/evidence proves that claim.



Historical/reconstructed evidence must not be upgraded into directly observed evidence.



External-agent observation must not be represented as OpenShard-controlled execution.



\## Architecture



Preserve the canonical OpenShard flow:



Task

\-> Control

\-> Execution

\-> Verification

\-> Receipt

\-> Platform

\-> Insights



For OSN-controlled execution this can include:



Task

\-> organisation policy

\-> effective restrictions

\-> permissions

\-> approval where required

\-> routing

\-> budget enforcement

\-> OSN execution

\-> verification

\-> bounded retry/escalation

\-> canonical Receipt

\-> privacy-bounded hosted projection

\-> Platform

\-> Insights



Do not create competing Receipt, policy, routing, evidence, permission or OSN architectures when the existing architecture can be extended.



\## Control-plane rules



A UI control is not a real control unless it has:



1\. persisted state;

2\. a real runtime consumer;

3\. enforceable behaviour;

4\. truthful evidence of the resulting decision where applicable.



Do not create decorative Settings, Policies or Permissions functionality.



Do not broaden OpenShard's claimed authority beyond the execution boundaries it actually controls.



\## Change discipline



Prefer the smallest correct change.



Do not perform unrelated refactors.



Do not add speculative features while fixing integration problems.



Keep changes reversible where practical.



Preserve backwards compatibility unless a deliberate breaking change is required.



Do not weaken:



\- policy enforcement

\- permissions

\- approvals

\- sandboxing

\- verification

\- Receipt evidence semantics

\- privacy boundaries



to make tests pass.



\## Verification



Never declare work complete because the code looks correct.



Run the applicable:



\- focused pytest tests

\- broader pytest suite

\- Ruff

\- mypy

\- integration tests

\- end-to-end tests

\- build/package checks



Use pyproject.toml and repository CI configuration as the source of truth for exact commands.



If something cannot be verified, state that explicitly.



\## Current priority



The current priority is v0.5.0 integration and release readiness.



The major systems substantially exist.



Do not expand scope simply because additional features are possible.



Priority:



1\. Prove the complete product loop end to end.

2\. Find real integration defects.

3\. Fix correctness/evidence/security problems.

4\. Strengthen missing integration/E2E tests.

5\. Freeze the v0.5.0 release bar.

6\. Ship a coherent v0.5.0.



Post-v0.5.0 work such as deeper OSN capabilities, Receipt -> outcome -> learning/eval work and Managed Compute should not be pulled into v0.5.0 unless required to fix an actual release blocker.

