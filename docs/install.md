# Installing OpenShard

## Recommended: pipx

pipx installs OpenShard in an isolated environment, so its dependencies don't conflict with other Python tools.

If you don't have pipx yet:

```sh
# macOS
brew install pipx

# Linux / Windows (via pip)
pip install pipx
```

Then install OpenShard:

```sh
pipx install git+https://github.com/MichaelObasa/openshard.git
```

Set up Claude Code capture for a repository (once per repository, safe to re-run):

```sh
cd my-project
openshard setup
```

Then use Claude Code normally and run `openshard last` to see the receipt. `openshard doctor` answers "is OpenShard actually working here?", and `openshard mcp uninstall claude` removes OpenShard's Claude Code configuration again (local history is never deleted).

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
uv tool install git+https://github.com/MichaelObasa/openshard.git
```

## Local development

```sh
git clone https://github.com/MichaelObasa/openshard.git
cd openshard
pip install -e .
```

## Notes

- **pipx** is recommended for CLI users — isolated environment, clean upgrades, no conflicts with system Python.
- **uv tool** is also supported where available.
- **pip install** works but is less ideal for end users (installs into the active environment).
- PyPI, Homebrew, and curl installers are future release steps.
