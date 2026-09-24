# Contributing to Openshard

Thanks for your interest in contributing.

## The one rule

A Receipt is an evidence record. Code that touches capture, history or
rendering must never make a stronger claim than its evidence supports.
Prefer *observed*, *agent-reported*, *independently verified*, *unknown*
and *incomplete* over filling a gap. If Openshard loses evidence, cannot
prove causality or cannot authenticate an event, the Receipt must become
more explicit and more conservative, never quietly more confident. See
`docs/architecture.md` for the receipt path and its evidence levels.

## Development environment

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m ruff check .
python -m mypy openshard/ --ignore-missing-imports
python -m pytest
```

That is exactly what CI runs after merge (Linux and Windows, Python 3.11
and 3.12). Pull requests get a faster gate that selects tests from the
changed files and always runs the evidence invariants; see
[docs/ci.md](docs/ci.md) for the tiers and how to preview what your change
will run (`python scripts/ci/select_tests.py explain <paths>`).
The suite isolates itself from any real capture service on your machine
(`tests/conftest.py`); never make a test depend on one being up or down.

## What we'd love help with

- Capture fidelity and honesty for the supported agents (Claude Code,
  Codex, Cursor, OpenCode); new agent integrations are a separate,
  deliberate decision
- Receipt rendering that states facts precisely
- Repo analyzers, model profiles, evaluation datasets, provider
  integrations, CLI UX

## How to contribute

1. Fork the repo and create a branch for your change.
2. Add or update tests first where the change is a behaviour fix.
3. Keep schema changes additive: old `runs.jsonl` records must still
   render, and `--json` consumers must keep every existing field.
4. Do not broaden telemetry; `docs/telemetry.md` is the contract.
5. Open a pull request explaining what changed and why.

## Security

Report vulnerabilities privately as described in `SECURITY.md`.

## Questions

Open an issue and we'll respond as soon as we can.
