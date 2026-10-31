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
- [Licence of contributions](#licence-of-contributions)
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
| `main` | releases only: every commit is a tagged release, fast-forwarded from `staging`, and holds the `release-gate`, `ci-ok` and `Analyze (Python)` checks | the maintainer only (fast-forward from `staging`) |
| `staging` | the integration branch: pull requests target it; signed, linear, never force-pushed | the maintainer, or GitHub merging an approved PR (squash) |
| `dependabot/…` | Dependabot's update branches (targeting `staging`) | Dependabot |
| anything else | not created in this repo: contributors work on a branch in their fork (`fix/…`, `feat/…`, `docs/…`, `ci/…`) and open a PR against `staging` | — |

## Pull requests

- Target `staging` when opening a pull request (`main` holds releases only).
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
- Run `ruff check .` in the repository root at the version pinned in `.github/requirements/lint.txt` (CI's lint job); `uvx ruff@<pin> check .` does it without installing anything.

## Coding conventions

- Tabs for indentation, except YAML, JSON and CFF (spaces); see `.editorconfig`.
- Every `.py` file starts with `# SPDX-License-Identifier: GPL-3.0-or-later` (after a shebang, if there is one).
- Every command-line option must be documented in `docs/llmtop.1.md`; `tests/test_docs.py` enforces this.
- `tests/test_public.py` rejects local paths and private names from the public tree.

## Releases

To cut a release, the maintainer bumps the version in `pyproject.toml` and `CITATION.cff` and adds the dated heading in `CHANGELOG.md` on `staging`; once `release-gate`, `ci-ok` and `Analyze (Python)` pass, `main` is fast-forwarded to `staging` and the signed `v*` tag is pushed. Ordinary `staging` commits aren't releases: the `release-check` job passes without posting `release-gate`, which keeps them off `main`, and it fails only on a new version that isn't ready. The release workflow re-runs all of CI on the tagged commit, and publishing to PyPI waits for the maintainer's approval in the `pypi` environment. Releases are immutable once published. Versions follow Semantic Versioning, and `CHANGELOG.md` follows Keep a Changelog.

## Help wanted

**GPU load on NVIDIA and Intel cards needs someone with the hardware.** The maintainer has only an AMD card. There, Windows' `GPU Engine` counters read 0 or 100 from one sample to the next during compute work, so `win-llmtop` reads AMD's driver sensor instead. NVIDIA and Intel cards fall back to a 3-second mean of those counters, and nobody has checked whether that is right.

To report what your card does, run a long prompt and compare the Live page's `GPU load` with your vendor's own tool (for example `nvidia-smi`). Open an issue with your card, driver version and llama.cpp backend, the `gpu` line of `win-llmtop --check`, and both readings.

If the figures disagree, the fix is a reader like `AmdLoad` in `llmtop_lib/win.py`, picked by adapter vendor ID in `Collector._adapter_load`. It must use only ctypes and a library that ships with the driver, load it from the system folder, and fall back to the counters when the library or sensor is missing. A pull request needs a real reading from the hardware alongside its tests.

## Labels

- `good first issue` — a good place to start.
- `help wanted` — the maintainer would like a hand.

## Licence of contributions

win-llmtop is licensed under the GNU General Public License v3.0 or later (`GPL-3.0-or-later`). By opening a pull request you agree that your contribution is licensed under the same licence, so the terms are the same inbound and outbound; no sign-off line is needed.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
