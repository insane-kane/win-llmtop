# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
