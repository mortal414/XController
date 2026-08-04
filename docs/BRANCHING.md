# Branching Workflow

This repository uses `main` as its single long-lived, releasable branch. Work is isolated on short-lived branches and merged through focused pull requests.

## Branch Roles

- `main`
  Releasable source of truth for the controller package, tests, packaging files, and documentation.
- Short-lived feature or fix branches
  One scoped change per branch. Keep tests and documentation with the implementation they validate.

Downstream projects should update from `main` or a release tag, not from a development branch.

## Daily Workflow

Refresh the remote state and branch from `origin/main`:

```bash
git fetch origin
git switch -c <feature-branch> origin/main
```

Before opening or updating a pull request, rebase the focused branch on the latest `main` and run the local CI gates:

```bash
git fetch origin
git rebase origin/main
python -m ruff check .
python -W error -m pytest -q
```

Push the branch and open a pull request targeting `main`:

```bash
git push -u origin <feature-branch>
```

Do not commit persistent browser profiles, credentials, diagnostic snapshots, captured media, build output, or unrelated local changes.

## Suggested Rules

- Keep `main` fast-forwardable and releasable at all times.
- Delete merged feature branches when they are no longer needed.
- Keep local experiments out of a pull request unless they are intentional, maintained project assets.
- Tag releases from `main` after CI passes.
