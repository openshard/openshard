"""Transport-level probe of an arm's MCP server: what tools does Claude actually get?

Claude Code's ``system/init`` event lists MCP tool *names* but loads their
schemas lazily (through ``ToolSearch``). To prove that control and
treatment expose the same tool surface, the benchmark also speaks MCP to
each arm's configured server itself -- the same command, arguments,
working directory and environment Claude will use -- and records the tool
names, descriptions and input schemas. Two servers with the same
fingerprint present Claude with the same tools; the init event's names
are checked as well, from Claude's side.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from evals.pr13.benchmark.errors import BenchmarkError
from openshard.adapters.claude_mcp_install import MCP_TOOLS

EXPECTED_TOOLS: tuple[str, ...] = MCP_TOOLS  # the production read-only surface, by name
KIND_PRODUCTION = "production"
KIND_PLACEBO = "placebo"


def surface_fingerprint(tools: list[dict[str, Any]]) -> str:
    """sha256 over (name, description, input schema) of every tool, order-independent."""
    canonical = json.dumps(
        sorted(({"name": t["name"], "description": t.get("description"), "input_schema": t.get("input_schema")}
                for t in tools), key=lambda t: str(t["name"])),
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _probe_async(command: str, args: list[str], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command, args=list(args), env=dict(env), cwd=str(cwd))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            tools = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in listed.tools
            ]
            server_name = init.server_info.name if init.server_info else None
            return {
                "server_name": server_name,
                "instructions": init.instructions,
                "tools": tools,
            }


def probe_stdio_server(
    command: str, args: list[str], *, cwd: Path, env: dict[str, str], timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Start the server exactly as configured, list its tools, shut it down. Raises ``BenchmarkError``."""

    async def runner() -> dict[str, Any]:
        return await asyncio.wait_for(_probe_async(command, args, cwd, env), timeout=timeout_seconds)

    try:
        result = asyncio.run(runner())
    except TimeoutError as exc:
        raise BenchmarkError(
            "mcp_server_unreachable", f"MCP server {command} {args} did not answer within {timeout_seconds}s",
        ) from exc
    except Exception as exc:
        raise BenchmarkError(
            "mcp_server_unreachable", f"MCP server {command} {args} could not be probed: {type(exc).__name__}: {exc}",
        ) from exc
    result["tool_names"] = sorted(t["name"] for t in result["tools"])
    result["fingerprint"] = surface_fingerprint(result["tools"])
    result["expected_tools"] = sorted(EXPECTED_TOOLS)
    result["matches_expected"] = result["tool_names"] == sorted(EXPECTED_TOOLS)
    return result


def require_expected_surface(probe: dict[str, Any], *, label: str) -> None:
    if not probe.get("matches_expected"):
        raise BenchmarkError(
            "mcp_surface_mismatch",
            f"{label}: MCP server exposes {probe.get('tool_names')}, expected {sorted(EXPECTED_TOOLS)}",
        )
    if probe.get("server_name") != "openshard":
        raise BenchmarkError(
            "mcp_surface_mismatch", f"{label}: MCP server is named {probe.get('server_name')!r}, expected 'openshard'",
        )


def require_same_surface(control: dict[str, Any], treatment: dict[str, Any]) -> None:
    if control["fingerprint"] != treatment["fingerprint"]:
        raise BenchmarkError(
            "mcp_surface_mismatch",
            "control (placebo) and treatment (production) MCP servers expose different tool surfaces; "
            "the arms would not have the same tools",
            details={"control": control["tools"], "treatment": treatment["tools"]},
        )
    if control.get("instructions") != treatment.get("instructions"):
        raise BenchmarkError(
            "mcp_surface_mismatch", "control and treatment MCP servers carry different server instructions",
        )
