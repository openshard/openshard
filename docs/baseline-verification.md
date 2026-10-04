# Evidence-aware baseline verification

`openshard verify --compare-base <commit> --strict` preserves ordinary verification
and appends a supplemental baseline_comparison to the local attestation. JSON
output includes it. The Receipt remains immutable, and the original strict exit
code and verification status are never improved by this diagnostic.

Approved pytest checks run independently on temporary detached worktrees for the
base and current clean HEAD, using the same interpreter and installed dependency
set. Dependencies are fingerprinted; source paths prefer each worktree. Nothing
is installed or repaired. This compares that shared environment, not two freshly
provisioned dependency environments. Editable packages/import hooks may influence
imports; it is not an isolation or reproducibility guarantee. Baseline test code
executes with the same local privileges as existing verification; select a trusted
base. Blocked and unapproved commands never run. Non-pytest commands stay incomplete.

JUnit reports are temporary, bounded and reduced to counts. Identifiers (including
parameter IDs) are hashed while comparing, not stored. Duplicate identities,
collection/setup errors, inconsistent exit codes, timeouts, changes to tracked
source or the dependency fingerprint, and unavailable reports remain incomplete.
Missing or additional test identities are counted explicitly. Counts describe
reported test cases, not proof that every possible test ran.

`also_failed_on_base` establishes only that a test identity failed in both runs.
`new_failures` counts head failures not observed failing on the base (including
new test identities); it does not attribute causation to the patch. A regression
or an environmental cause requires further evidence. In particular, the output
never invents “6 environment-incompatible” from six matching failures:
`environment_incompatible` stays null and `environment_cause` is not_established.
A flat red can now be explained with observed baseline facts without turning green.

This first diagnostic is local/CLI evidence in .openshard/verifications.jsonl.
It is not yet part of the hosted verification-evidence sync contract. Historical
Receipts are not rewritten or retroactively populated with model/effort guesses.
