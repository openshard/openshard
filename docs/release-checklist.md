# OpenShard Release Checklist

Releases are built and published **only** by the `Release` workflow
(`.github/workflows/release.yml`), from the exact commit a pushed `v*` tag
points at. Nothing is ever uploaded from a developer machine. This is what
makes a PyPI artifact provably equal to its git tag.

Why: PyPI `openshard==0.4.1` was built from a local working tree that
contained uncommitted work, so the published package did not match the
`v0.4.1` tag and had to be yanked. A clean runner checking out the tag
cannot reproduce that.

## One-time setup (already done unless the project moves)

- PyPI project `openshard` → Publishing → add a Trusted Publisher:
  owner `openshard`, repository `openshard`, workflow `release.yml`,
  environment `pypi`.
- GitHub repository → Settings → Environments → `pypi`: require a reviewer
  (the maintainer) so a pushed tag still needs one explicit approval before
  anything is published.

## Releasing a version

On a branch, in one pull request to `main`:

- [ ] Bump `version` in `pyproject.toml`.
- [ ] Give the version a dated `## X.Y.Z - YYYY-MM-DD` section in
      `CHANGELOG.md` (the workflow refuses `Unreleased`).
- [ ] CI green (ruff, mypy, full pytest on Linux and Windows).
- [ ] Manual smoke tests done where the change needs them (a fresh install
      with each affected agent; see `docs/demo-smoke-checklist.md`).
- [ ] Capture upgrade smoke: with a pre-upgrade `.claude/settings.local.json`
      (no `X-OpenShard-Capture-Token` header), run `openshard setup` and
      confirm `doctor` no longer reports "no valid capture credential", then
      complete one Claude Code turn and confirm `openshard last` shows the
      session with `Capture  partial`, `Gaps  None known` and a `Receipt ID`.
- [ ] `openshard capture status` shows `refused: 0 unauthenticated` after a
      normal session; a non-zero count means some hook still lacks a credential.

After the pull request is merged:

```bash
git checkout main
git pull --ff-only origin main
git status --porcelain          # must print nothing
git tag -a vX.Y.Z -m "OpenShard vX.Y.Z"
git push origin vX.Y.Z
```

The workflow then, in order:

1. checks out the tag and verifies `vX.Y.Z` == `pyproject.toml` version and
   that the tagged commit is on `main`;
2. runs ruff, mypy and the full test suite on Linux and Windows;
3. builds the wheel and sdist, checks the build left the tree untouched,
   runs `twine check --strict`, and checks the artifact names carry the
   tagged version;
4. waits for the `pypi` environment approval, then publishes through PyPI
   Trusted Publishing (OIDC; no token exists anywhere);
5. creates the GitHub Release from the `CHANGELOG.md` section and attaches
   the wheel and sdist.

- [ ] Approve the `pypi` environment deployment when it pauses.
- [ ] After it finishes: `pip install openshard==X.Y.Z` into a fresh venv,
      `openshard --version`, and confirm the release page shows both files.

If any step fails, fix it on `main` through a normal pull request, then
**move to the next patch version**. Never delete and re-push a tag, and
never re-upload a version to PyPI.

## Emergency: publishing without the workflow

Only if GitHub Actions is unavailable. Even then the artifact must come from
the tag, never from a working tree:

```bash
git fetch origin --tags
git worktree add /tmp/openshard-release vX.Y.Z      # a clean checkout of the tag
cd /tmp/openshard-release
python -c "import tomllib; v=tomllib.load(open('pyproject.toml','rb'))['project']['version']; assert 'vX.Y.Z' == 'v'+v, v"
pip install build twine
python -m build
python -m twine check --strict dist/*
python -m twine upload dist/*                        # needs a scoped API token
```

Then create the GitHub Release by hand from the same `dist/` files, and
remove the worktree (`git worktree remove /tmp/openshard-release`).
