"""Repeatable benchmark for the OpenShard MCP server: stdio startup + one
representative tool call (``recent_shards``).

Used to compare the MCP SDK v1 -> v2 migration (PR13.5) against a baseline:
run once before the dependency bump (``pip install 'mcp>=1.2,<2'``), once
after (``pip install 'mcp>=2.0,<3'``), and diff the two JSON outputs.

    python evals/mcp_sdk_bench.py --runs 5 --out /tmp/mcp_bench_v2.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path


async def _one_run(repo_path: Path) -> dict[str, float]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "openshard.cli.entrypoint", "mcp", "serve", "--repo-path", str(repo_path)],
    )
    t0 = time.perf_counter()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            t1 = time.perf_counter()
            await session.call_tool("recent_shards", {"limit": 5})
            t2 = time.perf_counter()
    return {"startup_seconds": t1 - t0, "tool_call_seconds": t2 - t1}


def run_benchmark(runs: int, repo_path: Path) -> dict[str, object]:
    samples = [asyncio.run(_one_run(repo_path)) for _ in range(runs)]
    startup = [s["startup_seconds"] for s in samples]
    tool_call = [s["tool_call_seconds"] for s in samples]

    import mcp as mcp_pkg

    return {
        "mcp_version": getattr(mcp_pkg, "__version__", None) or "unknown",
        "runs": runs,
        "samples": samples,
        "startup_seconds": {"median": statistics.median(startup), "mean": statistics.mean(startup)},
        "tool_call_seconds": {"median": statistics.median(tool_call), "mean": statistics.mean(tool_call)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="Number of stdio round trips to sample.")
    parser.add_argument("--repo-path", type=Path, default=Path.cwd(), help="Repository to point the server at.")
    parser.add_argument("--out", type=Path, default=None, help="Optional path to write the JSON result to.")
    args = parser.parse_args(argv)

    result = run_benchmark(args.runs, args.repo_path)
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
