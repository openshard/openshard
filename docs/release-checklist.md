# OpenShard Release Checklist

## Pre-release

- [ ] Working tree is clean (`git status`)
- [ ] All tests pass: `python -m pytest`
- [ ] Linter passes: `python -m ruff check .`
- [ ] Version bumped in `pyproject.toml`

## Build

```bash
pip install build
python -m build
```

## Inspect dist

```bash
pip install twine
python -m twine check dist/*
```

## Optional: TestPyPI smoke test

```bash
twine upload --repository testpypi dist/*
pip install --index-url https://test.pypi.org/simple/ openshard
openshard --version
```

## Publish to PyPI

```bash
twine upload dist/*
```

## GitHub release

```bash
git tag v0.2.0
git push origin v0.2.0
```

Then create a GitHub Release at https://github.com/openshard/openshard/releases/new
targeting the new tag.
