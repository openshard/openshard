"""Exercise the built distribution in a clean venv outside the source checkout."""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="openshard-wheel-smoke-") as temp:
        root = Path(temp)
        venv.EnvBuilder(with_pip=True).create(root / "venv")
        bindir = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        cli = bindir / ("openshard.exe" if os.name == "nt" else "openshard")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        subprocess.run([str(python), "-m", "pip", "install", str(wheel)], cwd=root, env=env, check=True)
        actual = subprocess.check_output([str(python), "-c", "import importlib.metadata; print(importlib.metadata.version('openshard'))"], cwd=root, env=env, text=True).strip()
        if actual != args.version:
            raise SystemExit(f"Installed version {actual!r} differs from {args.version!r}")
        for command in (("remote",), ("remote", "create"), ("remote", "attach"), ("workflow", "timeline"), ("verify",), ("learn", "install"), ("learn", "hook"), ("learn", "uninstall")):
            subprocess.run([str(cli), *command, "--help"], cwd=root, env=env, check=True)
        help_text = subprocess.check_output([str(cli), "learn", "install", "--help"], cwd=root, env=env, text=True)
        if "--hosted" not in help_text:
            raise SystemExit("Installed learning hook is missing --hosted")
        for agent in ("claude", "codex"):
            output = subprocess.check_output([str(cli), "learn", "hook", agent, "--hosted"], input="{}", cwd=root, env=env, text=True)
            if output.strip() != "{}":
                raise SystemExit("Installed hosted hook did not fail open for empty input")
        print(f"Built wheel smoke passed: {actual}; remote, workflow, verify, local and hosted learning hooks")


if __name__ == "__main__":
    main()
