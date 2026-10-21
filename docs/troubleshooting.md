# Troubleshooting win-llmtop

**Contents:** [32-bit Python or older Python (`windows FAIL`)](#32-bit-python-or-older-python) · [No GPU counters or adapter not matched (`gpu FAIL`)](#no-gpu-counters-or-adapter-not-matched) · [No server found or unreachable (`server FAIL`)](#no-server-found-or-server-unreachable) · [Several llama-servers running](#several-llama-servers-running) · [`/slots` endpoint is off (`HTTP 404` / `HTTP 501`)](#slots-endpoint-is-off) · [Server or `/slots` authentication error (`HTTP 401` / `HTTP 403`)](#server-or-slots-authentication-error) · [Missing per-model log, unrecognised format or missing timestamps](#missing-per-model-log-unrecognised-format-or-missing-timestamps) · [Router log not found](#router-log-not-found) · [State folder not writable (`state FAIL`)](#state-folder-not-writable-or-unavailable) · [`llama-fit-params` not found](#llama-fit-params-not-found) · [Informational notices](#informational-notices) · [Unknown threshold or collector error](#unknown-threshold-or-collector-error) · [Garbled display in the classic console](#garbled-display-or-missing-braille-in-the-classic-console)

Each entry lists the exact message printed by `win-llmtop` or `win-llmtop --check` (see [`usage.md`](usage.md#first-run)), its cause, and how to fix it. Every command-line option links to [`llmtop.1.md#options`](llmtop.1.md#options).

---

## 32-bit Python or older Python

**Symptom:**

- Startup exits with `win-llmtop needs 64-bit Python (32-bit Python cannot read a 64-bit llama-server's memory)`, `win-llmtop needs Python 3.11 or later (this is X.Y)`, or `llmtop reads Windows performance counters and runs on Windows only.`
- `win-llmtop --check` prints:
	```text
	windows            FAIL  Windows 10.0.xxxxx, Python 3.x.x 32-bit
	```

**Cause:** `win-llmtop` walks the `llama-server` process address space with `VirtualQueryEx` and reads 64-bit Windows structures via `ctypes`. A 32-bit Python interpreter on 64-bit Windows cannot inspect a 64-bit `llama-server` process.

**Fix:** Install 64-bit Python 3.11 or later (`winget install Python.Python.3.13` or the 64-bit installer from [python.org](https://www.python.org/downloads/windows/)) and reinstall `win-llmtop`. See [`install.md#getting-64-bit-python`](install.md#getting-64-bit-python).

---

## No GPU counters or adapter not matched

**Symptom:**

- `win-llmtop --check` prints one of:
	```text
	gpu                FAIL  <adapter>, watching <adapter>; PDH counters absent
	gpu                FAIL  <adapter>, no adapter selected; PDH counters present
	gpu                --    no GPU adapters (CPU-only)
	gpu                FAIL  Windows only
	```
- The **Live** page shows `no GPU telemetry (needs Windows performance counters)` (or `--plan` prints `no GPU telemetry`).
- A notice prints `GPU index <N> not in 0..<M>` or `no GPU matching '<spec>'`.

**Cause:**

1. Windows is older than Windows 10 version 1709 (when `GPU Process Memory` and `GPU Engine` PDH counters were added), or the local Windows PDH counter registry is damaged.
2. [`--gpu`](llmtop.1.md#options) was given an adapter index outside `0..M` or a substring that matches no DXGI adapter name.
3. The session runs inside an environment without a DXGI display/compute adapter (such as a CPU-only machine or CI runner; `--check` prints `--  no GPU adapters (CPU-only)` and exits 0 when the server is reachable).

**Fix:**

- On CPU-only machines with no GPU adapters, no action is needed: `win-llmtop` monitors host RAM, system commit, page files and `llama-server` requests without GPU telemetry, and `--check` exits 0 when the server is reachable.
- If [`--gpu`](llmtop.1.md#options) was passed, omit it so `win-llmtop` selects the adapter `llama-server` runs on automatically, or run `win-llmtop --check` to see the exact adapter names and indices.
- If `PDH counters absent` appears on Windows 10 1709+ or Windows 11 with a working GPU driver, rebuild the Windows performance counter strings from an elevated terminal (`cmd.exe` or PowerShell **Run as administrator**):
	```shell
	lodctr /r
	```
	Then restart `win-llmtop`.

---

## No server found or server unreachable

**Symptom:**

- `win-llmtop --check` prints:
	```text
	processes          --    none running
	server             FAIL  server unreachable (tried http://127.0.0.1:8080, ...)
	```
	(or `pid <PID>: cannot read process info` when `llama-server` runs under another account with restricted permissions).
- The top bar shows `● server not found`, `● router DOWN`, or `● server DOWN`.
- The alert banner shows `router unreachable at <URL>: <error>`, and the notice list shows `server unreachable (tried http://127.0.0.1:8080, ...)`.

**Cause:** No `llama-server` process is running, or `llama-server` is listening on an address/port that could not be reached at `http://127.0.0.1:8080` or read from a local `llama-server` command line.

**Fix:**

- Start `llama-server` on the machine.
- If `llama-server` listens on a non-default host or port and its command line is not readable by your user account, pass [`--router`](llmtop.1.md#options) `http://<host>:<port>` or set `LLMTOP_ROUTER`.

---

## Several llama-servers running

**Symptom:**

- A notice prints:
	```text
	<N> llama-server instances found; watching <url> (pass --router to choose)
	```
- `win-llmtop` connects to a different `llama-server` instance than intended.

**Cause:** More than one independent `llama-server` process is running (not counting worker child processes spawned by a router). Single-model mode matches process flags and logs to the process whose `--host` and `--port` correspond to the watched URL; discovery tries `http://127.0.0.1:8080` first, then each process's command line.

**Fix:** Pass [`--router`](llmtop.1.md#options) `<URL>` (or set `LLMTOP_ROUTER`) to select the exact server URL you want to monitor.

---

## `/slots` endpoint is off

**Symptom:**

- An alert warns:
	```text
	/slots for <preset> returned HTTP 404: the server's /slots endpoint is off (start it with --slots)
	```
	(or `HTTP 501`).
- `win-llmtop --check` shows `/slots error: HTTP 404` on the `server` line, and the **Live** page preset section shows `slot data unavailable: HTTP 404`.

**Cause:** `llama-server` was started without the `--slots` flag, so it disables the `/slots` endpoint that reports in-flight prefill/decode progress, ETA and live tokens/s.

**Fix:** Add `--slots` to your `llama-server` command line (or to each preset's arguments in router mode) and restart `llama-server`. See [`usage.md#live-slot-telemetry`](usage.md#live-slot-telemetry).

---

## Server or `/slots` authentication error

**Symptom:**

- When `llama-server` is started with `--api-key`, connecting without a key causes:
	- `win-llmtop --check` prints:
		```text
		server             FAIL  <url> (<how found>): HTTP 401 (pass --api-key or set LLMTOP_API_KEY / LLAMA_API_KEY)
		```
		(or `HTTP 403`).
	- An alert warns:
		```text
		server at <url> returned HTTP 401: pass --api-key or set LLMTOP_API_KEY / LLAMA_API_KEY
		```
		(or `HTTP 403`). In this case, the `server unreachable` notice is not shown.
- When the server is reachable but the `/slots` endpoint requires a key:
	- An alert warns:
		```text
		/slots for <preset> returned HTTP 401: pass --api-key or set LLAMA_API_KEY
		```
		(or `HTTP 403`).
	- `win-llmtop --check` shows `/slots error: HTTP 401` on the `server` line, and the **Live** page shows `slot data unavailable: HTTP 401`.

**Cause:** `llama-server` requires a Bearer API key for HTTP requests. When started with `--api-key`, `/models` (and everything except `/health`) requires authentication, not only `/slots`.

**Fix:** Pass [`--api-key`](llmtop.1.md#options) `<KEY>`, or set `LLMTOP_API_KEY` or `LLAMA_API_KEY` in your environment before starting `win-llmtop`.

---

## Missing per-model log, unrecognised format or missing timestamps

**Symptom:**

- `win-llmtop` prints one of these notices:
	```text
	no log file for preset <name>
	<name>: lines are being read (<N>) but none match the expected format
	<name>: the log has no timestamps (pass --log-timestamps for wall-clock history)
	```
- `win-llmtop --check` prints `<name>: no log path`, `<name>: <path> (absent)`, `<name>: <path> (exists, no timestamps)`, or `logs               --    no log files configured or found`.

**Cause:**

- `no log file for preset <name>` / `no log path` / `(absent)`: `llama-server` was not given `--log-file`, or the preset's log is in a directory `win-llmtop` was not told about.
- `the log has no timestamps`: `llama-server` is writing a log file without `--log-timestamps`.
- `lines are being read (<N>) but none match the expected format`: the file pointed to is not a `llama-server` log, or no request has finished yet.

**Fix:** Pass `--log-file` and `--log-timestamps` to `llama-server`, and pass [`--log-dir`](llmtop.1.md#options) `<DIR>` (or set `LLMTOP_LOG_DIR`) to `win-llmtop` if presets do not specify full paths. See [`usage.md#per-model-logs-and-timestamps`](usage.md#per-model-logs-and-timestamps).

---

## Router log not found

**Symptom:**

- `win-llmtop --check` includes:
	```text
	router log: none found (model loads and evictions need it: pass --router-log or --log-dir)
	```
	or `router log: <path> (absent)`.

**Cause:** In router mode, `llama-server` logs model spawns, request queueing and LRU evictions to the router process's own log (`llama-server-<date>-<time>.log`), and `win-llmtop` was not given its path or folder.

**Fix:** Pass [`--router-log`](llmtop.1.md#options) `<PATH>` with the router log file, or [`--log-dir`](llmtop.1.md#options) `<DIR>` (or `LLMTOP_LOG_DIR`) pointing at the directory where the router writes its log files. See [`usage.md#router-event-log`](usage.md#router-event-log).

---

## State folder not writable or unavailable

**Symptom:**

- `win-llmtop --check` prints:
	```text
	state              FAIL  <path> (not writable)
	```
	or `state              FAIL  <path> (<error>)`.
- A notice prints:
	```text
	state folder <path> is unavailable: <reason>
	```
	(where `<reason>` is `no state directory configured`, `invalid path ...`, `drive <X:> does not exist`, `not a directory: <path>`, or `permission denied: <path>`).

**Cause:** The state folder (`%LOCALAPPDATA%\win-llmtop` by default, or the path passed to [`--state-dir`](llmtop.1.md#options) / `LLMTOP_STATE_DIR`) points to a missing drive, a regular file, an invalid Windows path, or a directory without write permission. When this happens, `win-llmtop` automatically disables persistence for the session and continues running in memory.

**Fix:** Point [`--state-dir`](llmtop.1.md#options) (or `LLMTOP_STATE_DIR`) at a writable folder, or pass [`--no-persist`](llmtop.1.md#options) when you intentionally want a stateless run.

---

## `llama-fit-params` not found

**Symptom:**

- `win-llmtop --check` prints:
	```text
	llama-fit-params   --    not found
	```

**Cause:** `llama-fit-params.exe` was not found in the same folder as `llama-server.exe` or on `PATH`. Without it, `win-llmtop` still derives model weights, KV cache and recurrent state from the GGUF header, but cannot cross-check them against `llama.cpp`'s own projection or show `llama.cpp`'s compute-buffer figure.

**Fix:** Keep `llama-fit-params.exe` (included in official `llama.cpp` Windows release archives) in the same directory as `llama-server.exe`, or add its folder to `PATH`.

---

## Informational notices

**Symptom:**

- `win-llmtop` or `win-llmtop --check` shows one of:
	```text
	server found at <URL> from llama-server (pid <PID>) command line (tried ...)
	single-model mode
	<N> llama-server instances found; watching <url> (pass --router to choose)
	<N> model processes resident: VRAM calibration is paused
	```

**Cause and action:**

- `server found at <URL> from llama-server (pid <PID>) command line`: the default `http://127.0.0.1:8080` did not answer, so `win-llmtop` found the server's `--host` and `--port` from the running `llama-server` process. Pass [`--router`](llmtop.1.md#options) `<URL>` (or set `LLMTOP_ROUTER`) to connect directly without discovery.
- `single-model mode`: `llama-server` is serving a single model rather than running as a multi-preset router. All six pages, alerts and memory projections work normally.
- `<N> llama-server instances found; watching <url> (pass --router to choose)`: more than one independent `llama-server` instance is running. Pass [`--router`](llmtop.1.md#options) `<URL>` to select which instance to monitor.
- `<N> model processes resident: VRAM calibration is paused`: more than one model child process is currently holding VRAM, so `win-llmtop` cannot attribute the unprojected VRAM overhead to a single preset. Calibration resumes automatically once a single model is resident.

---

## Unknown threshold or collector error

**Symptom:**

- Startup exits with:
	```text
	unknown or non-numeric threshold '<item>'; thresholds: ...
	```
- Or `win-llmtop --check` prints `collector          FAIL  <Exception>: <message>` (and the live display shows `llmtop: collector error (press q to quit)`).

**Cause:** [`--threshold`](llmtop.1.md#options) `NAME=VALUE` was given an unknown key or a non-numeric value, or an unexpected runtime exception occurred during a collector tick.

**Fix:** Check the valid threshold names (`spill_mib`, `burst_mib`, `avail_warn`, `avail_crit`, `commit_warn`, `commit_crit`, `hold_s`, `clear_s`, `near_budget_mib`, `at_budget_mib`, `idle_headroom_mib`, `rival_gpu_pct`, `card_full_pct`, `pagefile_pct`, `ws_drop_mib`, `burst_window_s`, `load_grace_s`, `hz`, `history_hours`) documented in [`llmtop.1.md#alerts`](llmtop.1.md#alerts).

---

## Garbled display or missing braille in the classic console

**Symptom:** Trend charts show empty boxes (`▯` / `?`) instead of braille dots, or raw ANSI escape codes appear on screen.

**Cause:** The classic Windows console (`conhost.exe`) with a legacy raster font lacks Unicode braille characters (`U+2800`–`U+28FF`), or virtual terminal processing is disabled in the console host.

**Fix:**

- Run `win-llmtop` in **Windows Terminal** (`wt.exe`) with a Unicode monospace font such as Cascadia Mono (see [`install.md#recommended-terminal`](install.md#recommended-terminal)).
- Press `s` (or pass [`--graph-style`](llmtop.1.md#options) `blocks`) to draw charts with standard block characters instead of braille.
- Pass [`--no-color`](llmtop.1.md#options) to turn off ANSI colour escape sequences.
