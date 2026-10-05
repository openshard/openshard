# Next Run release handoff

## Delivered
- v0.4.11 published and verified as a public installed package.
- Native Claude telemetry ingestion and observed session usage deployed: platform PR #91; Receipt rcpt_92542199a7f82b13043e508f7d6e3fca.
- Native prompt handoff contract deployed: platform PR #92; Receipt rcpt_2f3fb3bba42bb6dd5942d260562e6141.
- Local Claude/Codex prompt learning merged: Core PR #412; Receipt rcpt_f7f731884e55db3e694dbcea4fdd9c8c; 10722 tests passed.
- Hosted next-task history deployed: platform PR #93; Receipt rcpt_09418fe94c8f49a8b7d3c5b1345282eb; 922 tests passed.
- Supporting hosted Receipt links deployed: platform PR #94; Receipt rcpt_af08427f8cc8a97fa52c3d3e65f4d514; 924 tests passed, generated schema checked.
- Opt-in hosted prompt hooks merged: Core PR #413; Receipt rcpt_5229e378cc538cb17568cb0202dd64e0; 10758 tests passed, 3 skipped.

The first PR #94 head had a passed four-check Receipt but failed the separate generated-schema CI gate. Both results are retained in its PR. Exact succeeding commits were green before merge.

## Publication
v0.4.12 packages the local and hosted hooks. Title: Openshard v0.4.12 - The Next Run Release. Release notes are plain Markdown in CHANGELOG.md. The release workflow installs the actual wheel outside the checkout and exercises hosted hooks before publishing.

Local execution is unavailable; CI is the validation source. Publication is not complete until the protected PyPI job succeeds and the public package is confirmed.

## Still open
- Actual Scribe/Tether session diagnostics and launch-time native telemetry configuration; current app browser access redirects to login.
- A real cloud acceptance run with repeated requests, model changes, export retries, final flush and Receipt delivery.
- Complete task-level usage/cost attribution across resumed sessions. Observed session totals are not full task bills.
- Evidence contracts and real runtime acceptance for additional cloud/mobile surfaces such as Herdr, Replicas and JCode. Generic capture protocol does not prove automatic runtime support.
- Demonstrating agents consume advice, implementing further evidence-backed routing/recovery decisions, and measuring outcomes. Hook emission is only a verified handoff.
- Drive progress-log updates: connector write access to Volume 2 was denied. Repository and PR delivery logs are complete; no Drive write is claimed.

Easy first-user onboarding remains excluded from the current implementation scope.
