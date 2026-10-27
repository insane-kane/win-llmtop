# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] - 2026-10-27

### Security

- `win-llmtop` runs its two helper programs, `llama-server --help` and `llama-fit-params`, only from the folder of the running `llama-server` it monitors: the process named `llama-server*` that listens on the watched port at a loopback address. It no longer takes a program path from the server's `/models` reply, the current folder or `PATH` ([GHSA-fxxm-3w8c-mjr4](https://github.com/insane-kane/win-llmtop/security/advisories/GHSA-fxxm-3w8c-mjr4)).
- The model, draft-model and log-file paths a server reports are opened only for that server, and only when they are absolute paths on a local drive letter; network, device and relative paths are skipped with a notice.
- The API key is sent only to a URL given with `--router` (or `LLMTOP_ROUTER`) or read from a running `llama-server`'s command line, never to the default-address probe, and HTTP redirects are no longer followed.
- A server at a loopback address is always reached directly, never through a proxy; a remote `--router` uses the system proxy settings.
- Escape sequences and control characters in text from the server, the logs or error messages are removed before they reach the terminal; only `win-llmtop`'s own colour codes are written.

### Changed

- A server on another machine, or one whose listening process is not `llama-server*`, is still monitored, but its Memory and Tensors pages say `could not analyse` with the reason, and `--check` says why on its `llama-fit-params` line.
- The release workflow checks that a release tag is signed by the maintainer's pinned key fingerprint.

## [0.3.0] - 2026-10-22

### Added

- `d` on the Events page and `--once --page events --detail` show each event's detail lines.
- `--graph-style coarse` and `--graph-style ascii`, and the `s` key cycles all four styles; the classic console now gets `coarse` by default.
- `--check` prints a `terminal` line saying which console was detected and which chart style it uses.
- The Requests page marks a prompt that spanned a compaction as `compacted` (`compacted ×N`).
- Atheris fuzz targets for the log, GGUF, fit-output and argument parsers, run in CI.

### Changed

- The Live shared-memory row is renamed `llama-server shared` to report only `llama-server`'s own GPU memory backed by system RAM (its expected share and what is beyond it), dropping the system-wide all-process percentage here, on the Memory page and in the `spill` alert detail.
- Every event and alert is now a short headline (one line, at most 72 characters, the one or two figures that matter) plus an optional detail: the detail carries the comparisons, thresholds and deductions the headline leaves out. The detail is persisted with the event and old event lines without one still load.
- The Events page shows the headlines; `d` toggles the detail line(s) under each event (dim, wrapped to the width), and `--once --page events --detail` shows them in a snapshot.
- Alert levels now follow the consequence: `committed, not resident` is info within `load_grace_s` of a load and crit after; `shared GPU memory` is info when only above the CPU-side baseline and warn when grown past the resident minimum; a log `E` line is crit only for a known failure pattern (out of memory, allocation and load failures, aborts, fatals) and warn otherwise; a prompt cut off (`truncated = 1`) and a failed request (`got exception`) are crit.
- `/slots` unavailable (HTTP 404, 501, 401 or 403) is info, not a warning: the server is fine, llmtop only loses live slot progress.
- The Requests page no longer treats a compaction (the conversation summarised and restarted from the summary) as a new prompt: the turn continues the prompt and gets a `compacted` note instead of a `cache reset` one.

### Fixed

- The classic Windows console (`conhost.exe`) no longer draws boxes in the trend charts: `win-llmtop` now defaults to a new `coarse` glyph style there (code page 437 only — `▄`/`█` charts, whole- and half-cell bars), detected from the terminal like the existing `braille` and `blocks` styles. `--graph-style` and the `s` key cycle all four, including a plain-ascii style.
- A malformed GGUF model header now fails with a clear `ValueError` or `IndexError` in the memory map, as the parser promises, instead of `ZeroDivisionError` (a zero `general.alignment`) or `OverflowError` (a string array with an oversized length).
- A loaded model whose command line is unknown (a router whose `/models` entry has no `status.args`, or a single-model server whose process is not found) no longer crashes the Memory and Tensors pages with a `KeyError`: the pages show `could not analyse: …`, the state is recorded once instead of retried every frame, and `m` still cycles over the model.
- When a `loaded` or `loading` model is reported but no process named `llama-server*` is found, a notice is printed — `llama-server: no process named llama-server* found, so its GPU and RAM figures read 0 (a renamed or wrapped server is not recognised)` — and `--check` agrees.
- A page that fails to render no longer locks the display: the header (tabs and clock) and footer keys keep rendering, the body shows `llmtop: this page failed to render (<type>: <message>)` and `press 1-6 to try another page`, and the traceback is written to `llmtop-error.log` in the state folder (created only on error, overwritten per error, with the version, Python and platform). The same error is written once, not every tick; if the state folder is unavailable the last traceback lines are shown in the body.

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
