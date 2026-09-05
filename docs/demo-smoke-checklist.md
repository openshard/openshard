# HarbourDocs Demo Smoke Checklist

Validates the OpenShard public demo flow end-to-end.
Run from repo root unless noted. Last validated: 2026-05-20.

---

## 1. Workflow packs (from repo root)

```bash
openshard packs list
openshard packs show production-iac-hardening
openshard packs prompt production-iac-hardening
```

- [ ] `packs list` - 6 packs listed with aligned columns, no crash
- [ ] `packs show` - all fields present, labels spaced correctly, no crash
- [ ] `packs prompt` - prints only the raw prompt text, nothing else

---

## 2. TUI (from `C:\Users\Michael\HarbourDocs\harbourdocs-infra`)

**Manual test - cannot be driven non-interactively.**

Copy the fixture outside the OpenShard repo first (prevents git root detection):

```powershell
robocopy examples\production-infra-demo `
    C:\Users\Michael\HarbourDocs\harbourdocs-infra /E /NP
cd C:\Users\Michael\HarbourDocs\harbourdocs-infra
openshard tui
```

Inside TUI:
```
/packs
/pack production-iac-hardening
```

- [ ] TUI launches - hero panel shows project name, branch, git state
- [ ] `/packs` renders a list of 6 packs cleanly
- [ ] `/pack production-iac-hardening` shows: title, category, summary, full prompt, "How to run"
- [ ] Prompt is fully readable in the output panel
- [ ] No internal terms or fake company/customer names in pack output

---

## 3. Run (from `C:\Users\Michael\HarbourDocs\harbourdocs-infra`)

```bash
openshard run "Review and harden this deliberately flawed Terraform codebase. Assess it through security/compliance posture, 2am operability, and developer experience for a 5-10 person engineering team. Identify critical, high, and medium risks. Explain trade-offs. Do not apply changes directly without review."
```

> Takes ~5-6 minutes and costs ~$0.30. Run once only.

- [ ] Command runs without crashing
- [ ] Progress shows routing and planning/execution stages
- [ ] Run completes and prints a compact live receipt (using `+--...--+` borders)
- [ ] Output summary mentions security, operability, or developer experience
- [ ] Run is recorded (confirmed by `openshard last` working after)

---

## 4. Receipt - default view

```bash
openshard last
```

- [ ] No crash or UnicodeEncodeError
- [ ] Shows Task, timestamp, summary, Model line
- [ ] Compact RECEIPT block renders with border characters
- [ ] Key fields visible: Agent, Strategy, Model, Risk, Cost, Result
- [ ] No obviously wrong "Not recorded" - Cost and Model should be real values

---

## 5. Receipt - expanded view

```bash
openshard last --more
```

- [ ] No crash
- [ ] Compact RECEIPT renders near top
- [ ] Full SHARD block renders with sections: TASK, EXECUTION, CONTEXT, INSPECTED FILES, POLICY, CHECKS, FINDINGS, CHANGES, COST, RECEIPT
- [ ] EXECUTION section: Model, Strategy, Duration, and Status are present
- [ ] CONTEXT section: may show "Not recorded" for Repo/Branch/Git state on non-native runs - this is honest
- [ ] INSPECTED FILES: "Not recorded" is acceptable for non-native runs - not a lie
- [ ] FINDINGS: shows "No structured findings recorded." or real structured findings - never fabricated
- [ ] CHANGES: lists the files created/updated with descriptions
- [ ] Notes section shows useful information about the run
- [ ] Internal terms ("candidate", "native-loop-candidate") may appear in the Routing/Form factor diagnostic lines below the SHARD block - acceptable
- [ ] No internal terms inside the RECEIPT or SHARD sections themselves
- [ ] No fake employer, customer, or private company strings
- [ ] FEEDBACK section is absent before feedback is given

---

## 6. Feedback

```bash
openshard feedback accept
```

- [ ] Command runs cleanly
- [ ] Output confirms: shard ID, outcome, and note
- [ ] No crash

```bash
openshard last --more
```

- [ ] FEEDBACK section now appears at the bottom
- [ ] Shows `Outcome     accepted`
- [ ] Shows `Note        Demo smoke test result`

---

## Pass criteria

All checkboxes ticked (manual TUI steps confirmed by human). Specifically:

- No crashes in any CLI command
- `openshard last` does not throw UnicodeEncodeError on Windows
- Compact RECEIPT and full SHARD sections are clean and beginner-readable
- FINDINGS is honest - never fabricated
- INSPECTED FILES is honest - "Not recorded" when data wasn't collected
- FEEDBACK section appears after `openshard feedback` is run
- No internal terms in the product-facing RECEIPT/SHARD sections
- No fake employer or customer strings anywhere in output

---

## Known acceptable gaps (not failures)

- `Sandbox: Not recorded` and `Approval: Not recorded` in RECEIPT/SHARD - not populated for this run type
- `CONTEXT` fields "Not recorded" - git and file-context metadata not collected by non-native executor
- `INSPECTED FILES: Not recorded` - same reason
- Internal terms in `Routing` / `Form factor` lines below the SHARD block - diagnostic section, not product-facing
- TUI cannot be automated - requires manual verification
