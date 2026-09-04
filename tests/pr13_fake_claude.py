"""A stand-in for the Claude Code CLI used only by tests/test_pr13_benchmark.py.

It speaks just enough of ``claude -p --output-format stream-json`` for the
benchmark harness: a ``system/init`` event (reporting the MCP servers named
in ``--mcp-config``), a few ``assistant``/``user`` tool events, and a final
``result`` event. It edits the workspace the way a real agent would, and
in burn-in mode it delivers Claude Code hook payloads to OpenShard's
*production* hook adapter (``handle_claude_hook``) exactly as Claude Code
would, so the treatment history the tests check is produced by the real
capture code path -- the benchmark itself never writes history.

Behaviour is selected with environment variables set by the test:

* ``PR13_FAKE_MODE``: ``naive`` (hand-edit relay/_schema.py), ``correct``
  (edit schema/jobs.json and regenerate), ``noop``, ``sleep`` (hang for
  ``PR13_FAKE_SLEEP`` seconds), ``crash`` (exit 3 with no result event),
  ``mcp`` (like ``correct`` but first calls the OpenShard MCP tool and
  echoes ``relevant_context`` from the workspace's own history).
* ``PR13_FAKE_MODE_TREATMENT`` / ``PR13_FAKE_MODE_CONTROL`` /
  ``PR13_FAKE_MODE_BURN_IN`` override the mode per stage; the stage is
  inferred from the invocation (hooks installed -> burn-in; openshard in
  the MCP config -> treatment; otherwise control).
* ``PR13_FAKE_SIMULATE_HOOKS=1``: in burn-in mode, deliver hook payloads.
* ``PR13_FAKE_MODEL``: model id to report (default ``fake-model-1``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

MODEL = os.environ.get("PR13_FAKE_MODEL", "fake-model-1")


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def parse_argv(argv: list[str]) -> dict:
    out: dict = {"mcp_config": None, "model": None, "max_turns": None, "flags": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mcp-config":
            out["mcp_config"] = argv[i + 1]
            i += 2
        elif a == "--model":
            out["model"] = argv[i + 1]
            i += 2
        elif a == "--max-turns":
            out["max_turns"] = int(argv[i + 1])
            i += 2
        elif a in ("--output-format", "--setting-sources", "--max-budget-usd"):
            out["flags"].append((a, argv[i + 1]))
            i += 2
        else:
            out["flags"].append((a, None))
            i += 1
    return out


MCP_TOOL_NAMES = ("recent_shards", "get_shard", "get_receipt", "search_history", "relevant_context")


def mcp_servers(config_path: str | None) -> list[dict]:
    if not config_path:
        return []
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    return [{"name": name, "status": "connected",
             "placebo": any(str(a).endswith("placebo_mcp.py") for a in (cfg.get("args") or []))}
            for name, cfg in servers.items()]


def stage(ws: Path, servers: list[dict]) -> str:
    if (ws / ".claude" / "settings.local.json").exists():
        return "burn_in"
    if not servers:
        # No hooks, no MCP server at all: only a claude_wrap_chain burn-in
        # stage looks like this (an arm always configures at least the
        # placebo or the production server; ordinary hook-based burn-in
        # always has .claude/ by the time the agent runs).
        return "wrap_chain_stage"
    openshard = next((s for s in servers if s["name"] == "openshard"), None)
    if openshard is None or openshard["placebo"]:
        return "control"
    return "treatment"


def mode_for(stage_name: str) -> str:
    specific = os.environ.get(f"PR13_FAKE_MODE_{stage_name.upper()}")
    return specific or os.environ.get("PR13_FAKE_MODE", "noop")


# --------------------------------------------------------------------------- edits


def edit_queue_ordering(ws: Path) -> None:
    path = ws / "relay" / "queue.py"
    text = path.read_text(encoding="utf-8")
    old = ('    def ordered(self) -> list[Job]:\n        """Jobs in the order they should run: insertion order."""\n'
           '        return self.load()\n')
    new = ('    def ordered(self) -> list[Job]:\n        """Jobs in the order they should run: priority first, then insertion."""\n'
           '        return sorted(self.load(), key=lambda job: -job.priority)\n')
    assert old in text, "seed queue.py changed; update the fake"
    path.write_text(text.replace(old, new), encoding="utf-8")


_PURGE_PARSER_MARK = '    remove = commands.add_parser("remove", help="delete a job")\n    remove.add_argument("name")\n'
_PURGE_DISPATCH_MARK = '        elif args.subcommand == "remove":\n            queue.remove(args.name)\n'


def purge_registered(ws: Path) -> bool:
    return '"purge"' in (ws / "relay" / "cli.py").read_text(encoding="utf-8")


def naive_purge_edit(ws: Path) -> list[str]:
    """Wrap-chain stage 1: add `relay purge`, writing the file directly (buggy)."""
    cli_path = ws / "relay" / "cli.py"
    text = cli_path.read_text(encoding="utf-8")
    assert _PURGE_PARSER_MARK in text, "seed cli.py changed; update the fake"
    text = text.replace(_PURGE_PARSER_MARK, _PURGE_PARSER_MARK + '\n    commands.add_parser("purge", help="remove noop jobs")\n')
    assert _PURGE_DISPATCH_MARK in text
    addition = (
        '        elif args.subcommand == "purge":\n'
        '            jobs = [j for j in queue.load() if j.command != "noop"]\n'
        '            with open(args.queue, "w", encoding="utf-8") as fh:\n'
        '                for j in jobs:\n'
        '                    fh.write(f"{j.name}\\t{j.command}\\t{j.retries}\\n")\n'
    )
    text = text.replace(_PURGE_DISPATCH_MARK, _PURGE_DISPATCH_MARK + addition)
    cli_path.write_text(text, encoding="utf-8")
    return ["relay/cli.py"]


def fix_purge_edit(ws: Path) -> list[str]:
    """Wrap-chain stage 2: fix `relay purge` to write through QueueFile.save()."""
    cli_path = ws / "relay" / "cli.py"
    text = cli_path.read_text(encoding="utf-8")
    buggy = (
        '        elif args.subcommand == "purge":\n'
        '            jobs = [j for j in queue.load() if j.command != "noop"]\n'
        '            with open(args.queue, "w", encoding="utf-8") as fh:\n'
        '                for j in jobs:\n'
        '                    fh.write(f"{j.name}\\t{j.command}\\t{j.retries}\\n")\n'
    )
    assert buggy in text, "stage 1's edit is not present; the fake wrap-chain stages ran out of order"
    fixed = (
        '        elif args.subcommand == "purge":\n'
        '            jobs = [j for j in queue.load() if j.command != "noop"]\n'
        '            queue.save(jobs)\n'
    )
    cli_path.write_text(text.replace(buggy, fixed), encoding="utf-8")
    return ["relay/cli.py"]


def reset_retries_edit(ws: Path) -> list[str]:
    """Scenario 2's evaluation task: `relay reset-retries`, correctly routed
    through QueueFile.save() (the same discipline the wrap-chain burn-in's
    stage 2 fix demonstrated for a different command)."""
    cli_path = ws / "relay" / "cli.py"
    text = cli_path.read_text(encoding="utf-8")
    assert _PURGE_PARSER_MARK in text, "seed cli.py changed; update the fake"
    text = text.replace(
        _PURGE_PARSER_MARK,
        _PURGE_PARSER_MARK + '\n    commands.add_parser("reset-retries", help="zero every job\'s retries")\n',
    )
    assert _PURGE_DISPATCH_MARK in text
    addition = (
        '        elif args.subcommand == "reset-retries":\n'
        '            jobs = [Job(**{**j.to_dict(), "retries": 0}) for j in queue.load()]\n'
        '            queue.save(jobs)\n'
    )
    cli_path.write_text(text.replace(_PURGE_DISPATCH_MARK, _PURGE_DISPATCH_MARK + addition), encoding="utf-8")
    return ["relay/cli.py"]


def readme_license_edit(ws: Path) -> list[str]:
    """Scenario 6's burn-in: a docs-only change, no code file touched."""
    path = ws / "README.md"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines.insert(2, "License: Apache-2.0\n\n")
    path.write_text("".join(lines), encoding="utf-8")
    return ["README.md"]


_WATCH_DISPATCH = (
    '        elif args.subcommand == "watch":\n'
    '            import os as _os\n'
    '            import time as _time\n'
    '            last_mtime = None\n'
    '            while True:\n'
    '                try:\n'
    '                    mtime = _os.path.getmtime(args.queue)\n'
    '                except OSError:\n'
    '                    mtime = None\n'
    '                if mtime != last_mtime:\n'
    '                    last_mtime = mtime\n'
    '                    for job in queue.ordered():\n'
    '                        print(format_row(job))\n'
    '                    sys.stdout.flush()\n'
    '                _time.sleep(0.15)\n'
)


def watch_edit(ws: Path) -> list[str]:
    """Scenario 6's evaluation task: a correct, polling `relay watch`."""
    cli_path = ws / "relay" / "cli.py"
    text = cli_path.read_text(encoding="utf-8")
    assert _PURGE_PARSER_MARK in text and _PURGE_DISPATCH_MARK in text, "seed cli.py changed; update the fake"
    text = text.replace(
        _PURGE_PARSER_MARK,
        _PURGE_PARSER_MARK + '\n    commands.add_parser("watch", help="poll the queue file for changes")\n',
    )
    text = text.replace(_PURGE_DISPATCH_MARK, _PURGE_DISPATCH_MARK + _WATCH_DISPATCH)
    cli_path.write_text(text, encoding="utf-8")
    return ["relay/cli.py"]


def run_wrap_chain_stage(ws: Path) -> list[str]:
    """State-based stage detection: `openshard wrap claude` passes no stage
    indicator of its own, so this decides purely from what the workspace
    already looks like -- exactly the information a real agent would use.

    Two env overrides exist only so tests can exercise the runner's
    precondition-failure paths: PR13_FAKE_WRAP_STAGE1_CORRECT makes the
    first stage implement purge correctly from the start (so no known
    failed approach is ever produced); PR13_FAKE_WRAP_STAGE2_NOOP makes
    the second stage leave stage 1's bug in place (so the final state
    never recovers). Neither is read by the state-based path above.
    """
    if not purge_registered(ws):
        if os.environ.get("PR13_FAKE_WRAP_STAGE1_CORRECT") == "1":
            cli_path = ws / "relay" / "cli.py"
            text = cli_path.read_text(encoding="utf-8")
            text = text.replace(_PURGE_PARSER_MARK, _PURGE_PARSER_MARK + '\n    commands.add_parser("purge", help="remove noop jobs")\n')
            text = text.replace(
                _PURGE_DISPATCH_MARK,
                _PURGE_DISPATCH_MARK
                + '        elif args.subcommand == "purge":\n'
                  '            jobs = [j for j in queue.load() if j.command != "noop"]\n'
                  '            queue.save(jobs)\n',
            )
            cli_path.write_text(text, encoding="utf-8")
            return ["relay/cli.py"]
        return naive_purge_edit(ws)
    if os.environ.get("PR13_FAKE_WRAP_STAGE2_NOOP") == "1":
        return []
    return fix_purge_edit(ws)


def naive_edit(ws: Path) -> list[str]:
    schema = ws / "relay" / "_schema.py"
    text = schema.read_text(encoding="utf-8")
    text = text.replace(
        "    Field('retries', int, False, 0, 'How many times to retry the job after a failure.'),\n)",
        "    Field('retries', int, False, 0, 'How many times to retry the job after a failure.'),\n"
        "    Field('priority', int, False, 0, 'Higher priority jobs run first.'),\n)",
    )
    schema.write_text(text, encoding="utf-8")
    edit_queue_ordering(ws)
    return ["relay/_schema.py", "relay/queue.py"]


def correct_edit(ws: Path) -> list[str]:
    spec_path = ws / "schema" / "jobs.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["fields"].append({"name": "priority", "type": "int", "required": False, "default": 0,
                           "help": "Higher priority jobs run first."})
    spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "scripts/gen_schema.py"], cwd=ws, check=True, capture_output=True)
    edit_queue_ordering(ws)
    return ["schema/jobs.json", "relay/_schema.py", "relay/queue.py"]


# --------------------------------------------------------------------------- hooks


def deliver_hooks(ws: Path, session_id: str, prompt: str, files: list[str]) -> None:
    """Feed the production hook adapter exactly the payload shapes Claude Code sends."""
    from openshard.adapters.claude_hooks import handle_claude_hook

    env = {"CLAUDE_PROJECT_DIR": str(ws), "OPENSHARD_CAPTURE_DISABLE": "1"}
    base = {"session_id": session_id, "cwd": str(ws), "transcript_path": str(ws / "transcript.jsonl")}
    handle_claude_hook({**base, "hook_event_name": "SessionStart", "source": "startup"}, env=env)
    handle_claude_hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt}, env=env)
    for rel in files:
        handle_claude_hook({
            **base, "hook_event_name": "PostToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": str(ws / rel)}, "tool_response": {"filePath": str(ws / rel)},
            "tool_use_id": "toolu_" + uuid.uuid4().hex[:8],
        }, env=env)
    handle_claude_hook({
        **base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "python -m unittest discover -s tests"}, "tool_response": {"stdout": "OK"},
        "tool_use_id": "toolu_" + uuid.uuid4().hex[:8],
    }, env=env)
    handle_claude_hook({**base, "hook_event_name": "Stop", "stop_hook_active": False}, env=env)
    handle_claude_hook({**base, "hook_event_name": "SessionEnd", "reason": "prompt_input_exit"}, env=env)


# --------------------------------------------------------------------------- MCP


def call_relevant_context(config_path: str | None, ws: Path, prompt: str) -> str:
    """Spawn the configured ``openshard`` MCP server over stdio and call relevant_context."""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    data = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {}
    cfg = (data.get("mcpServers") or {}).get("openshard")
    if not cfg:
        return json.dumps({"error": "no openshard MCP server configured"})

    async def run() -> str:
        params = StdioServerParameters(command=cfg["command"], args=list(cfg.get("args") or []),
                                       env=dict(os.environ), cwd=str(ws))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("relevant_context", {"task": prompt[:200]})
                return "\n".join(getattr(c, "text", "") for c in result.content)

    return asyncio.run(asyncio.wait_for(run(), timeout=90))


# --------------------------------------------------------------------------- main


def tool_use(session_id: str, name: str, tool_input: dict) -> str:
    tid = "toolu_" + uuid.uuid4().hex[:10]
    emit({"type": "assistant", "session_id": session_id,
          "message": {"role": "assistant", "model": MODEL,
                      "content": [{"type": "thinking", "thinking": "MUST NEVER BE RECORDED"},
                                  {"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}})
    return tid


def tool_result(session_id: str, tid: str, content: str, *, is_error: bool = False) -> None:
    emit({"type": "user", "session_id": session_id,
          "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                    "content": [{"type": "text", "text": content}],
                                                    "is_error": is_error}]}})


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("9.9.9 (fake Claude Code for PR13 tests)")
        return 0
    parsed = parse_argv(argv)
    prompt = sys.stdin.read()
    ws = Path.cwd()
    servers = mcp_servers(parsed["mcp_config"])
    stage_name = stage(ws, servers)
    mode = mode_for(stage_name)
    if stage_name == "wrap_chain_stage" and mode == "noop":
        # No explicit PR13_FAKE_MODE(_WRAP_CHAIN_STAGE) was requested for
        # this (hooks-less, MCP-less) invocation: it's a claude_wrap_chain
        # burn-in stage, so fall back to the state-based dispatch rather
        # than doing nothing. A test that DOES want sleep/crash/etc. here
        # still gets it, since mode would then not be "noop".
        mode = "wrap_chain"
    session_id = str(uuid.uuid4())
    started = time.monotonic()

    if mode == "crash":
        sys.stderr.write("fake claude: crashing before init\n")
        return 3

    emit({"type": "system", "subtype": "init", "session_id": session_id, "cwd": str(ws), "model": parsed["model"],
          "claude_code_version": "9.9.9", "permissionMode": "bypassPermissions",
          "tools": ["Bash", "Edit", "Read", "Write"]
          + [f"mcp__{s['name']}__{tool}" for s in servers for tool in MCP_TOOL_NAMES],
          "mcp_servers": [{"name": s["name"], "status": s["status"]} for s in servers]})

    if mode == "sleep":
        time.sleep(float(os.environ.get("PR13_FAKE_SLEEP", "30")))
        return 0

    files: list[str] = []
    if mode == "mcp":
        # Ask the arm's *configured* server (placebo or production) over MCP,
        # exactly as Claude Code would, and echo the answer as a tool result.
        tid = tool_use(session_id, "mcp__openshard__relevant_context", {"task": prompt[:200]})
        tool_result(session_id, tid, call_relevant_context(parsed["mcp_config"], ws, prompt))
        mode = "correct"

    if mode == "naive":
        files = naive_edit(ws)
    elif mode == "correct":
        files = correct_edit(ws)
    elif mode == "wrap_chain":
        files = run_wrap_chain_stage(ws)
    elif mode == "reset_retries":
        files = reset_retries_edit(ws)
    elif mode == "tags_naive":
        from pr13_fake_opencode import naive_tags_edit

        files = naive_tags_edit(ws)
    elif mode == "tags_correct":
        from pr13_fake_opencode import correct_tags_edit

        files = correct_tags_edit(ws)
    elif mode == "readme_license":
        files = readme_license_edit(ws)
    elif mode == "watch":
        files = watch_edit(ws)
    for rel in files:
        tid = tool_use(session_id, "Edit", {"file_path": str(ws / rel)})
        tool_result(session_id, tid, "ok")
    tid = tool_use(session_id, "Bash", {"command": "python -m unittest discover -s tests"})
    tool_result(session_id, tid, "OK")

    if stage_name == "burn_in" and os.environ.get("PR13_FAKE_SIMULATE_HOOKS") == "1":
        deliver_hooks(ws, session_id, prompt, files)

    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
          "duration_ms": int((time.monotonic() - started) * 1000), "duration_api_ms": 10, "num_turns": 3 + len(files),
          "result": f"Done ({mode}).", "total_cost_usd": 0.0123,
          "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0},
          "modelUsage": {MODEL: {"inputTokens": 100, "outputTokens": 50, "costUSD": 0.0123}},
          "permission_denials": []})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
