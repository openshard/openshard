"""A stand-in for the OpenCode CLI used only by tests/test_pr13_scenarios_2to7.py
(Scenario 7, cross-agent handoff).

Real OpenCode capture works entirely through a Node plugin running inside
the real `opencode` process, which this benchmark cannot fake without a
live OpenCode session. Instead, exactly like `pr13_fake_claude.py` does
for Claude Code hooks, this script edits the workspace the way a real
agent would and then calls OpenShard's *production* translator/fold
(`handle_hook(..., agent="opencode")`) directly with the same document
shapes the real plugin sends (`opencode_plugin.extract_opencode_payload`'s
own docstring), so the resulting Shard is produced by the real capture
code path, not fabricated by this test double.

Argv shape (verified against the installed `opencode run --help`):
    opencode run --dir <ws> --format json --model <provider/model> <prompt>

Modes (env vars, mirroring pr13_fake_claude.py):
* ``PR13_FAKE_OPENCODE_MODE``: ``naive`` (hand-edit relay/_schema.py) or
  ``correct`` (edit schema/jobs.json and regenerate).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def naive_tags_edit(ws: Path) -> list[str]:
    """Scenario 7's known failed approach: hand-edit relay/_schema.py directly."""
    schema = ws / "relay" / "_schema.py"
    text = schema.read_text(encoding="utf-8")
    old = "    Field('retries', int, False, 0, 'How many times to retry the job after a failure.'),\n)"
    assert old in text, "seed relay/_schema.py changed; update the fake"
    new = (
        "    Field('retries', int, False, 0, 'How many times to retry the job after a failure.'),\n"
        "    Field('tags', str, False, '', 'Comma-separated tags.'),\n)"
    )
    schema.write_text(text.replace(old, new), encoding="utf-8")
    return ["relay/_schema.py"]


def correct_tags_edit(ws: Path) -> list[str]:
    """The correct approach: edit schema/jobs.json and regenerate."""
    spec_path = ws / "schema" / "jobs.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["fields"].append({"name": "tags", "type": "str", "required": False, "default": "",
                           "help": "Comma-separated tags."})
    spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "scripts/gen_schema.py"], cwd=ws, check=True, capture_output=True)
    return ["schema/jobs.json", "relay/_schema.py"]


def parse_argv(argv: list[str]) -> dict:
    out: dict = {"dir": None, "model": None, "prompt": None}
    i = 0
    positional: list[str] = []
    if argv and argv[0] == "run":
        i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--dir":
            out["dir"] = argv[i + 1]
            i += 2
        elif a == "--model":
            out["model"] = argv[i + 1]
            i += 2
        elif a in ("--format",):
            i += 2
        else:
            positional.append(a)
            i += 1
    out["prompt"] = " ".join(positional)
    return out


def deliver_opencode_hooks(ws: Path, session_id: str, prompt: str, files: list[str], model: str) -> None:
    """Post the same document shapes the real OpenCode plugin sends, straight
    to OpenShard's production translator/fold (bypassing only the Node
    runtime and the HTTP hop -- see the module docstring)."""
    from openshard.adapters.claude_hooks import handle_hook

    env = {"CLAUDE_PROJECT_DIR": str(ws), "OPENSHARD_CAPTURE_DISABLE": "1"}
    base = {"session_id": session_id, "directory": str(ws)}
    handle_hook({**base, "event": "session.created"}, env=env, agent="opencode")
    provider, _, model_id = model.partition("/")
    handle_hook({**base, "event": "chat.message", "prompt": prompt, "provider_id": provider or None,
                "model_id": model_id or model}, env=env, agent="opencode")
    for rel in files:
        handle_hook({**base, "event": "tool.execute.after", "tool": "edit", "file_path": str(ws / rel)},
                    env=env, agent="opencode")
        handle_hook({**base, "event": "file.edited", "file_path": str(ws / rel)}, env=env, agent="opencode")
    handle_hook({**base, "event": "tool.execute.after", "tool": "bash", "command": "python -m unittest discover -s tests"},
                env=env, agent="opencode")
    handle_hook({**base, "event": "session.deleted"}, env=env, agent="opencode")


def fail_unknown_model(ws: Path, session_id: str, prompt: str, model: str) -> int:
    """Reproduce, byte-for-byte in shape, what the real OpenCode did in the live
    Scenario 7 pilot when handed a model id that is not 'provider/model': it
    creates the session, records the prompt, emits two error events, goes
    idle (never 'deleted'), makes no tool calls, touches no files, exits 1.
    The plugin still captures a (task-only) Shard for that session."""
    from openshard.adapters.claude_hooks import handle_hook

    env = {"CLAUDE_PROJECT_DIR": str(ws), "OPENSHARD_CAPTURE_DISABLE": "1"}
    base = {"session_id": session_id, "directory": str(ws)}
    handle_hook({**base, "event": "session.created"}, env=env, agent="opencode")
    handle_hook({**base, "event": "chat.message", "prompt": prompt}, env=env, agent="opencode")
    print(json.dumps({"type": "error", "sessionID": session_id,
                      "error": {"name": "UnknownError", "data": {"message": f"Model not found: {model}/."}}}))
    print(json.dumps({"type": "error", "sessionID": session_id,
                      "error": {"name": "UnknownError",
                                "data": {"message": "Unexpected server error. Check server logs for details."}}}))
    handle_hook({**base, "event": "session.idle"}, env=env, agent="opencode")
    return 1


def run_api_error(ws: Path, session_id: str, prompt: str) -> int:
    """Reproduce the v2 live failure exactly: OpenCode starts the session and
    submits the prompt, the model call fails (OpenRouter 402 'Insufficient
    credits'), and OpenCode EXITS 0 having made no tool calls and no file
    changes. The plugin still captures a task-only, in-progress Shard (session
    created, prompt submitted, idle -- never a tool call, edit, or delete)."""
    from openshard.adapters.claude_hooks import handle_hook

    env = {"CLAUDE_PROJECT_DIR": str(ws), "OPENSHARD_CAPTURE_DISABLE": "1"}
    base = {"session_id": session_id, "directory": str(ws)}
    handle_hook({**base, "event": "session.created"}, env=env, agent="opencode")
    handle_hook({**base, "event": "chat.message", "prompt": prompt}, env=env, agent="opencode")
    print(json.dumps({"type": "error", "sessionID": session_id,
                      "error": {"name": "APIError", "data": {
                          "message": "Insufficient credits. Add more using https://openrouter.ai/settings/credits",
                          "statusCode": 402, "isRetryable": False}}}))
    handle_hook({**base, "event": "session.idle"}, env=env, agent="opencode")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"] or argv == ["-v"]:
        print("9.9.9 (fake OpenCode for PR13 tests)")
        return 0
    parsed = parse_argv(argv)
    ws = Path(parsed["dir"]) if parsed["dir"] else Path.cwd()
    model = parsed["model"] or "fake-provider/fake-model"
    mode = os.environ.get("PR13_FAKE_OPENCODE_MODE", "naive")
    session_id = "ses_" + uuid.uuid4().hex[:20]

    # Real OpenCode requires "provider/model"; anything else is rejected
    # before any work happens. Keep that contract so the harness can never
    # again hand OpenCode a Claude Code alias without the test suite noticing.
    if "/" not in model or mode == "fail_model":
        return fail_unknown_model(ws, session_id, parsed["prompt"] or "", model)
    # v2 failure mode: the CLI accepts the model and exits 0, but the model
    # call itself fails mid-session (e.g. no provider credits), so no work
    # happens.
    if mode == "api_error":
        return run_api_error(ws, session_id, parsed["prompt"] or "")

    print(f'{{"type":"session.started","session_id":"{session_id}"}}')

    if mode == "naive":
        files = naive_tags_edit(ws)
    elif mode == "correct":
        files = correct_tags_edit(ws)
    else:
        files = []

    deliver_opencode_hooks(ws, session_id, parsed["prompt"] or "", files, model)

    print(f'{{"type":"session.idle","session_id":"{session_id}"}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
