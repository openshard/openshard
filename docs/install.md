# Installing Openshard

## Recommended: pipx

pipx installs Openshard in an isolated environment, so its dependencies don't conflict with other Python tools.

If you don't have pipx yet:

```sh
# macOS
brew install pipx

# Linux / Windows (via pip)
pip install pipx
```

Then install Openshard:

```sh
pipx install openshard
```

Set up receipt capture for a repository (once per repository, safe to re-run):

```sh
cd my-project
openshard setup
```

`openshard setup` detects whichever of Claude Code, Codex, Cursor,
OpenCode, Google Antigravity or Grok Build are available for the repo and
configures capture for each (for the Antigravity IDE without the `agy` CLI
on PATH, run `openshard capture install antigravity`; for Grok Build, trust
the folder with `/hooks-trust` or `--trust`). Then
use your coding agent normally and look at what was captured:

```sh
openshard last                   # the newest receipt
openshard history                # recent receipts for this repository
openshard context "some task"    # what Openshard would surface for that task, and why
openshard stats                  # counts over everything recorded here
```

These work from any subdirectory of the repository and never need a network connection or account. `openshard doctor` answers "is Openshard actually working here?", and `openshard mcp uninstall claude` removes Openshard's Claude Code configuration again (local history is never deleted).

Or run the TUI:

```sh
openshard tui
```

Upgrade later:

```sh
pipx upgrade openshard
```

## Alternative: uv tool

If you use [uv](https://docs.astral.sh/uv/):

```sh
uv tool install openshard
```

## Local development

```sh
git clone https://github.com/openshard/openshard.git
cd openshard
pip install -e .
```

## Notes

- **pipx** is recommended for CLI users — isolated environment, clean upgrades, no conflicts with system Python.
- **uv tool** is also supported where available.
- **pip install** works but is less ideal for end users (installs into the active environment).
- Homebrew and curl installers are future release steps.
