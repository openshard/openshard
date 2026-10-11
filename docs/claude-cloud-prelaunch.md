# Claude cloud prelaunch capture

Use `openshard capture install claude --defer-service` inside the checked-out Git repository after installing a Core build that contains this command. This configures the existing project-local Claude hooks without requiring the Claude CLI or installing MCP. The first native hook starts the existing capture service with the launched runtime environment. Ordinary local onboarding remains `openshard setup`.

Claude setup scripts run before Claude launches; network-secret proxy injection does not apply to setup-script requests. Preserve the configured network secret and set `OPENSHARD_CONNECTED_TOKEN=proxy-injected` and `OPENSHARD_CONNECTED_CREDENTIAL_MODE=proxy` alongside the existing endpoint, organisation and cloud surface settings. Never put the actual connection token in a tracked file.

Configure only existing checkouts; do not assume both Core and Platform are present. These instructions require a coordinated Core release or a tested pinned public Git revision; the observed PyPI 0.4.12 build lacks this installer.

Official references: [cloud environments](https://code.claude.com/docs/en/cloud-environments), [hooks](https://code.claude.com/docs/en/hooks). Installed hooks and passing local tests do not prove a hosted run reached Remote. Validate native events, Receipt creation and usage separately; missing usage remains unavailable.

## Sessions with several repositories

A Claude Code cloud session with more than one repository starts in their parent directory (`/home/user`), and Claude Code loads project hooks only from there, so hooks installed inside each checkout never fire. Install from the parent instead:

```bash
openshard capture install claude --defer-service --workspace /home/user
```

This configures every checkout directly under the directory (for sessions that start inside one) and the directory itself, marked with `.openshard/workspace.json`. When `OPENSHARD_CONNECTED_SURFACE=claude-code-web` (turns sealed at `Stop`), workspace events are recorded into the checkout they touched, by file path or working directory:

- each touched checkout gets its own Receipt for the turn, with the task and session start replayed at their original times;
- a turn that touches no checkout is not attributed to any repository;
- the turn's transcript usage is counted once, on the first checkout it touched; the others record `workspace_usage_attributed_to_other_repository` instead of tokens and cost.

Agent identity comes from Claude Code's own hooks reaching the capture service with the workspace capability, not from commit trailers. A pushed commit without `Openshard-Agent` metadata still receives its GitHub CI verdict when such a Receipt recorded that exact commit (`attach_only`); otherwise nothing is recorded for it.

## Guidance before the task starts

Add `--learn` to either install line to also install the hosted prompt-learning hook (`openshard learn hook claude --hosted`, see `docs/hosted-learning.md`) in the workspace and in every checkout:

```bash
openshard capture install claude --defer-service --learn --workspace /home/user
```

On each `UserPromptSubmit` the hook asks the Platform for bounded, verified check history for this repository and similar task terms, and hands it to Claude as advisory context. A cloud environment holds no organisation read key: the hook uses the connection's own credential (the proxy marker in Claude Cloud, a runtime secret in Cursor), and the Platform answers only for repositories that connection has itself captured, with check names, pass/fail counts and supporting Receipt ids, never Receipts. The Receipt records whether context was emitted (`learning.context_delivery`); it never records that the agent used it. Missing access, a 403, a timeout or empty history emit nothing and never delay the prompt.

## Who the Receipt names

The hosted Receipt keeps Core's `owner` exactly as recorded (only an explicit `openshard config set-owner`, which a cloud environment does not have). Beside it, the dashboard shows the Openshard account that created the connection under Settings -> Agents as "connected by": a sign-in-verified identity, but the credential's owner, not proof of who launched the task, because anyone using the environment presents the same credential and the host does not say who pressed start.
