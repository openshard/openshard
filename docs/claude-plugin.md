# Receipts for every Claude session (the Openshard plugin)

Openshard sees a Claude Code session through hooks. Hooks installed on your
computer reach sessions that run on your computer. A Claude **chat** that writes
code (on desktop, web or phone) runs on a fresh cloud machine instead, so
nothing installed on your computer reaches it. A plugin added to your Claude
account does: Claude loads its hooks in every session, wherever it runs.

| Where you work | What captures it |
| --- | --- |
| Claude Code in a terminal, or the desktop app's Code tab | `openshard capture install claude --global` (once per computer) |
| A Claude chat that writes code, on desktop, web or phone | The Openshard plugin (once per Claude account) |
| Claude Code on the web started on a repository with `openshard setup --repo` | That repository's hooks |

On a computer with the user-level hooks installed, the plugin stands aside, so
nothing is recorded twice.

## Set it up

One command does every step below and tells you what is left:

```sh
openshard setup --everywhere
```

Or by hand:

1. On the Platform, create a connected-capture credential: **Settings -> Agents**.
   Save it to a file (not your shell history), for example `~/osc.txt`.
2. Write your plugin:

   ```sh
   openshard plugin claude --token-file ~/osc.txt
   ```

   The endpoint and organisation come from `openshard login`; pass `--endpoint`
   and `--org` otherwise.
3. Add `openshard.plugin` to your Claude account (Customize -> Plugins), or
   share it in a chat and accept it.

Start a new chat and do a small coding task in a repository. Its Receipt shows
on the Platform a few minutes after the session ends.

## What happens in a session

- **A new cloud machine has no Openshard.** The plugin keeps the first events
  (at most 200) in `~/.openshard/plugin-spool/`, installs Openshard in the
  background (about a minute), and delivers the kept events, in order, with
  the first event after the install. They are the session's real events,
  delivered late: timings start when Openshard first saw them.
- **A chat does not start inside a repository.** The session is followed into
  the repositories it works in: the first file it edits there, or the first
  shell command that `cd`s or `git -C`s into one, starts that repository's
  Receipt. Earlier prompts are held and delivered then. A file it only reads
  starts nothing. See [install.md](install.md#capture-every-repository-at-once-claude-code).
- **Receipts reach the Platform before the machine is deleted**, through the
  connected-capture credential, if the plugin was built with one. Without one
  they stay on the cloud machine and are lost with it.

## What to know about the credential

The plugin carries your connected-capture credential in `connection.env`. The
agent in each session can read that file. The credential can only add capture
evidence to your organisation; it cannot read Receipts. If it leaks, revoke it
under **Settings -> Agents** and build a new plugin.

Receipts from a chat are captured inside the agent's own machine. Openshard did
not run or control that session; the Receipt says what the hooks observed.

## Not proven yet

Whether Claude runs a plugin's hooks in every chat surface (desktop, web,
phone) has been checked against Claude's plugin format, not yet on each
surface. Do one small task on each and check the Platform before relying on it.
