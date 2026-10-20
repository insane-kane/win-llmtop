# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
