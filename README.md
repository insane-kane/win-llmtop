# win-llmtop

[![CI](https://github.com/insane-kane/win-llmtop/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/insane-kane/win-llmtop/actions/workflows/ci.yml) [![PyPI version](https://img.shields.io/pypi/v/win-llmtop)](https://pypi.org/project/win-llmtop/) [![Python versions](https://img.shields.io/pypi/pyversions/win-llmtop)](https://pypi.org/project/win-llmtop/) [![Licence](https://img.shields.io/pypi/l/win-llmtop)](https://github.com/insane-kane/win-llmtop/blob/main/LICENSE)

A live, read-only terminal panel for [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` on Windows — see where your VRAM, shared GPU memory and system RAM go while a model runs.

![The Live page](https://raw.githubusercontent.com/insane-kane/win-llmtop/main/docs/img/live.svg)

[What it shows](#what-it-shows) · [Who it is for](#who-it-is-for) · [Quick start](#quick-start) · [Install](#install-options-at-a-glance) · [Requirements](#requirements) · [Get more out of it](#get-more-out-of-it) · [Common options](#common-options) · [Documentation](#documentation)

## What it shows

`win-llmtop` is strictly **read-only**: it reads the server's HTTP API, log files, Windows performance counters and, on AMD cards, the driver's GPU activity sensor, and never changes the server. The only programs it runs are two helpers of the `llama-server` it watches — `llama-server --help`, to read each flag's default, and `llama-fit-params`, llama.cpp's own memory projection — and it runs them only from the folder of that running `llama-server`, never from a path the server reports, the current folder or `PATH`.

- **Dedicated and shared GPU memory per process** against the DXGI OS budget, with alerts when Windows demotes VRAM into system RAM.
- **System RAM, commit charge and the page file** — working sets by consumer, private commit headroom and disk page-ins/page-outs.
- **CPU load, clock and cache levels** — per-core load, P/E cores and L1/L2/L3 sizes.
- **Live in-flight request progress** — active phase (`PREFILL` or `DECODE`), prompt batch progress, ETA, live tokens/s and KV context fill.
- **Prompt and turn history** — prefill and decode speed by context depth, cache hit and draft acceptance per turn and per prompt.
- **Model memory map and tensor placement** — weights, KV cache, recurrent state and compute buffers derived from the GGUF header and checked against `llama-fit-params`, plus per-layer GPU/CPU placement.
- **Router and threshold events** — model loads, queueing, evictions and sustained memory alerts persisted across restarts.

## Who it is for

Built by [Aaron Kane (`@insane-kane`)](https://github.com/insane-kane) for Windows users running llama.cpp's `llama-server` — either in router mode across multiple presets or serving a single model — on the same machine. It answers whether a preset fits inside Windows' VRAM budget, when shared GPU memory spillover slows decoding down, and where host RAM goes.

## Quick start

```shell
pipx install win-llmtop
win-llmtop --check
win-llmtop
```

- `pipx install win-llmtop` — installs `win-llmtop` into an isolated environment and puts it on `PATH`; `uv tool install win-llmtop` does the same if you manage your tools with `uv`.
- `win-llmtop --check` — checks Windows counters, the GPU budget, the running `llama-server`, logs and state directory, and exits `0` when the server is reachable and either GPU telemetry is present or the machine has no GPU adapters (`1` otherwise).
- `win-llmtop` — opens the live six-page terminal panel (`1`–`6` or `Tab` switch pages, `q` quits).

## Install options at a glance

See the [full installation guide (`docs/install.md`)](https://github.com/insane-kane/win-llmtop/blob/main/docs/install.md) for commands, upgrades, uninstalls and package verification. Every method installs the same package; pick the one that matches your setup.

| Method | Command | Use when |
| :--- | :--- | :--- |
| **[pipx](https://github.com/insane-kane/win-llmtop/blob/main/docs/install.md#pipx)** | `pipx install win-llmtop` | You want `win-llmtop` on `PATH` in an isolated environment, installed from the Python you already have |
| **[uv tool](https://github.com/insane-kane/win-llmtop/blob/main/docs/install.md#uv)** | `uv tool install win-llmtop` | The same isolated `PATH` install, when you manage your tools with `uv` |
| **[pip](https://github.com/insane-kane/win-llmtop/blob/main/docs/install.md#pip-virtual-environment-or-user-site)** | `py -m pip install win-llmtop` | You have an existing virtual environment, or want a `--user` install |
| **[GitHub release wheel](https://github.com/insane-kane/win-llmtop/blob/main/docs/install.md#offline-or-air-gapped-install)** | `py -m pip install win_llmtop-*.whl` | The machine has no internet access (offline or air-gapped) |
| **[From source](https://github.com/insane-kane/win-llmtop/blob/main/docs/install.md#from-source)** | `py llmtop.py` | You want to run it from a git checkout without installing (zero dependencies) |

## Requirements

- **Windows 10 version 1709 or later** — provides the per-process `GPU Process Memory` and `GPU Engine` performance counters.
- **64-bit Python 3.11 or later** — 32-bit Python cannot inspect a 64-bit `llama-server` process.
- **No third-party Python packages** — uses the Python standard library only.
- **A local `llama-server`** — running in router mode or single-model mode on the same machine.

## Get more out of it

`win-llmtop` works with zero configuration, and unlocks deeper telemetry when `llama-server` exposes logs and slots:

- **Per-model log file with timestamps** — pass `--log-file` and `--log-timestamps` to `llama-server` (win-llmtop reads the path from its command line; `--log-dir` only when it can't) for request history, cache hit rates, draft acceptance and wall-clock times ([usage guide](https://github.com/insane-kane/win-llmtop/blob/main/docs/usage.md#per-model-logs-and-timestamps)).
- **Live `/slots` telemetry** — on by default in `llama-server` (build b11669; `--no-slots` turns it off), showing live prefill/decode progress, current tokens/s, ETA and context fill ([usage guide](https://github.com/insane-kane/win-llmtop/blob/main/docs/usage.md#live-slot-telemetry)).
- **Router event log** — pass `--router-log` or `--log-dir` so `win-llmtop` can tail router model loads, request queueing and preset evictions ([usage guide](https://github.com/insane-kane/win-llmtop/blob/main/docs/usage.md#router-event-log)).

## Common options

See [`win-llmtop(1)`](https://github.com/insane-kane/win-llmtop/blob/main/docs/llmtop.1.md#options) for the complete reference of every command-line option, environment variable and default.

| Option | Environment variable | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `--check` | — | off | Check prerequisites and reachable sources, then exit |
| `--once` | — | off | Print a single snapshot and exit (combine with `--page`) |
| `--plan [PRESET]` | — | off | Project preset VRAM against the OS budget without loading |
| `--router URL` | `LLMTOP_ROUTER` | find it | `llama-server` URL (`http://127.0.0.1:8080` or discovered) |
| `--log-dir DIR` | `LLMTOP_LOG_DIR` | (none) | Directory containing per-model and router log files |
| `--router-log PATH` | — | (none) | Path to the router's own log file |
| `--api-key KEY` | `LLMTOP_API_KEY` or `LLAMA_API_KEY` | (none) | Bearer token for `llama-server` HTTP requests |
| `--window {1m,5m,15m,1h,6h,24h}` | — | `5m` | Initial time window for trend charts |

## Known issues

- **GPU load on NVIDIA and Intel cards is unverified (help wanted).** AMD cards use the driver's own activity sensor, because Windows' `GPU Engine` counters proved erratic for compute work on an AMD card. Every other GPU, and an AMD card without that sensor, shows a 3-second mean of those counters, which has not been checked on real NVIDIA or Intel hardware and may read wrong. Nothing else depends on it, and `win-llmtop --check` says which source is in use. See [Contributing](https://github.com/insane-kane/win-llmtop/blob/main/CONTRIBUTING.md#help-wanted) for how to help.

## Documentation

- **[Documentation index (`docs/README.md`)](https://github.com/insane-kane/win-llmtop/blob/main/docs/README.md)** — one-line map of all documentation pages.
- **[Installation (`docs/install.md`)](https://github.com/insane-kane/win-llmtop/blob/main/docs/install.md)** — every deployment method, offline wheels, provenance verification, 64-bit Python and terminal setup.
- **[Usage (`docs/usage.md`)](https://github.com/insane-kane/win-llmtop/blob/main/docs/usage.md)** — `--check` output, the six pages, keyboard controls, `llama-server` flags, `--once`, `--plan` and state files.
- **[Troubleshooting (`docs/troubleshooting.md`)](https://github.com/insane-kane/win-llmtop/blob/main/docs/troubleshooting.md)** — symptom, cause and fix for every `--check` failure, notice and warning.
- **[Manual page (`docs/llmtop.1.md`)](https://github.com/insane-kane/win-llmtop/blob/main/docs/llmtop.1.md)** — `win-llmtop(1)` reference: all options, alert rules, data sources and measurement limits.

## Contributing, support and security

- **Contributing and support** — see [`CONTRIBUTING.md`](https://github.com/insane-kane/win-llmtop/blob/main/CONTRIBUTING.md) to report bugs or submit pull requests, and [`SUPPORT.md`](https://github.com/insane-kane/win-llmtop/blob/main/SUPPORT.md) for usage help.
- **Security** — see [`SECURITY.md`](https://github.com/insane-kane/win-llmtop/blob/main/SECURITY.md) to report a vulnerability privately through [GitHub's private vulnerability reporting](https://github.com/insane-kane/win-llmtop/security/advisories/new).
- **Code of conduct** — participation is governed by [`CODE_OF_CONDUCT.md`](https://github.com/insane-kane/win-llmtop/blob/main/CODE_OF_CONDUCT.md).

## Citing

If you use `win-llmtop` in research or benchmarks, see [`CITATION.cff`](https://github.com/insane-kane/win-llmtop/blob/main/CITATION.cff) for citation metadata.

## Licence

Copyright (C) 2026 Aaron Kane.

GNU General Public License v3.0 or later (`GPL-3.0-or-later`); see [`LICENSE`](https://github.com/insane-kane/win-llmtop/blob/main/LICENSE).
