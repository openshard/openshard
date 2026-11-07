# Installing Openshard

Openshard is a Python package (`openshard` on PyPI) and needs **Python 3.11
or newer** and **git**. It runs on macOS, Windows and Linux; CI runs the
full test suite on all three after every merge and before every release.

Agent hooks start `openshard` by name, so after installing, make sure
`openshard --version` works in the same kind of shell your agent runs in.

## Install

### macOS

pipx or uv keep Openshard in its own environment and put `openshard` on
your PATH:

```sh
brew install pipx && pipx ensurepath   # once; then open a new terminal
pipx install openshard
```

or, with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install openshard
```

Plain pip works inside a virtual environment:

```sh
python3 -m venv ~/.venvs/openshard && ~/.venvs/openshard/bin/pip install openshard
```

Notes for macOS:

- `/usr/bin/python3` from the Xcode Command Line Tools is Python 3.9, which
  is too old. Use Homebrew's Python (`brew install python`), pipx or uv
  (both fetch a suitable interpreter for you).
- Homebrew's Python refuses `pip install` outside a virtual environment
  (`externally-managed-environment`); that is why pipx, uv or a venv is
  recommended rather than `pip install --user`.
- An editor started from the Dock or Finder may not see the PATH your
  terminal has (`~/.local/bin`, `/opt/homebrew/bin`). If `openshard doctor`
  shows hooks installed but no events arrive from an IDE-based agent, start
  the IDE from a terminal and try again.

### Windows

```powershell
py -m pip install --upgrade openshard
```

`pipx install openshard` and `uv tool install openshard` work too. If
`openshard` is not found afterwards, add the Scripts directory pip prints a
warning about to your PATH, or use pipx (`pipx ensurepath`).

### Linux desktop

```sh
pipx install openshard        # Debian/Ubuntu: sudo apt install pipx && pipx ensurepath
# or
uv tool install openshard
```

As on macOS, recent distribution Pythons refuse a system-wide
`pip install` (`externally-managed-environment`); use pipx, uv or a virtual
environment.

### Linux VMs and agent sandboxes

For a cloud or VM sandbox (Claude Code on the web, Replicas, Devin, a
devcontainer and similar), add one non-interactive line to the environment's
setup script. Run it from the checked-out repository:

```sh
python3 -m pip install --upgrade openshard && openshard capture install claude --defer-service
```

- Replace `claude` with the agent the sandbox runs (`codex`, `cursor`,
  `opencode`, `antigravity`, `hermes`, `grok-build`); pass `--repo-path DIR`
  when the script does not run inside the repository.
- `--defer-service` installs the hooks without starting the capture
  service; the agent's first hook starts it, so the service inherits the
  agent's network proxy and credentials.
- A Claude Code session with several repositories starts in their parent
  directory; install from there instead:
  `openshard capture install claude --defer-service --workspace /home/user`
  (see [claude-cloud-prelaunch.md](claude-cloud-prelaunch.md)).
- Many sandboxes run as root with no virtual environment. If pip stops with
  `externally-managed-environment`, the image's Python is managed by the
  distribution. Prefer `pipx install openshard` or `uv tool install
  openshard` if either is available; in a disposable sandbox only, add
  `--break-system-packages` to the pip command. Do not use that flag on a
  machine you keep.
- If pip installs to `~/.local/bin` (a `--user` install) and that is not on
  PATH, the `openshard` command is not found; add
  `export PATH="$HOME/.local/bin:$PATH"` to the setup script.

To commit a repository configuration that cloud agents pick up from a fresh
clone instead (no per-sandbox setup script), use `openshard setup --repo`;
see [remote-capture.md](remote-capture.md).

## Set up capture for a repository

Once per repository, safe to re-run:

```sh
cd my-project
openshard setup
```

`openshard setup` detects whichever of Claude Code, Codex, Cursor,
OpenCode, Google Antigravity or Grok Build are available for the repo and
configures capture for each (for the Antigravity IDE without the `agy` CLI
on PATH, run `openshard capture install antigravity`; for Grok Build, trust
the folder with `/hooks-trust` or `--trust`). Hermes Agent's hooks live in
its user-global config (`~/.hermes/config.yaml`, or `%LOCALAPPDATA%\hermes`
on Windows), so `setup` only detects it: run
`openshard capture install hermes` (in each repository you want captured) to
enable it. Then use your coding agent normally and look at what was
captured:

```sh
openshard last                   # the newest receipt
openshard history                # recent receipts for this repository
openshard context "some task"    # what Openshard would surface for that task, and why
openshard stats                  # counts over everything recorded here
```

These work from any subdirectory of the repository and never need a network connection or account.
The history lives in `.openshard/` inside the repository; `setup` keeps that runtime state out of
git through the repository's local `.git/info/exclude` (only `.openshard/config.yml`, the
verification contract, stays committable). `openshard doctor` answers "is Openshard actually working here?", and `openshard mcp uninstall claude` removes Openshard's Claude Code configuration again (local history is never deleted).

Or run the TUI:

```sh
openshard tui
```

## Capture every repository at once (Claude Code)

To have every Claude Code session get Receipts, in whichever repository it
works in, with no per-repository setup, install once for your user account:

```sh
openshard capture install claude --global
openshard login        # once, so Receipts from every repository reach the Platform
```

This writes Openshard's hooks into Claude Code's user settings
(`~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json`), which
Claude Code loads for every session wherever it starts. Your other settings
and hooks are left as they were. Restart open Claude Code sessions to pick it
up. What it records, and what it leaves alone:

| Where the session starts | What happens |
| --- | --- |
| Inside any git repository, set up or not | Receipts are recorded in that repository's `.openshard/`. |
| A repository with its own Openshard hooks (`openshard setup`, `setup --repo`) | Its own hooks capture it; the user-level hook stands aside, so nothing is recorded twice. If those hooks cannot find `openshard`, the user-level hook captures instead. |
| A folder that is not a git repository (including a folder that holds several repositories) | Nothing is recorded and no `.openshard/` is created. Start the session inside the repository to capture it. |
| Your home directory | Never captured, even if it is a git repository. |

`openshard capture uninstall claude --global` removes only these hooks;
recorded history is never deleted. Cloud sessions (Claude Code on the web)
do not read your computer's user settings: see
[claude-cloud-prelaunch.md](claude-cloud-prelaunch.md).

## Upgrade

```sh
pipx upgrade openshard            # pipx
uv tool upgrade openshard         # uv
python3 -m pip install --upgrade openshard   # pip (Windows: py -m pip ...)
```

## Local development

```sh
git clone https://github.com/openshard/openshard.git
cd openshard
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the checks CI runs.

There is no Homebrew formula or `curl | sh` installer yet.
