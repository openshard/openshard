# OpenShard 3-Minute Demo Script

A deeper demo showing OpenShard on production-shaped engineering work. Uses the HarbourDocs infrastructure scenario from `examples/production-infra-demo/`.

## Pre-recording repo setup

The recording runs from a copy of `examples/production-infra-demo` placed **outside** the
OpenShard repo. This prevents `openshard tui` from detecting the OpenShard git root instead
of the HarbourDocs infrastructure repo.

Run once before recording (PowerShell):

```powershell
robocopy examples\production-infra-demo `
    C:\Users\Michael\HarbourDocs\harbourdocs-infra /E /NP
```

Then start the recording from that directory:

```powershell
cd C:\Users\Michael\HarbourDocs\harbourdocs-infra
```

## Pre-recording checklist

- [ ] Repo copy is in place: `C:\Users\Michael\HarbourDocs\harbourdocs-infra`
- [ ] Clean terminal — no open TUI, no previous output visible
- [ ] Zoom level: large font, high contrast (viewer must read Shard output)
- [ ] Working directory: `C:\Users\Michael\HarbourDocs\harbourdocs-infra`
- [ ] No secrets visible in shell history, env, or `.openshard/` output — check before recording
- [ ] Confirm `.openshard/` run output is public-safe: no real project IDs, real IPs, or company names
- [ ] Use the existing recorded run for the receipt — do not rerun the $0.30 model task unless explicitly approved

---

## Act 1 — Install (~20 seconds)

**Say:**
> "OpenShard installs in one command. No hosted infrastructure — it runs locally."

**Do:**
```bash
pipx install openshard
```

**Say:**
> "For local development you'd clone and use `pip install -e .` — but for a standard install, pipx is the right path."

**Viewer sees:** Install output. Single command, published on PyPI.

---

## Act 2 — Navigate to the HarbourDocs infrastructure repo and show the pack prompt (~30 seconds)

**Do:**
```powershell
cd C:\Users\Michael\HarbourDocs\harbourdocs-infra
openshard packs prompt production-iac-hardening
```

**Say:**
> "This is the production-iac-hardening pack. HarbourDocs is a fictional document-processing platform — entirely public-safe. No real secrets, no real company. The Terraform is deliberately flawed so we get real findings."

**Viewer sees:** Raw pack prompt text printed to the terminal.

---

## Act 3 — Show existing run output (~30 seconds)

**Do:**
```bash
openshard last
```

**Say:**
> "Here's the most recent run against the HarbourDocs Terraform. I'm using the existing recorded output rather than running the model again."

**Viewer sees:** Compact receipt block — task summary, model, cost, result.

---

## Act 4 — Walk through the full Shard receipt (~60 seconds)

**Do:**
```bash
openshard last --more
```

Walk each section by name as it appears on screen:

- **TASK** — The exact task text that was sent to the model. Nothing paraphrased.
- **EXECUTION** — Agent, strategy, model used, duration, and final status.
- **CONTEXT** — Repo, branch, git state, files read, files touched. May show "Not recorded" for non-native run types — that is honest, not a gap.
- **INSPECTED FILES** — The specific files recorded as inspected, when that provenance exists.
- **POLICY** — Risk level, sandbox state, allowed and blocked paths.
- **CHECKS** — Post-task verification checks. Shows result per check or "Not run."
- **FINDINGS** — Structured findings grouped by severity: Critical, High, Medium, Low, Note. Only renders when the run produced structured findings.
- **CHANGES** — Files changed with diff line counts. The model proposed these; you decide whether to apply.
- **COST** — Exact cost for this run in dollars.
- **FEEDBACK** — Appears here if you ran `openshard feedback` after the task.

---

## Act 5 — Honest gaps and value close (~30 seconds)

**Say:**
> "A few things worth being clear about."
>
> "Findings only appear when the run produced structured findings. Not every task does — and we do not fabricate them."
>
> "Context may show Not recorded if the run type didn't collect it. That's intentional honesty, not a missing feature."
>
> "There's no hosted product, no cloud sync, no team dashboard yet. This is a local tool."

**Pause.**

**Say:**
> "AI agents can generate code. OpenShard records, controls, and reviews the work before you trust it."

**Viewer sees:** Terminal with last Shard output still visible.

---

## Notes

- Pause after each command — let the viewer read before continuing.
- HarbourDocs Terraform is deliberately flawed, but the Findings section only shows structured findings when the run records them. Otherwise it will honestly say no structured findings were recorded.
- If the task is unexpectedly re-run and takes longer than expected, use `openshard last --more` to show the prior run's receipt while waiting.
- Do not include real project IDs, real IPs, real secrets, or real company names in any visible output.
- Do not claim PyPI, Homebrew, cloud sync, or external adapter support (Claude Code, Codex, OpenCode) unless implemented.
