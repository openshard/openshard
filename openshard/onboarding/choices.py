"""Shared onboarding choice constants.

Pure data — no imports from the rest of the codebase.
Both CLI (openshard/cli/ui/onboarding.py) and TUI (openshard/tui/onboarding_screen.py)
import from here to avoid a cross-layer dependency.

Each tuple is (label, value, note, is_planned).
"""
from __future__ import annotations

USER_TYPE_CHOICES: list[tuple[str, str, str, bool]] = [
    ("I'm a Human", "human", "", False),
    ("I'm an AI Agent", "agent", "", False),
    ("Just exploring / demo", "demo", "", False),
]

EXECUTOR_CHOICES: list[tuple[str, str, str, bool]] = [
    ("OpenShard Native (recommended)", "native",
     "Use OpenShard's own coding agent, built to work with receipts, policy, checks, and approvals.", False),
    ("Connect an installed CLI agent", "cli_agent",
     "Use Claude Code CLI, Codex CLI, OpenCode, etc. if already installed.", False),
    ("Review output from another tool", "review",
     "Use this if you work in Claude.ai, Claude Desktop, Cursor, VS Code, or another tool.", False),
    ("Agent / CI setup", "agent_ci",
     "Machine-readable setup for agents, scripts, and automation.", False),
    ("Demo mode", "demo",
     "Try OpenShard receipts without connecting a model.", False),
]

# Legacy executor values written by older configs. Kept so summaries and JSON
# render them safely without migrating or rewriting existing config files.
LEGACY_EXECUTOR_LABELS: dict[str, str] = {
    "claude_code": "Claude Code",
    "codex": "Codex / OpenAI",
    "opencode": "OpenCode",
    "goose": "Goose",
    "antigravity": "Antigravity CLI",
    "other": "Other",
}

PROVIDER_ROUTE_CHOICES: list[tuple[str, str, str, bool]] = [
    ("OpenRouter aggregator", "openrouter", "Broadest model access through one key.", False),
    ("Direct provider API", "direct", "Connect directly to a provider's API.", False),
    ("Skip for now / demo mode", "demo", "No key required. Limited to local operations.", False),
]

DIRECT_PROVIDER_CHOICES: list[tuple[str, str, str, bool]] = [
    ("Anthropic (direct)", "anthropic", "Set ANTHROPIC_API_KEY.", False),
    ("OpenAI (direct)", "openai", "Set OPENAI_API_KEY.", False),
    ("Google Gemini (direct planned)", "google", "Direct support is planned. Uses local-only mode until available.", True),
    ("xAI Grok (direct planned)", "xai", "Direct support is planned. Uses local-only mode until available.", True),
    ("DeepSeek (direct planned)", "deepseek", "Direct support is planned. Uses local-only mode until available.", True),
    ("Moonshot / Kimi (direct planned)", "moonshot", "Direct support is planned. Uses local-only mode until available.", True),
    ("GLM / Zhipu (direct planned)", "glm", "Direct support is planned. Uses local-only mode until available.", True),
    ("MiniMax (direct planned)", "minimax", "Direct support is planned. Uses local-only mode until available.", True),
    ("Other / custom", "other", "", False),
]

SAFETY_PROFILE_CHOICES: list[tuple[str, str, str, bool]] = [
    (
        "Recommended",
        "recommended",
        "ask before risky actions · keep receipts · run checks where possible",
        False,
    ),
    (
        "Strict",
        "strict",
        "ask more often · safer for production repos · stronger review posture",
        False,
    ),
    (
        "Fast",
        "fast",
        "fewer prompts · still writes receipts · good for low-risk local work",
        False,
    ),
]

LOCAL_FIRST_NOTICE = (
    "OpenShard is local-first.\n\n"
    "  Your receipts stay on this machine by default.\n"
    "  API keys stay in your environment variables.\n"
    "  Your code, prompts, file names, repository names and receipt contents\n"
    "  are never sent anywhere, and your runs are never used for training.\n\n"
    "  Help improve OpenShard: on. OpenShard shares anonymous usage and\n"
    "  reliability data (counts, versions, timings, errors by category) to\n"
    "  improve the product. Turn it off any time:  openshard telemetry off\n"
    "  Details: docs/telemetry.md"
)

NEXT_COMMANDS = (
    "  openshard demo shard\n"
    "  openshard env\n"
    "  openshard run \"explain this repo\"\n"
    "  openshard last --more"
)
