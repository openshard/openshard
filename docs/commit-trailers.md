# Agent commit trailers

`openshard setup` marks every commit an agent makes in an Openshard-observed
session, so "agent or human?" can be answered from git itself. The
[merge gate](merge-gate.md) relies on that: a pull request whose commits carry
an agent mark but have no Receipt fails ("an agent changed code but there is
no Receipt").

## What is written

One git trailer, the same key the cloud-receipts workflow and the merge gate
already read:

```text
Openshard-Agent: Claude Code
```

Nothing else is added. Usage trailers (`Openshard-Tokens-*`) stay a separate,
explicit step (`openshard usage trailers`, see
[agent capture](agent-capture.md#github-fallback-metadata-for-cloud-agents)).

## When it is written

Only when both are true at commit time:

1. The process running `git commit` names an agent session in its
   environment. Claude Code exports `CLAUDE_CODE_SESSION_ID` to the commands
   it runs; no other supported agent exposes its session to commands yet,
   so their commits get no trailer (unknown, not "human").
2. Openshard's capture service holds that same session, still open, for this
   repository or one of its worktrees (`.openshard/claude_sessions/`).
   Symlinked capture state is not evidence.

Nothing is added for a human commit, an unknown or ended session, a rebase or
cherry-pick replaying commits (they may be someone else's), a message that
already has `Openshard-Agent` (amend, rebased agent commits: never
duplicated), or a Claude Code cloud session (`CLAUDE_CODE_REMOTE`), which
already adds its own `Claude-Session` trailer that the gate reads. An agent
that amends or merges inside its session marks the new commit.

## How it is installed

`openshard setup` writes `.git/hooks/prepare-commit-msg` (POSIX `sh`; git
runs it with its own `sh` on Windows too). The hook only starts `openshard`
when an agent session variable is set, so human commits pay nothing. It
always exits 0: if Openshard is missing or fails, the commit goes ahead
unmarked.

Existing hooks are kept:

| Situation | What setup does |
|---|---|
| No `prepare-commit-msg` | Installs the hook |
| Your own `prepare-commit-msg` | Keeps it as `prepare-commit-msg.pre-openshard`; it runs first and its exit status still decides |
| `core.hooksPath` set (husky, shared or global hooks) | Writes nothing; prints the line to add |
| lefthook or pre-commit owns `prepare-commit-msg` | Writes nothing; prints the config to add |

The line to add by hand, in any hook manager:

```sh
openshard hooks git-commit-msg "$1" "$2" "$3" || true
```

For husky: `echo 'openshard hooks git-commit-msg "$1" "$2" "$3" || true' >> .husky/prepare-commit-msg`.

`openshard doctor` shows "Agent commit trailers" and, when the hook is
missing, how to enable it. Existing installs are not changed on upgrade:
re-run `openshard setup`.

## Opting out

- `openshard setup --no-commit-trailers` sets
  `git config openshard.commitTrailers false` for the repository and removes
  Openshard's hook (restoring a chained one).
- `git config --global openshard.commitTrailers false` turns it off everywhere.
- `OPENSHARD_COMMIT_TRAILERS=off` turns it off for one environment.

With the opt-out, agent commits carry no mark, and the merge gate treats a
pull request without a Receipt as a human change under `require_receipt =
agent_prs`.
