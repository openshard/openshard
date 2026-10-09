# Claude cloud prelaunch capture

Use `openshard capture install claude --defer-service` inside the checked-out Git repository after installing a Core build that contains this command. This configures the existing project-local Claude hooks without requiring the Claude CLI or installing MCP. The first native hook starts the existing capture service with the launched runtime environment. Ordinary local onboarding remains `openshard setup`.

Claude setup scripts run before Claude launches; network-secret proxy injection does not apply to setup-script requests. Preserve the configured network secret and set `OPENSHARD_CONNECTED_TOKEN=proxy-injected` and `OPENSHARD_CONNECTED_CREDENTIAL_MODE=proxy` alongside the existing endpoint, organisation and cloud surface settings. Never put the actual connection token in a tracked file.

Configure only existing checkouts; do not assume both Core and Platform are present. These instructions require a coordinated Core release or a tested pinned public Git revision; the observed PyPI 0.4.12 build lacks this installer.

Official references: [cloud environments](https://code.claude.com/docs/en/cloud-environments), [hooks](https://code.claude.com/docs/en/hooks). Installed hooks and passing local tests do not prove a hosted run reached Remote. Validate native events, Receipt creation and usage separately; missing usage remains unavailable.
