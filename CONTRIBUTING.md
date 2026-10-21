# Contributing to win-llmtop

Thanks for your interest in win-llmtop. Contributions of every kind are welcome.

## Contents

- [What helps](#what-helps)
- [Where development happens](#where-development-happens)
- [Branch policy](#branch-policy)
- [Pull requests](#pull-requests)
- [Development setup](#development-setup)
- [Coding conventions](#coding-conventions)
- [Releases](#releases)
- [Labels](#labels)
- [Code of conduct](#code-of-conduct)

## What helps

- Bug reports, with the output of `win-llmtop --check`.
- llama.cpp log formats from other builds, so the parser keeps working.
- Documentation fixes and improvements.

## Where development happens

The code is developed in the maintainer's private repository and published here. Accepted pull requests are squash-merged on GitHub, then pulled back into the private repository, so nothing is lost.

## Branch policy

| Branch | Purpose | Who pushes |
| :--- | :--- | :--- |
| `main` | always releasable; every commit pushed to it must have passed all of CI and CodeQL and be signed, and the history is linear (no merge commits, no force-push, never deleted) | the maintainer (fast-forward), or GitHub merging an approved PR (squash) |
| `staging` | the maintainer's pre-`main` gate: a commit goes here first, CI runs, then the same commit is fast-forwarded to `main` | the maintainer only |
| `dependabot/…` | Dependabot's update branches | Dependabot |
| anything else | not created in this repo: contributors work on a branch in their fork (`fix/…`, `feat/…`, `docs/…`, `ci/…`) and open a PR against `main` | — |

## Pull requests

- A pull request needs CI (the `ci-ok` check) and CodeQL green.
- It needs one approving review from the code owner (`@insane-kane`), and every review thread resolved.
- A new push dismisses earlier approvals.
- Merging is squash-only, so the PR title becomes the commit subject: use Conventional Commits (`fix: …`, `feat: …`, `docs: …`, `ci: …`, `test: …`, `chore: …`).
- First-time contributors' workflow runs wait for the maintainer's approval (GitHub's setting for forks).

The maintainer aims to respond to a pull request within 7 days.

## Development setup

- 64-bit Python 3.11 or later on Windows.
- No dependencies: clone the repository, then run `py llmtop.py`.
- Tests: `py -I tests/run_all.py` runs every `tests/test_*.py`; each file also runs on its own.
- Lint: `ruff check .` (rules E9, F, B, set in `pyproject.toml`), and `editorconfig-checker`.

## Coding conventions

- Tabs for indentation, except YAML, JSON and CFF (spaces); see `.editorconfig`.
- Every `.py` file starts with `# SPDX-License-Identifier: GPL-3.0-or-later` (after a shebang, if there is one).
- Every command-line option must be documented in `docs/llmtop.1.md`; `tests/test_docs.py` enforces this.
- `tests/test_public.py` rejects local paths and private names from the public tree.

## Releases

Releases are cut only from `main`, by a signed `v*` tag that only the maintainer can create. The release workflow re-runs all of CI on the tagged commit, and publishing to PyPI waits for the maintainer's approval in the `pypi` environment. Releases are immutable once published. Versions follow Semantic Versioning, and `CHANGELOG.md` follows Keep a Changelog.

## Labels

- `good first issue` — a good place to start.
- `help wanted` — the maintainer would like a hand.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
