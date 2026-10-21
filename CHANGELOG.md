# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- The install guide and README no longer announce a standalone `.exe` or a `winget` package: neither is planned.

## [0.2.0] - 2026-10-21

### Added

- Prompts: the Requests page groups turns (one `llama-server` request each) into prompts, a conversation continuing on one slot without a pause of `prompt_gap_s` (30 s) or more, with time-weighted prefill and decode t/s per prompt. `t` nests turns under each prompt on the Requests page and switches between recent turns and recent prompts on the Live page.
- Cache hit and speculative-draft acceptance for every turn and prompt.
- CPU load, clock and processor queue on the Live page; per-core load, core and thread counts (P and E cores on hybrid CPUs), L1/L2/L3 cache sizes and CPU charts on the System page.
- `--threshold prompt_gap_s=N`.
- Sample counts in the speed table.

### Changed

- Prefill speed is grouped by context depth (tokens already in the KV cache plus the new ones) instead of by new prompt tokens: depth, not prompt size, drives prefill speed.
- t/s colours compare with the same preset's median at a similar context depth, in five bands (120% or more, 80-120%, 50-80%, under 50%, not enough data), and never fall back to the terminal's default colour. A prompt is compared with the time its turns usually take at their own depths. The by-context tables rank presets in each column; near-ties share a colour.
- Notes list only exceptions: `cache reset`, `cut off`, `shared`, `resized`.
- Requests are called turns. The volume table adds prompts and an issues column (cache resets, cut-off prompts) and shows loads with the mean load time.
- The prompt and turn tables fit 100 columns, dropping lower-priority columns on narrow terminals.

### Removed

- The hard page fault (page-in) alert: page-ins are normal OS activity. `--threshold pagein_warn` is still accepted, with a note, and ignored.
- The slowest-prefills list, the worst-case speed columns, the shared-memory effect table, and the `cached` column in the prompt and turn tables.

## [0.1.1] - 2026-10-20

### Added

- End-to-end integration tests in CI against a real `llama-server` binary.
- Modular documentation: install, usage, and troubleshooting pages, interface screenshot, contributing guidelines, and security policy.

### Fixed

- Treat HTTP 401/403 from `/models` on API-key protected servers as authentication failure rather than reporting the server unreachable.
- Match single-model process flags and logs to the watched URL when several `llama-server` instances run, and show an instance count notice.
- Exit 0 from `--check` on CPU-only machines with no GPU adapters when the server is reachable (`--  no GPU adapters (CPU-only)`).

## [0.1.0] - 2026-10-20

### Added

- Six pages: Live, Memory, System, Requests, Events and Tensors.
- `--once`: print one snapshot and exit.
- `--plan`: project each preset's VRAM against the OS budget.
- History kept in memory and mirrored to disk across restarts.
- Alerts with configurable thresholds.
- `--check`: report on prerequisites and reachable sources, then exit.
- Single-model servers, alongside the router.
- `--api-key`: Bearer token for llama-server.
- `--state-dir`, `--router-log` and `--gpu`: the state folder, the router log file, and the adapter to watch.
- Finding the server, the logs and the GPU without options.
- Logs without timestamps, with colours, or forwarded by the router.
- Notices that say why a figure is missing.
- Alert thresholds scaled to the machine.

Windows only; standard library only; 64-bit Python 3.11+.
