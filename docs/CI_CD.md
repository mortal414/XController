# CI/CD Instructions

This repository validates its releasable branch before any downstream project pulls from it.

## CI

Use the GitHub Actions workflow at `.github/workflows/ci.yml`.

The published repository has one long-lived branch, `main`. CI should run for pushes to `main` and pull requests targeting `main`; short-lived feature branches are validated through their pull requests.

The workflow performs the following checks on both `ubuntu-latest` and `windows-latest`:

- installs the project with its development dependencies
- lints the complete tree with Ruff
- runs the test suite with warnings promoted to errors
- builds the wheel and sdist
- installs the built wheel and smoke-tests the public imports and `py.typed` marker

Run the same source-tree test gate locally with:

```bash
python -m ruff check .
python -W error -m pytest -q
```

That means:

- `main` gets package-level verification on both Linux and Windows, including the sync-runtime test coverage that matters most for Windows consumers
- pull requests get the same validation before merge

## CD

There is no automatic deployment step configured here. The recommended release flow is manual and branch-driven:

1. Land or cherry-pick releasable controller changes onto `main`.
2. Wait for CI on `main` to pass.
3. Build release artifacts locally if needed:

```bash
python -m pip install --upgrade pip build
python -m build
```

4. Tag the release from `main`, substituting the version being released:

```bash
git tag v<version>
git push origin main --tags
```

5. Update the downstream project from `main` or from the new tag, depending on how tightly you want to pin updates.

## Practical Policy

- Do not deploy from an unreviewed feature branch.
- Prefer focused, reviewable pull requests over long-lived integration branches.
- Use `main` as the source of truth for anything another repository imports.
