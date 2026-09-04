"""Real Claude Code CLI execution and ``stream-json`` transcript parsing.

The coding agent is the installed ``claude`` binary run non-interactively
(``claude -p``) with an explicit model, an explicit MCP configuration and a
hard wall-clock timeout. Its NDJSON output is captured to disk unmodified
and parsed for the facts the benchmark reports: the model Claude actually
used, the session id, which MCP servers connected, which tools were
called (including ``mcp__openshard__*``), the final result status, and
Claude's own turn/cost/token accounting. Thinking blocks are never read.

Everything the benchmark decides about the agent's environment is here, so
it is applied identically to every arm:

* ``--strict-mcp-config --mcp-config <file>``: the agent sees exactly the
  MCP servers the arm's file declares -- none for control, only OpenShard
  for treatment -- and never the machine's own user/project MCP servers.
* ``--setting-sources project,local``: the machine's user-level settings
  (model overrides, permissions, hooks) are not loaded.
* ``--disable-slash-commands``: no skills from the machine.
* ``--no-session-persistence``: nothing is written to the user's sessions.
* A scrubbed environment: no inherited ``CLAUDE*`` session variables
  (this benchmark may itself be launched from inside Claude Code) and no
  inherited ``OPENSHARD_*`` knobs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from evals.pr13.benchmark.errors import BenchmarkError

HARNESS_NAME = "claude_code_cli"
OPENSHARD_TOOL_PREFIX = "mcp__openshard__"
_ENV_PREFIXES_SCRUBBED = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID", "CLAUDE_AGENT_", "OPENSHARD_")
_ENV_KEPT = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})  # auth, not session state
_FINAL_TEXT_CAP = 4000
_COMMAND_CAP = 300


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class HarnessConfig:
    """Everything that shapes one ``claude -p`` invocation. Identical across arms."""

    claude_argv: tuple[str, ...]
    model: str
    max_turns: int | None
    timeout_seconds: float
    max_budget_usd: float | None = None
    permission_flag: str = "--dangerously-skip-permissions"
    setting_sources: str | None = "project,local"
    disable_skills: bool = True
    no_session_persistence: bool = True
    extra_args: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness": HARNESS_NAME,
            "claude_argv": list(self.claude_argv),
            "model": self.model,
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "max_budget_usd": self.max_budget_usd,
            "permission_flag": self.permission_flag,
            "setting_sources": self.setting_sources,
            "disable_skills": self.disable_skills,
            "no_session_persistence": self.no_session_persistence,
            "extra_args": list(self.extra_args),
        }


def build_argv(cfg: HarnessConfig, mcp_config_path: Path) -> list[str]:
    """The exact command line for one run; the prompt itself goes in on stdin."""
    argv = [*cfg.claude_argv, "-p", "--output-format", "stream-json", "--verbose", "--model", cfg.model]
    argv += ["--strict-mcp-config", "--mcp-config", str(mcp_config_path)]
    if cfg.permission_flag:
        argv.append(cfg.permission_flag)
    if cfg.max_turns is not None:
        argv += ["--max-turns", str(cfg.max_turns)]
    if cfg.max_budget_usd is not None:
        argv += ["--max-budget-usd", str(cfg.max_budget_usd)]
    if cfg.setting_sources:
        argv += ["--setting-sources", cfg.setting_sources]
    if cfg.disable_skills:
        argv.append("--disable-slash-commands")
    if cfg.no_session_persistence:
        argv.append("--no-session-persistence")
    argv += list(cfg.extra_args)
    return argv


def scrubbed_env(
    base: dict[str, str] | os._Environ[str] | None = None,
    *,
    path_prepend: list[str] | None = None,
    overrides: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return ``(env, removed_names)``: the agent's environment and what was dropped from it."""
    source = dict(os.environ if base is None else base)
    env: dict[str, str] = {}
    removed: list[str] = []
    for key, value in source.items():
        upper = key.upper()
        if upper in _ENV_KEPT:
            env[key] = value
            continue
        if any(upper.startswith(p) for p in _ENV_PREFIXES_SCRUBBED):
            removed.append(key)
            continue
        env[key] = value
    if path_prepend:
        existing = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([*path_prepend, existing]) if existing else os.pathsep.join(path_prepend)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if overrides:
        env.update(overrides)
    return env, sorted(removed)


def write_mcp_config(path: Path, servers: dict[str, dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# stream-json parsing
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    name: str
    tool_use_id: str | None
    input_summary: str | None  # Bash command / file path / MCP task text, bounded
    result_is_error: bool | None = None
    result_text: str | None = None  # only kept for OpenShard tools (bounded)


@dataclass
class ParsedStream:
    lines_total: int = 0
    lines_unparsed: int = 0
    session_id: str | None = None
    claude_version: str | None = None
    model_init: str | None = None
    models_observed: list[str] = field(default_factory=list)
    tools_available: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    permission_mode: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    result_subtype: str | None = None
    result_is_error: bool | None = None
    result_text: str | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    model_usage: dict[str, Any] | None = None
    permission_denials: int | None = None
    saw_result: bool = False

    @property
    def openshard_calls(self) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name.startswith(OPENSHARD_TOOL_PREFIX)]

    def tool_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.tool_calls:
            counts[call.name] = counts.get(call.name, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines_total": self.lines_total,
            "lines_unparsed": self.lines_unparsed,
            "session_id": self.session_id,
            "claude_version": self.claude_version,
            "model_init": self.model_init,
            "models_observed": list(self.models_observed),
            "tools_available": list(self.tools_available),
            "mcp_servers": list(self.mcp_servers),
            "permission_mode": self.permission_mode,
            "tool_calls_total": len(self.tool_calls),
            "tool_calls_by_name": self.tool_counts(),
            "tool_calls": [
                {
                    "name": c.name, "input_summary": c.input_summary,
                    "result_is_error": c.result_is_error,
                    **({"result_text": c.result_text} if c.result_text is not None else {}),
                }
                for c in self.tool_calls
            ],
            "result_subtype": self.result_subtype,
            "result_is_error": self.result_is_error,
            "result_text": self.result_text,
            "num_turns": self.num_turns,
            "duration_ms": self.duration_ms,
            "duration_api_ms": self.duration_api_ms,
            "total_cost_usd": self.total_cost_usd,
            "usage": self.usage,
            "model_usage": self.model_usage,
            "permission_denials": self.permission_denials,
            "saw_result": self.saw_result,
        }


def _bounded(text: object, cap: int) -> str | None:
    if not isinstance(text, str):
        return None
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _tool_input_summary(name: str, tool_input: object) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    if name.startswith(OPENSHARD_TOOL_PREFIX):
        for key in ("task", "query", "shard_id", "run_id"):
            if isinstance(tool_input.get(key), str):
                return _bounded(f"{key}={tool_input[key]}", _COMMAND_CAP)
        return None
    for key in ("command", "file_path", "path", "pattern", "notebook_path"):
        if isinstance(tool_input.get(key), str):
            return _bounded(tool_input[key], _COMMAND_CAP)
    return None


def _result_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(b["text"]) for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return "\n".join(parts)
    return ""


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def parse_stream(lines: list[str]) -> ParsedStream:
    """Parse ``claude -p --output-format stream-json`` output. Never raises.

    Unknown or malformed lines are counted, not guessed at. Assistant
    ``thinking`` blocks are skipped entirely: the benchmark never inspects
    chain-of-thought.
    """
    parsed = ParsedStream()
    by_id: dict[str, ToolCall] = {}
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        parsed.lines_total += 1
        try:
            event = json.loads(stripped)
        except ValueError:
            parsed.lines_unparsed += 1
            continue
        if not isinstance(event, dict):
            parsed.lines_unparsed += 1
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            parsed.session_id = event.get("session_id") if isinstance(event.get("session_id"), str) else parsed.session_id
            parsed.model_init = event.get("model") if isinstance(event.get("model"), str) else None
            parsed.claude_version = (
                event.get("claude_code_version") if isinstance(event.get("claude_code_version"), str) else None
            )
            parsed.permission_mode = event.get("permissionMode") if isinstance(event.get("permissionMode"), str) else None
            tools = event.get("tools")
            parsed.tools_available = [t for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
            servers = event.get("mcp_servers")
            parsed.mcp_servers = [
                {"name": s.get("name"), "status": s.get("status")}
                for s in servers if isinstance(s, dict)
            ] if isinstance(servers, list) else []
        elif kind == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                message = {}
            model = message.get("model")
            if isinstance(model, str) and model not in parsed.models_observed:
                parsed.models_observed.append(model)
            if parsed.session_id is None and isinstance(event.get("session_id"), str):
                parsed.session_id = event["session_id"]
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue  # text and thinking blocks are not read
                name = block.get("name")
                if not isinstance(name, str):
                    continue
                call = ToolCall(
                    name=name,
                    tool_use_id=block.get("id") if isinstance(block.get("id"), str) else None,
                    input_summary=_tool_input_summary(name, block.get("input")),
                )
                parsed.tool_calls.append(call)
                if call.tool_use_id:
                    by_id[call.tool_use_id] = call
        elif kind == "user":
            message = event.get("message")
            if not isinstance(message, dict):
                message = {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                use_id = block.get("tool_use_id")
                target = by_id.get(use_id) if isinstance(use_id, str) else None
                if target is None:
                    continue
                target.result_is_error = bool(block.get("is_error")) if "is_error" in block else None
                if target.name.startswith(OPENSHARD_TOOL_PREFIX):
                    target.result_text = _bounded(_result_content_text(block.get("content")), _FINAL_TEXT_CAP)
        elif kind == "result":
            parsed.saw_result = True
            parsed.result_subtype = event.get("subtype") if isinstance(event.get("subtype"), str) else None
            parsed.result_is_error = bool(event.get("is_error")) if "is_error" in event else None
            parsed.result_text = _bounded(event.get("result"), _FINAL_TEXT_CAP)
            parsed.num_turns = _int_or_none(event.get("num_turns"))
            parsed.duration_ms = _int_or_none(event.get("duration_ms"))
            parsed.duration_api_ms = _int_or_none(event.get("duration_api_ms"))
            parsed.total_cost_usd = _float_or_none(event.get("total_cost_usd"))
            parsed.usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
            parsed.model_usage = event.get("modelUsage") if isinstance(event.get("modelUsage"), dict) else None
            denials = event.get("permission_denials")
            parsed.permission_denials = len(denials) if isinstance(denials, list) else None
            if parsed.session_id is None and isinstance(event.get("session_id"), str):
                parsed.session_id = event["session_id"]
    return parsed


# ---------------------------------------------------------------------------
# Process execution
# ---------------------------------------------------------------------------


@dataclass
class AgentRun:
    argv: list[str]
    cwd: str
    started_at: str
    ended_at: str
    wall_clock_seconds: float
    exit_code: int | None
    timed_out: bool
    launch_error: str | None
    stdout_path: str
    stderr_path: str
    env_removed: list[str]
    parsed: ParsedStream

    @property
    def agent_reported_completion(self) -> bool | None:
        """True/False from Claude's own final ``result`` event; None when there was none."""
        if not self.parsed.saw_result:
            return None
        return self.parsed.result_subtype == "success" and not self.parsed.result_is_error

    @property
    def exit_status(self) -> str:
        if self.launch_error:
            return "launch_failed"
        if self.timed_out:
            return "timeout"
        if self.exit_code == 0 and self.parsed.saw_result:
            return "exited_0"
        if self.exit_code == 0:
            return "exited_0_no_result"
        return f"exited_{self.exit_code}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness": HARNESS_NAME,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "wall_clock_seconds": self.wall_clock_seconds,
            "exit_code": self.exit_code,
            "exit_status": self.exit_status,
            "timed_out": self.timed_out,
            "launch_error": self.launch_error,
            "agent_reported_completion": self.agent_reported_completion,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "env_removed": list(self.env_removed),
            "stream": self.parsed.to_dict(),
        }


def _pump(stream: IO[bytes], sink_path: Path, collected: list[str]) -> None:
    with sink_path.open("wb") as sink:
        for chunk in iter(stream.readline, b""):
            sink.write(chunk)
            sink.flush()
            collected.append(chunk.decode("utf-8", "replace"))


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill *proc* and its children (Claude spawns MCP servers and shells)."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=30)
    except Exception:
        pass


def run_agent(
    cfg: HarnessConfig,
    *,
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    env_removed: list[str],
    mcp_config_path: Path,
    out_dir: Path,
) -> AgentRun:
    """Run one non-interactive Claude Code session in *cwd*. Never raises for agent failures.

    A failure to *launch* the CLI (missing binary) is a benchmark failure
    and raises ``BenchmarkError("claude_launch_failed")``; anything the
    agent itself does (non-zero exit, timeout, no result event) is recorded
    faithfully in the returned ``AgentRun``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "agent_stdout.jsonl"
    stderr_path = out_dir / "agent_stderr.txt"
    (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    argv = build_argv(cfg, mcp_config_path)
    (out_dir / "argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")

    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd), "env": env, "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    started_at = utc_now()
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise BenchmarkError(
            "claude_launch_failed", f"could not launch the coding agent: {exc}", details={"argv": argv},
        ) from exc

    out_lines: list[str] = []
    err_lines: list[str] = []
    assert proc.stdout is not None and proc.stderr is not None and proc.stdin is not None
    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, stdout_path, out_lines), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, stderr_path, err_lines), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    except OSError:
        pass

    timed_out = False
    exit_code: int | None
    try:
        exit_code = proc.wait(timeout=cfg.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc)
        exit_code = proc.poll()
    for t in threads:
        t.join(timeout=30)
    ended_at = utc_now()
    wall = round(time.monotonic() - t0, 3)

    parsed = parse_stream(out_lines)
    return AgentRun(
        argv=argv, cwd=str(cwd), started_at=started_at, ended_at=ended_at, wall_clock_seconds=wall,
        exit_code=exit_code, timed_out=timed_out, launch_error=None,
        stdout_path=str(stdout_path), stderr_path=str(stderr_path), env_removed=list(env_removed), parsed=parsed,
    )
