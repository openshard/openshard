#!/usr/bin/env python3
"""Plan the PR test run from the set of changed files.

This is the brain of the *Fast PR Gate* in ``.github/workflows/ci.yml``. It
is deliberately a small, standard-library-only script so the ``plan`` job can
run it on the runner's stock ``python3`` before any dependency is installed.

Design (see docs/ci.md for the developer-facing description):

* Every changed path is classified. Documentation never triggers tests. CI
  infrastructure (this script, the workflows, ``pyproject.toml``, the shared
  pytest fixtures) and every path under the *sensitive* prefixes below --
  receipts/history persistence, capture, verification, auth/security, sync --
  escalate straight to the **full** PR suite, no matter how small the diff.
* Everything else is **targeted**: the curated invariant suite always runs,
  plus every test module that (transitively) imports a changed module. The
  dependency graph is built from real ``import`` statements (including those
  inside function bodies) *and* from dotted ``"openshard.x.y"`` string
  literals, so ``unittest.mock.patch("openshard.run.pipeline.foo")`` counts
  as a dependency too. Test modules that reference no repository module at
  all are always included -- they are cheap and we cannot prove them
  unaffected.
* Anything the classifier does not understand (a data file inside the
  package, a deleted module, a new kind of top-level file) escalates to full.
  Unknown means "run everything", never "run nothing".

The plan also says which of the serial capture/service test files to run and
whether the Windows smoke job is needed, and it splits the parallel-safe files
into balanced shards (by file size, a cheap and stable proxy for runtime).

Usage::

    python scripts/ci/select_tests.py plan --base <sha> --head <sha>
    python scripts/ci/select_tests.py plan --changed-files changed.txt --json
    python scripts/ci/select_tests.py explain tests/test_foo.py openshard/x.py
    python scripts/ci/select_tests.py check   # curated lists still resolve
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Curated lists. Edit these deliberately; ``check`` (and the unit tests) fail
# if any listed file stops existing, so a rename can never silently drop
# coverage.
# ---------------------------------------------------------------------------

# Tests that spin up a real background HTTP capture service (threads, an
# OS-assigned socket, in one case a spawned Node subprocess) and poll with
# real timeouts. Correct under xdist, but running many at once multiplies
# thread/socket/process contention, which is where the Windows flakes came
# from. They always run serially, in their own job, never on Windows in the
# PR gate, and never mixed into the parallel shards.
SERVICE_TEST_FILES: tuple[str, ...] = (
    "tests/test_antigravity_capture.py",
    "tests/test_claude_capture_service.py",
    "tests/test_cross_agent_capture.py",
    "tests/test_codex_capture.py",
    "tests/test_cursor_capture.py",
    "tests/test_hermes_capture.py",
    "tests/test_opencode_capture.py",
    "tests/test_v044_capture_auth.py",
    "tests/test_v044_evidence_loss.py",
)

# A small slice that exercises the code paths most likely to regress
# specifically on Windows (util/home.py + util/git.py CREATE_NO_WINDOW
# handling, jsonl_store's atomic replace, claude_hooks path/CRLF handling,
# sandbox diff/apply, repo_map's git subprocess calls, CLI entrypoints).
# None of these use the real capture service.
WINDOWS_SMOKE_TEST_FILES: tuple[str, ...] = (
    "tests/test_util_home_git.py",
    "tests/test_jsonl_store.py",
    "tests/test_claude_hooks.py",
    "tests/test_cli_claude_hooks.py",
    "tests/test_cli_doctor.py",
    "tests/test_cli_entrypoint.py",
    "tests/test_cli_repo_map.py",
    "tests/test_first_run_ux.py",
    "tests/test_repo_map.py",
    "tests/test_sandbox.py",
    "tests/test_sandbox_apply.py",
    "tests/test_sandbox_apply_receipts.py",
    "tests/test_sandbox_diff.py",
    "tests/test_adapters_wrap_exec.py",
)

# The invariant suite: evidence-critical guarantees that run on *every*
# non-docs PR regardless of what changed. Receipt schema/hash and identity,
# provenance, evidence filtering, history integrity, path/secret/shell
# safety, sync envelope integrity, verification contracts. Keep this list
# focused -- it is the floor, not the whole suite.
INVARIANT_TEST_FILES: tuple[str, ...] = (
    "tests/test_shard_schema.py",
    "tests/test_shard_hash.py",
    "tests/test_shard_contract.py",
    "tests/test_shard_identity.py",
    "tests/test_shard_proof_contract.py",
    "tests/test_v044_receipt_identity.py",
    "tests/test_v044_receipt_semantics.py",
    "tests/test_v044_change_attribution.py",
    "tests/test_v044_test_isolation.py",
    "tests/test_provenance.py",
    "tests/test_native_context_provenance.py",
    "tests/test_routing_truth_provenance.py",
    "tests/test_evidence_filter.py",
    "tests/test_verification_evidence.py",
    "tests/test_verification_contract_result.py",
    "tests/test_verification_v2.py",
    "tests/test_history_amend_integrity.py",
    "tests/test_jsonl_store.py",
    "tests/test_event_receipt_wiring.py",
    "tests/test_native_receipt.py",
    "tests/test_sandbox_apply_receipts.py",
    "tests/test_path_safety.py",
    "tests/test_secret_scan.py",
    "tests/test_presend_secret_guard.py",
    "tests/test_shell_policy.py",
    "tests/test_stack_guard.py",
    "tests/test_platform_sync.py",
    "tests/test_run_history.py",
)

# Any change under these prefixes runs the full PR suite. These are the
# evidence-critical areas: a one-line diff here can change what a Receipt
# hashes to, what gets captured, or what is sent off-machine.
SENSITIVE_PREFIXES: tuple[str, ...] = (
    "openshard/history/",       # Receipt/Shard schema, hash, persistence
    "openshard/adapters/",      # every capture adapter, hooks, capture auth
    "openshard/verification/",  # verification plans/execution/attestation
    "openshard/security/",      # path safety, secret scanning
    "openshard/safety/",        # output sanitisation
    "openshard/sync/",          # envelope, outbox, transport (off-machine)
    "openshard/telemetry/",     # off-machine, privacy-sensitive
    "openshard/mcp/",           # exposes history to agents
    "openshard/util/",          # home/git primitives everything persists through
    "openshard/native/sandbox_",  # sandbox diff/apply -> Receipt evidence
)

# CI infrastructure: a change here changes how tests run, so run them all.
CI_INFRA_PATHS: tuple[str, ...] = (
    ".github/",
    "scripts/ci/",
    "pyproject.toml",
    "tests/conftest.py",
    "tests/capture_fixtures.py",
    "tests/__init__.py",
)

# Paths that never affect any test.
DOCS_PATTERNS: tuple[str, ...] = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "NOTICE",
    "LICENSE",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "docs/*",
    "*.md",
    "*.png",
    "*.jpg",
    "*.svg",
)

# Python roots whose modules the dependency graph knows about. ``tests`` is
# included so ``from tests.capture_fixtures import ...`` resolves.
PY_ROOTS: tuple[str, ...] = ("openshard", "tests", "evals", "scripts", "demos", "examples")

# If a targeted plan would run more than this share of the parallel-safe
# files anyway, just run the full suite: simpler to reason about and the
# sharding balances better.
FULL_ESCALATION_RATIO = 0.6

DEFAULT_MAX_SHARDS = 3
# The serial capture-service files are split across at most this many
# runners. Each runner still executes its files strictly serially in one
# pytest process -- the serialisation that matters is *within* a machine
# (shared sockets, threads, subprocesses), and separate jobs are separate
# VMs. Measured: all nine files in one job take ~70s (338 tests, no single
# hot spot), two jobs take ~35s each.
DEFAULT_MAX_SERVICE_SHARDS = 2
# Shard balancing weights file size, a stable proxy for runtime, but a few
# files are dominated by real subprocesses (fake agents, `openshard`
# console-script runs) and take far longer than their size suggests. The
# `--durations` output of each shard job is where these come from; adjust
# when a shard is consistently the long pole.
SHARD_WEIGHTS: dict[str, float] = {
    "tests/test_pr13_scenarios_2to7.py": 14.0,  # ~36s of fake-agent subprocess runs
    "tests/test_pr13_benchmark.py": 10.0,       # ~28s
    "tests/test_evals.py": 4.0,                 # ~25s
    "tests/test_first_run_ux.py": 4.0,
    "tests/test_readonly_task.py": 3.0,
    "tests/test_home_screen.py": 3.0,
    "tests/test_cli_package.py": 3.0,
    "tests/test_platform_sync.py": 2.0,
}
# Test-source bytes per shard before another shard is worth its ~20s of job
# startup + install + collection. The whole parallel-safe suite is ~4.1 MB
# (Sept 2026), so a full run gets 3 shards, a half-size targeted run 2, and
# anything under ~1.5 MB runs as a single job.
BYTES_PER_SHARD = 1_500_000

_DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


# ---------------------------------------------------------------------------
# Module graph
# ---------------------------------------------------------------------------


def _iter_py_files(root: Path) -> list[Path]:
    """Tracked ``*.py`` files under PY_ROOTS.

    ``git ls-files`` is both much faster than walking the tree and exactly
    the set of files a CI checkout has; a directory that is not a git
    checkout (the unit tests' synthetic repo) falls back to a walk.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", *(f"{top}/*.py" for top in PY_ROOTS)],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        listed = ""
    if listed:
        return [root / rel for rel in listed.split("\x00") if rel]
    out: list[Path] = []
    for top in PY_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts or any(part.startswith(".") for part in p.parts):
                continue
            out.append(p)
    return out


def module_name_for(rel: str) -> str | None:
    """``openshard/history/shard.py`` -> ``openshard.history.shard``."""
    path = PurePosixPath(rel)
    if path.suffix != ".py" or not path.parts or path.parts[0] not in PY_ROOTS:
        return None
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@dataclass
class ModuleGraph:
    modules: dict[str, str] = field(default_factory=dict)      # module -> rel path
    imports: dict[str, set[str]] = field(default_factory=dict)  # module -> modules it uses
    dependents: dict[str, set[str]] = field(default_factory=dict)
    unresolved: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, root: Path | None = None) -> ModuleGraph:
        root = root or REPO_ROOT
        g = cls()
        for p in _iter_py_files(root):
            rel = p.relative_to(root).as_posix()
            name = module_name_for(rel)
            if name:
                g.modules[name] = rel
        for name, rel in g.modules.items():
            g.imports[name] = g._scan(root / rel, name)
        for src, targets in g.imports.items():
            for t in targets:
                g.dependents.setdefault(t, set()).add(src)
        return g

    # -- resolution ---------------------------------------------------------

    def resolve(self, dotted: str) -> str | None:
        """Longest known-module prefix of ``dotted`` (``a.b.c.func`` -> ``a.b.c``)."""
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in self.modules:
                return cand
        return None

    def _scan(self, path: Path, this_module: str) -> set[str]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
            self.unresolved.setdefault(this_module, set()).add(f"<unparseable: {exc}>")
            return set()
        is_package = self.modules.get(this_module, "").endswith("__init__.py")
        pkg = this_module if is_package else this_module.rpartition(".")[0]
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._add(found, alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    anchor = pkg.split(".") if pkg else []
                    anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                    base = ".".join([*anchor, base] if base else anchor)
                if base:
                    self._add(found, base)
                for alias in node.names:
                    if base and alias.name != "*":
                        self._add(found, f"{base}.{alias.name}", quiet=True)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value.strip()
                if _DOTTED_RE.match(s) and s.split(".")[0] in PY_ROOTS:
                    self._add(found, s, quiet=True)
        found.discard(this_module)
        return found

    def _add(self, found: set[str], dotted: str, *, quiet: bool = False) -> None:
        if dotted.split(".")[0] not in PY_ROOTS:
            return
        resolved = self.resolve(dotted)
        if resolved:
            found.add(resolved)

    # -- queries ------------------------------------------------------------

    def transitive_dependents(self, seeds: set[str]) -> set[str]:
        seen: set[str] = set()
        queue = deque(seeds)
        while queue:
            m = queue.popleft()
            for d in self.dependents.get(m, ()):
                if d not in seen:
                    seen.add(d)
                    queue.append(d)
        return seen

    def test_modules(self) -> dict[str, str]:
        return {
            m: rel
            for m, rel in self.modules.items()
            if rel.startswith("tests/") and PurePosixPath(rel).name.startswith("test_")
        }

    def unmapped_test_files(self) -> set[str]:
        """Test files that reference no repository module we can track."""
        out = set()
        for m, rel in self.test_modules().items():
            if not any(not t.startswith("tests.") for t in self.imports.get(m, ())):
                out.add(rel)
        return out


# ---------------------------------------------------------------------------
# Classification + planning
# ---------------------------------------------------------------------------


def classify(rel: str) -> str:
    """docs | ci | sensitive | test | code | other"""
    if any(fnmatch.fnmatchcase(rel, pat) for pat in DOCS_PATTERNS):
        return "docs"
    if any(rel == p or rel.startswith(p) for p in CI_INFRA_PATHS):
        return "ci"
    if any(rel.startswith(p) for p in SENSITIVE_PREFIXES):
        return "sensitive"
    if rel.startswith("tests/") and rel.endswith(".py"):
        return "test"
    if rel.endswith(".py") and module_name_for(rel):
        return "code"
    return "other"


@dataclass
class Plan:
    tier: str                      # docs | targeted | full
    reasons: list[str]
    fast_files: list[str]          # parallel-safe files to run in shards
    service_files: list[str]       # serial capture/service files to run
    windows: bool
    changed: list[str]
    affected_modules: list[str] = field(default_factory=list)

    @property
    def run_code(self) -> bool:
        return self.tier != "docs"

    def shards(self, max_shards: int = DEFAULT_MAX_SHARDS, root: Path | None = None) -> list[list[str]]:
        return split_shards(self.fast_files, max_shards, root)

    def service_shards(
        self, max_shards: int = DEFAULT_MAX_SERVICE_SHARDS, root: Path | None = None
    ) -> list[list[str]]:
        # Always split when there is more than one file: these are slow per
        # file, so a second runner pays for itself well below BYTES_PER_SHARD.
        files = sorted(set(self.service_files))
        if len(files) < 2 or max_shards < 2:
            return [files] if files else []
        return _balance(files, min(max_shards, len(files)), root)

    def to_dict(
        self,
        max_shards: int = DEFAULT_MAX_SHARDS,
        root: Path | None = None,
        max_service_shards: int = DEFAULT_MAX_SERVICE_SHARDS,
    ) -> dict:
        shards = self.shards(max_shards, root)
        service_shards = self.service_shards(max_service_shards, root)
        return {
            "tier": self.tier,
            "run_code": self.run_code,
            "reasons": self.reasons,
            "changed": self.changed,
            "fast_files": self.fast_files,
            "service_files": self.service_files,
            "windows": self.windows,
            "affected_modules": self.affected_modules,
            "shards": shards,
            # Ready-made `strategy.matrix` value for the shard job.
            "shard_matrix": _matrix(shards),
            "service_shards": service_shards,
            "service_matrix": _matrix(service_shards),
        }


def _matrix(shards: list[list[str]]) -> dict:
    return {
        "include": [
            {"id": i + 1, "of": len(shards), "files": " ".join(files)}
            for i, files in enumerate(shards)
        ]
    }


def _weight(f: str, root: Path) -> float:
    path = root / f
    size = path.stat().st_size if path.exists() else 1
    return size * SHARD_WEIGHTS.get(f, 1.0)


def _balance(files: list[str], n: int, root: Path | None = None) -> list[list[str]]:
    """Greedy longest-first bin packing by weight; deterministic."""
    root = root or REPO_ROOT
    bins: list[list[str]] = [[] for _ in range(n)]
    loads = [0.0] * n
    for f in sorted(files, key=lambda f: (-_weight(f, root), f)):
        i = loads.index(min(loads))
        bins[i].append(f)
        loads[i] += _weight(f, root)
    return [sorted(b) for b in bins if b]


def split_shards(files: list[str], max_shards: int, root: Path | None = None) -> list[list[str]]:
    """Balanced split by weighted file size (deterministic, no deps needed)."""
    root = root or REPO_ROOT
    files = sorted(set(files))
    if not files:
        return []

    total = sum(_weight(f, root) for f in files)
    n = max(1, min(max_shards, int(-(-total // BYTES_PER_SHARD))))
    if n == 1:
        return [files]
    return _balance(files, n, root)


def all_fast_files(graph: ModuleGraph) -> list[str]:
    service = set(SERVICE_TEST_FILES)
    return sorted(rel for rel in graph.test_modules().values() if rel not in service)


def make_plan(changed: list[str], graph: ModuleGraph | None = None, root: Path | None = None) -> Plan:
    root = root or REPO_ROOT
    changed = sorted({c.replace("\\", "/").strip() for c in changed if c.strip()})
    graph = graph or ModuleGraph.build(root)
    fast_all = all_fast_files(graph)
    service_all = list(SERVICE_TEST_FILES)

    def full(reasons: list[str]) -> Plan:
        return Plan("full", reasons, fast_all, service_all, True, changed)

    if not changed:
        return full(["no changed files reported -- cannot narrow, running everything"])

    kinds = {rel: classify(rel) for rel in changed}
    non_docs = [rel for rel, k in kinds.items() if k != "docs"]
    if not non_docs:
        return Plan("docs", ["docs-only change"], [], [], False, changed)

    reasons: list[str] = []
    for rel, k in kinds.items():
        if k == "ci":
            reasons.append(f"{rel}: CI/test infrastructure")
        elif k == "sensitive":
            reasons.append(f"{rel}: evidence-critical path")
        elif k == "other":
            reasons.append(f"{rel}: not a Python module or documentation")
        elif k in ("code", "test") and not (root / rel).exists():
            reasons.append(f"{rel}: deleted or renamed")
    if reasons:
        return full(reasons)

    # Targeted tier -----------------------------------------------------
    seeds: set[str] = set()
    fast: set[str] = set(INVARIANT_TEST_FILES) | graph.unmapped_test_files()
    service: set[str] = set()
    for rel, k in kinds.items():
        if k == "docs":
            continue
        mod = module_name_for(rel)
        if mod is None:  # pragma: no cover - classify() already routed these to full
            return full([f"{rel}: could not map to a module"])
        seeds.add(mod)
        if k == "test":
            (service if rel in SERVICE_TEST_FILES else fast).add(rel)

    affected = graph.transitive_dependents(seeds)
    tests = graph.test_modules()
    for m in affected:
        test_rel = tests.get(m)
        if test_rel is None:
            continue
        (service if test_rel in SERVICE_TEST_FILES else fast).add(test_rel)

    windows_seeds = {m for m, rel in tests.items() if rel in WINDOWS_SMOKE_TEST_FILES}
    windows = bool(windows_seeds & (affected | {m for m in seeds if m in windows_seeds}))

    if len(fast) > FULL_ESCALATION_RATIO * len(fast_all):
        return full([f"{len(fast)}/{len(fast_all)} parallel-safe files affected -- running everything"])

    reasons = [f"{len(seeds)} changed module(s) -> {len(affected)} dependent module(s)"]
    return Plan(
        "targeted",
        reasons,
        sorted(fast),
        sorted(service),
        windows,
        changed,
        affected_modules=sorted(seeds | affected),
    )


# ---------------------------------------------------------------------------
# git helpers + CLI
# ---------------------------------------------------------------------------


def changed_files_from_git(base: str, head: str, root: Path | None = None) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}" if base else head],
        cwd=root or REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def check_lists(root: Path | None = None) -> list[str]:
    root = root or REPO_ROOT
    problems = []
    for name, files in (
        ("SERVICE_TEST_FILES", SERVICE_TEST_FILES),
        ("WINDOWS_SMOKE_TEST_FILES", WINDOWS_SMOKE_TEST_FILES),
        ("INVARIANT_TEST_FILES", INVARIANT_TEST_FILES),
    ):
        for f in files:
            if not (root / f).is_file():
                problems.append(f"{name}: {f} does not exist")
    overlap = set(SERVICE_TEST_FILES) & set(WINDOWS_SMOKE_TEST_FILES)
    if overlap:
        problems.append(f"service files must not be in the Windows smoke set: {sorted(overlap)}")
    overlap = set(SERVICE_TEST_FILES) & set(INVARIANT_TEST_FILES)
    if overlap:
        problems.append(f"service files must not be in the invariant set: {sorted(overlap)}")
    return problems


def _write_github_output(path: str, plan: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"tier={plan['tier']}\n")
        fh.write(f"run_code={'true' if plan['run_code'] else 'false'}\n")
        fh.write(f"run_service={'true' if plan['service_files'] else 'false'}\n")
        fh.write(f"run_shards={'true' if plan['shards'] else 'false'}\n")
        fh.write(f"windows={'true' if plan['windows'] else 'false'}\n")
        fh.write(f"service_files={' '.join(plan['service_files'])}\n")
        fh.write(f"service_matrix={json.dumps(plan['service_matrix'])}\n")
        fh.write(f"windows_files={' '.join(WINDOWS_SMOKE_TEST_FILES)}\n")
        fh.write(f"shard_matrix={json.dumps(plan['shard_matrix'])}\n")


def _print_summary(plan: dict, stream=sys.stdout) -> None:
    print(f"tier: {plan['tier']}", file=stream)
    for r in plan["reasons"]:
        print(f"  - {r}", file=stream)
    print(f"changed files ({len(plan['changed'])}):", file=stream)
    for c in plan["changed"]:
        print(f"  {c}", file=stream)
    n_shards = len(plan["shards"])
    print(
        f"parallel-safe test files: {len(plan['fast_files'])} in {n_shards} shard(s); "
        f"serial service files: {len(plan['service_files'])} in {len(plan['service_shards'])} job(s); "
        f"windows smoke: {plan['windows']}",
        file=stream,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="compute the PR test plan")
    p.add_argument("--base", help="base commit (merge-base semantics: base...head)")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--changed-files", help="file with one changed path per line ('-' for stdin)")
    p.add_argument("--max-shards", type=int, default=DEFAULT_MAX_SHARDS)
    p.add_argument("--max-service-shards", type=int, default=DEFAULT_MAX_SERVICE_SHARDS)
    p.add_argument("--github-output", help="append outputs to this $GITHUB_OUTPUT file")
    p.add_argument("--json", action="store_true", help="print the full plan as JSON")

    e = sub.add_parser("explain", help="which tests would run for these changed paths, and why")
    e.add_argument("paths", nargs="+")

    sub.add_parser("check", help="verify the curated lists resolve to real files")

    args = ap.parse_args(argv)

    if args.cmd == "check":
        problems = check_lists()
        for pr in problems:
            print(f"error: {pr}", file=sys.stderr)
        if not problems:
            print("ok: curated test lists resolve")
        return 1 if problems else 0

    problems = check_lists()
    if problems:
        for pr in problems:
            print(f"error: {pr}", file=sys.stderr)
        return 2

    if args.cmd == "explain":
        graph = ModuleGraph.build()
        plan = make_plan(list(args.paths), graph)
        _print_summary(plan.to_dict())
        if plan.tier == "targeted":
            print("affected modules:")
            for m in plan.affected_modules:
                print(f"  {m}")
            print("selected parallel-safe files:")
            for f in plan.fast_files:
                tag = " (invariant)" if f in INVARIANT_TEST_FILES else ""
                print(f"  {f}{tag}")
            if plan.service_files:
                print("selected serial service files:")
                for f in plan.service_files:
                    print(f"  {f}")
        return 0

    if args.changed_files:
        text = sys.stdin.read() if args.changed_files == "-" else Path(args.changed_files).read_text()
        changed = [line.strip() for line in text.splitlines() if line.strip()]
    elif args.base:
        try:
            changed = changed_files_from_git(args.base, args.head)
        except subprocess.CalledProcessError as exc:
            # Fail open: an unusable base commit means "run everything",
            # never "run nothing". make_plan([]) is the full tier.
            print(
                f"warning: git diff failed ({exc.stderr.strip()}); running the full suite",
                file=sys.stderr,
            )
            changed = []
    else:
        ap.error("plan needs --base or --changed-files")

    result = make_plan(changed).to_dict(args.max_shards, max_service_shards=args.max_service_shards)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_summary(result)
    if args.github_output:
        _write_github_output(args.github_output, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
