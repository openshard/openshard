# OpenShard Release Checklist

## Pre-release

- [ ] Working tree is clean (`git status`)
- [ ] All tests pass: `python -m pytest`
- [ ] Linter passes: `python -m ruff check .`
- [ ] Type check passes (CI runs it): `python -m mypy openshard/ --ignore-missing-imports`
- [ ] Version bumped in `pyproject.toml`

## Publish

`.github/workflows/release.yml` builds, checks, and publishes to PyPI (via
[PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/) --
no API token stored anywhere) whenever a GitHub Release is published. It
also attaches the built sdist/wheel to that release. All that's left by
hand is creating the tag and the release itself:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Then create a GitHub Release at https://github.com/openshard/openshard/releases/new
targeting the new tag, write the notes, and hit **Publish release** --
the workflow does the rest.

One-time setup (needed before this works): register the repo as a
trusted publisher for the `openshard` project on PyPI (pypi.org ->
openshard -> Publishing -> Add a new publisher), pointing at this repo
and the `release.yml` workflow.

### Optional: TestPyPI smoke test (manual, before tagging)

```bash
pip install build twine
python -m build
twine upload --repository testpypi dist/*
pip install --index-url https://test.pypi.org/simple/ openshard
openshard --version
```
