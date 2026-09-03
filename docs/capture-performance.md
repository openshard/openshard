# Claude Code capture performance (PR7)

This documents what was measured, what changed, and what is still slow, for
the "make capture invisible to the developer" hardening pass on top of PR5
(auto-capture) and PR6 (rich receipts). It does not redesign the capture
pipeline (`Event -> Run/Attempt -> Shard -> Receipt`), Shard identity, or the
JSONL storage format.

**These numbers are from one development machine (Windows 11, Python 3.11)
and are a regression signal, not a universal performance claim.** Run
`python scripts/bench_claude_capture.py` yourself and compare against your
own baseline before drawing conclusions on different hardware/OS.

## Why this mattered more than it looks like

Claude Code's hook config (`openshard/adapters/claude_hooks_install.py`)
runs most hooks (`SessionStart`, `UserPromptSubmit`, `PostToolUse`,
`PostToolUseFailure`) as `async: true` -- Claude Code fires-and-forgets them,
so their latency was already mostly invisible. But **`Stop` and
`SessionEnd` are synchronous**, and the **status line is synchronous by
construction** (Claude Code waits on its stdout to render the line). `Stop`
fires at the end of *every assistant turn*. So before this change, every
single Claude Code turn was paying OpenShard's full hook latency
synchronously, and the status line (which can render very frequently) was
paying it too.

## What was actually on the critical path

Inspecting the path end to end (hook entrypoint -> `cli/main.py` -> the
Click app -> `openshard.adapters.claude_hooks` -> the per-session JSON
staging buffer -> `.openshard/runs.jsonl`) surfaced two problems, in order
of impact:

1. **Process start-up, not hook logic, dominated every invocation.**
   `openshard hooks claude` / `openshard hooks claude-status` are spawned as
   a fresh process by Claude Code for every hook and every status-line
   render. The console script pointed at `openshard.cli.main:cli`, a single
   Click app whose *module-level* imports pull in the run pipeline, the
   execution generator, provider clients (`httpx` and its own import chain:
   `importlib.metadata`, `email.*`), the planning generator, and the evals
   runner -- none of which the hook/status commands touch. Measured with
   `python -X importtime -c "import openshard.cli.main"`: **~884ms**
   cumulative, before any hook logic runs at all.
2. **The status-line handler was doing fold-boundary work on (almost)
   every ping.** `handle_claude_status` called `_fold()` whenever the
   observed model/cost/tokens changed and a Shard record already existed --
   which, once a session is doing real work, is close to every ping, since
   token counts change turn to turn. `_fold()` runs a `git diff` +
   `git ls-files --others` (2 subprocesses), a `git config --get
   remote.origin.url` lookup (1 subprocess), and a full read + crash-safe
   rewrite of `runs.jsonl` -- exactly the kind of work Requirement 7 says
   the status line must never do.

Everything else on the path -- the per-session staging buffer, the 30s
throttle on tool-hook folds, the bounded event/file caps -- was already
reasonably designed for this in PR5/PR6 and did not need to change.

## What changed

| # | Change | File(s) |
|---|---|---|
| 1 | New fast-dispatch console-script entrypoint: recognizes the exact `hooks claude` / `hooks claude-status` argv Claude Code uses and calls straight into `adapters/claude_hooks.py`, importing nothing else. Anything else (including `--help`, unknown flags, every other subcommand) falls through unchanged to the real Click app. | `openshard/cli/entrypoint.py` (new), `pyproject.toml` (`[project.scripts]`) |
| 2 | `openshard.__version__` resolved lazily (PEP 562 module `__getattr__`) instead of calling `importlib.metadata.version()` on every `import openshard`. | `openshard/__init__.py` |
| 3 | `handle_claude_status` no longer folds. It only updates and persists the small staging buffer; model/cost/token values are picked up at the *next* natural fold boundary (a throttled tool-hook snapshot, `Stop`, or `SessionEnd`) -- never fabricated, just deferred to a boundary that already existed. It also no longer creates a brand-new (git-collecting) buffer for a session it has never seen a lifecycle hook for; there is nothing useful to persist yet, so it is simply not recorded, and the next real hook creates the buffer normally. | `adapters/claude_hooks.py` (`handle_claude_status`, `_load_buffer_light`) |
| 4 | `git config --get remote.origin.url` (repo identity) is computed once per session and cached on the buffer, instead of once per fold. A remote does not change mid-session. | `adapters/claude_hooks.py` (`_cached_repo_identity`) |
| 5 | Bounded, fail-open lock waits. `_file_lock`/`history_file_lock`/`upsert_jsonl` accept an optional `timeout`; with a timeout, acquisition polls a non-blocking lock and raises `LockTimeoutError` on expiry instead of blocking forever. Default (`timeout=None`) is **unchanged** for every existing caller (native run pipeline, evals, etc.) -- this is opt-in. The hook/status-line handlers pass a 3s (5s for the stale-buffer sweep) bound; the existing top-level `except Exception` in `handle_claude_hook`/`handle_claude_status` already fails the single capture open on any exception, so a timed-out lock now degrades exactly like any other capture failure -- Claude Code is never blocked by it. | `history/jsonl_store.py`, `adapters/claude_hooks.py` |

Everything above is additive/behavior-preserving for anything other than
the hook/status-line hot path: `runs.jsonl`'s schema is unchanged, existing
callers of `jsonl_store` get the same unbounded-blocking lock they always
had, and every pre-existing test in `test_claude_hooks.py` /
`test_cli_claude_hooks.py` / `test_jsonl_store.py` passes unmodified (one
test was updated because it patched an internal seam -
`_load_or_create_buffer` - that the status path no longer calls; see
`tests/test_claude_hooks.py::TestModelTokenCostCapture::test_status_handler_never_raises_and_still_returns_text`).

## Benchmark methodology

`python scripts/bench_claude_capture.py` (stdlib only: `time.perf_counter`,
`statistics`, `subprocess`; no live Claude API calls, no network). Two
layers, on purpose:

- **subprocess** -- invokes the real, installed `openshard` console script
  exactly as Claude Code does (fresh process, real stdin payload, a real
  temp git repo). This is what a developer actually feels.
- **in-process** -- calls `handle_claude_hook` / `handle_claude_status` /
  `jsonl_store` functions directly in the already-running interpreter,
  isolating capture *logic* cost from interpreter start-up.

Each subprocess scenario uses a fresh session per sample except the
"steady state" ones (`UserPromptSubmit`, `PostToolUse` with no file change,
`claude-status`), which reuse one already-started session so the numbers
reflect normal repeated use, not repeated session setup. Run with
`--iterations N --in-process-iterations M`; `--skip-subprocess` for a quick
in-process-only check.

## Before / after

Before: `python scripts/bench_claude_capture.py --iterations 15
--in-process-iterations 100`, on the unmodified branch. After: the same
command with `--iterations 20 --in-process-iterations 200`, run alone (nothing
else competing for the machine) after all changes below landed.

| Scenario | Before median | Before p95 | After median | After p95 |
|---|---:|---:|---:|---:|
| subprocess: SessionStart (lifecycle hook) | 1081ms | 1147ms | 759ms | 1041ms |
| subprocess: UserPromptSubmit (steady state) | 853ms | 941ms | 381ms | 460ms |
| subprocess: PostToolUse, no file change | 828ms | 868ms | 399ms | 449ms |
| subprocess: PostToolUse, fold boundary + file change | 823ms | 884ms | 247ms | 307ms |
| subprocess: Stop (**synchronous, every turn**) | 1026ms | 1118ms | 388ms | 454ms |
| subprocess: SessionEnd (synchronous) | 1003ms | 1076ms | 400ms | 440ms |
| subprocess: claude-status (**synchronous, frequent**) | 970ms | 1965ms | 254ms | 327ms |
| in-process: `handle_claude_hook(UserPromptSubmit)` | 32.8ms | 44.4ms | 19.0ms | 23.6ms |
| in-process: `handle_claude_hook(PostToolUse, no fold)` | 34.9ms | 47.0ms | 19.3ms | 23.8ms |
| in-process: `handle_claude_status` (steady state) | **184.5ms** | **317.3ms** | **19.6ms** | **23.7ms** |
| in-process: `handle_claude_hook(Stop, folds)` | 172.5ms | 194.0ms | 139.4ms | 191.3ms |
| in-process: `append_jsonl` | 1.9ms | 2.8ms | 1.9ms | 2.7ms |
| in-process: `upsert_jsonl` (replace, growing file) | 15.7ms | 20.8ms | 15.7ms | 19.2ms |

Subprocess numbers still carry real run-to-run variance from interpreter/
site start-up (disk cache state, antivirus scan-on-open, etc.) -- run the
script yourself for a stable read on your own machine; treat the ~2-4x
subprocess improvement above as directional, not a guarantee. The
**in-process `handle_claude_status` row is the number that matters most
here and is not noisy**: it went from doing a real fold (git diff + git
identity + whole-file JSONL rewrite) on nearly every call to doing none of
that, a measured ~9x drop in median (184.5ms -> 19.6ms) with p95 tightening
from 317ms to 24ms -- the tail latency the old code could hit on a busy
repo essentially disappears. `SessionStart` remains the single slowest
subprocess scenario after the fix (759ms median) because it is the one
place that still always pays `collect_git_info`'s 4 git subprocesses (see
Remaining known bottlenecks) -- but that is a one-time per-session cost,
not a per-turn or per-status-ping one.

`handle_claude_hook(Stop, folds)` did **not** get meaningfully faster, on
purpose -- `Stop` is a genuine fold boundary (it must observe the real git
diff to stay honest about what changed), and Requirement 3 says not to
sacrifice correctness to chase a benchmark. What did shrink for `Stop` is
the *subprocess* wrapper around it (start-up cost), and the repeated
`git config` calls it used to also pay on every earlier status ping now
happen once per session instead.

## Why subprocess numbers don't hit "instant"

A `p95 < 10ms` target for the subprocess scenarios is not realistic and
would be a fabricated claim: on this machine, `python -X importtime -c
"pass"` alone measures **~60-85ms** for interpreter + `site` start-up,
before a single line of OpenShard code runs, and that floor is not
something OpenShard's capture logic can remove. What OpenShard controls is
not importing more than that floor needs -- which is exactly what the new
entrypoint does: `import openshard.adapters.claude_hooks` alone now
measures **~40ms** cumulative (down from ~154ms, mostly because
`__version__` is no longer eagerly resolved), versus **~565ms** for
`import openshard.cli.main` (down from ~884ms, still large because it is
the full CLI's dependency graph, and is now only paid by commands that
actually need it).

## Sync vs. async on the Claude Code side

| Hook | `async` | Why |
|---|---|---|
| `SessionStart` | true | staging only |
| `UserPromptSubmit` | true | staging only (though it does create the Shard record and fold once, on the first prompt) |
| `PostToolUse` / `PostToolUseFailure` | true | staging only, folds at most every 30s |
| `Stop` | **false** | must snapshot before the Claude process can be torn down |
| `SessionEnd` | **false** | finalizes the record; also raises Claude Code's default SessionEnd timeout budget |
| status line (`claude-status`) | **n/a -- inherently synchronous** | Claude Code renders its stdout directly |

(See `HookSpec`/`HOOK_SPECS` in `adapters/claude_hooks_install.py`.)

## Locking / concurrency

- `history/jsonl_store.py`'s sidecar-lock primitive (`fcntl.flock` on
  POSIX, `msvcrt.locking` on Windows) is unchanged in its default,
  unbounded-blocking mode -- every non-hook caller (the native run
  pipeline, evals, checkpoints, etc.) keeps exactly the guarantee it had.
- The Claude Code hook/status-line path now passes an explicit `timeout`
  (3s; 5s for the once-per-`SessionStart` stale-buffer sweep). On
  contention it polls a non-blocking lock attempt every 20ms and raises
  `LockTimeoutError` on expiry, which the existing fail-open exception
  handling in `handle_claude_hook`/`handle_claude_status` turns into "skip
  this one capture, Claude Code keeps running."
- New tests (`tests/test_jsonl_store.py::test_bounded_timeout_*`,
  `tests/test_claude_hooks.py::TestBoundedLockWaitsAndFailOpen`,
  `TestConcurrentHookActivity`) cover: a bounded wait actually raising
  within the requested window (not instantly, not forever); the default
  unbounded wait being unchanged; a real contended lock (held on a
  background thread) causing a hook to fail open within a bounded time
  instead of hanging; and 25 concurrent `PostToolUse` calls against the
  same session producing exactly one record with every call counted and no
  corrupted/torn JSONL lines. All of this runs (and is exercised in CI) on
  Windows, using the real `msvcrt` lock path.

## Fail-open behavior

Non-negotiable per the spec, and already true before this PR for most
failure modes: both `handle_claude_hook` and `handle_claude_status` wrap
their entire body in `except Exception` and degrade to a harmless outcome
(`action="error"` / the status-line fallback text) rather than raising.
What PR7 adds is making sure a **lock wait** actually reaches that
exception handler instead of hanging indefinitely first -- see Locking
above. `run_hook_from_stream`/`run_status_from_stream` (the stdin readers)
already had their own defensive layer for malformed/absent stdin. Nothing
in this PR narrows any of those guarantees.

## Compatibility

- `runs.jsonl`'s schema is unchanged; no `schema_version` bump.
- The staging buffer gained two internal fields (`repo_identity_computed`,
  `repo_identity`) purely as a same-session cache; it is transient, never
  read by any query/receipt path, and an old buffer without these fields
  just recomputes once, exactly like before.
- The hook command Claude Code invokes (`openshard hooks claude` /
  `openshard hooks claude-status`) is unchanged; only what happens *inside*
  the process that command launches changed. Existing installed
  `.claude/settings.local.json` hook configs keep working with no
  reinstall required.
- The console-script entry point (`[project.scripts]` in `pyproject.toml`)
  changed from `openshard.cli.main:cli` to `openshard.cli.entrypoint:main`.
  An editable/dev install must be re-run (`pip install -e .`) once to pick
  this up; a normal `pip install`/`pipx install` of a new release picks it
  up automatically.

## Tests

- `tests/test_jsonl_store.py` -- bounded-timeout lock tests (raises
  promptly, succeeds once released, default stays unbounded), public
  wrappers (`history_file_lock`, `upsert_jsonl`) plumb `timeout` through.
- `tests/test_claude_hooks.py` -- new classes `TestStatusLineFastPath`,
  `TestRepoIdentityCaching`, `TestBoundedLockWaitsAndFailOpen`,
  `TestConcurrentHookActivity`; one existing test updated to patch the
  status path's actual internal seam.
- `tests/test_cli_entrypoint.py` (new) -- the fast dispatcher is behavior-
  identical to the full CLI for every recognized/unrecognized argv shape
  (including `--help`, an unknown flag, and both `--event` forms), and,
  the point of the whole exercise, a subprocess probe confirms
  `openshard.run.pipeline` / `httpx` / even `openshard.cli.main` itself are
  never imported on the fast path, while a non-hook command still imports
  `openshard.cli.main` normally.
- Full targeted run: `pytest tests/test_claude_hooks.py
  tests/test_cli_claude_hooks.py tests/test_jsonl_store.py
  tests/test_cli_entrypoint.py` -- all passing. `ruff check` and `mypy`
  clean on every changed file.

## Remaining known bottlenecks (not fixed in this PR)

- **Interpreter/site start-up (~60-85ms on this machine) is an irreducible
  floor** for any subprocess-per-hook design; only a persistent process
  (a daemon Claude Code hooks talk to over a socket/pipe) would remove it,
  and that is a real redesign, out of scope here.
- **`SessionStart`'s one-time `collect_git_info` call still spawns 4
  separate git subprocesses** (`rev-parse --is-inside-work-tree`,
  `rev-parse --abbrev-ref HEAD`, `rev-parse HEAD`, `status --porcelain`).
  This is a shared utility (`analysis/repo_map.py`) used outside the hook
  path too, and it only runs once per session (not per event), so it was
  left alone -- but it is the largest per-*session* cost that remains, and
  a future PR could fold it into fewer git invocations.
- **This dev machine has an unrelated editable-install `sys.meta_path`
  finder** (`__editable___hermes_agent_...`) that adds measurable overhead
  to every Python import in this environment; that is machine-specific
  pollution, not something OpenShard can or should fix, and is called out
  here so the raw numbers above are not mistaken for OpenShard's floor on
  a clean machine.
- The `_TOOL_FOLD_INTERVAL_SECONDS = 30` throttle (unchanged, from PR5)
  means an interrupted turn with no `Stop` can lose up to 30s of staged
  tool evidence; this was an already-accepted tradeoff, not something PR7
  revisits.
