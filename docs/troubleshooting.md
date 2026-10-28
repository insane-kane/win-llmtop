# Troubleshooting win-llmtop

**Contents:** [32-bit Python or older Python (`windows FAIL`)](#32-bit-python-or-older-python) · [No GPU counters or adapter not matched (`gpu FAIL`)](#no-gpu-counters-or-adapter-not-matched) · [No server found or unreachable (`server FAIL`)](#no-server-found-or-server-unreachable) · [Several llama-servers running](#several-llama-servers-running) · ["llama-server 0 MiB", a missing RAM row, or "no memory map"](#llama-server-0-mib-a-missing-ram-row-or-no-memory-map) · [`/slots` endpoint is off (`HTTP 404` / `HTTP 501`)](#slots-endpoint-is-off) · [`unexpected /slots reply`](#unexpected-slots-reply) · [Server or `/slots` authentication error (`HTTP 401` / `HTTP 403`)](#server-or-slots-authentication-error) · [Missing per-model log, unrecognised format or missing timestamps](#missing-per-model-log-unrecognised-format-or-missing-timestamps) · [Router log not found](#router-log-not-found) · [State folder not writable (`state FAIL`)](#state-folder-not-writable-or-unavailable) · [`llama-fit-params` not found](#llama-fit-params-not-found) · [Helper programs and server paths are off](#helper-programs-and-server-paths-are-off) · [`llama-fit-params` skipped or off (Memory page)](#llama-fit-params-skipped-or-off-memory-page) · [A reported file path was ignored](#a-reported-file-path-was-ignored) · [Informational notices](#informational-notices) · [Unknown threshold or collector error](#unknown-threshold-or-collector-error) · [Garbled display or boxes in the charts](#garbled-display-or-boxes-in-the-charts)

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

## "llama-server 0 MiB", a missing RAM row, or "no memory map"

**Symptom:**

- A model is reported as `loaded` or `loading`, but the **Live** page shows the `llama-server` shared row reading `0 MiB` (with its GPU figures at 0), and the **Memory** page has no per-model RAM row.
- The **Memory** and/or **Tensors** page shows:
	```text
	could not analyse: the server did not report this model's command line (/models has no status.args) and no llama-server process was found for it
	```
- A notice prints:
	```text
	llama-server: no process named llama-server* found, so its GPU and RAM figures read 0 (a renamed or wrapped server is not recognised)
	```

**Cause:** `win-llmtop` measures GPU and RAM by walking the `llama-server` process address space, and it finds that process by matching an executable name that starts with `llama-server`. The figures read 0 and no memory map is built when either:

1. no process whose name starts with `llama-server` is running — for example the binary was renamed, is launched through a wrapper or launcher, or runs inside a container or WSL whose process name is not visible in the Windows process list — so the server answers over HTTP but its process is not recognised; or
2. a router serves the model and its `/models` entry has no `status.args` (no command line reported), and no matching process can be found, so `win-llmtop` has neither a command line to parse nor a process to walk.

In both cases the model still appears in the preset list and `m` still cycles over it; only the per-process figures are unavailable.

**Fix:**

- If the binary was renamed or wrapped, run it as `llama-server` (or `llama-server-<suffix>`) so its process name is recognised, and run `win-llmtop` on the same machine that hosts the process.
- If a router is in use, make sure the router reports each preset's command line in `/models` (`status.args`), or run `llama-server` as a normal (non-renamed) local process.
- To diagnose, run `win-llmtop --check` and send its full output together with a `/models` response (with any API keys or other secrets removed).
- If a page still fails to render, the last traceback is written to `llmtop-error.log` in the state folder (`%LOCALAPPDATA%\win-llmtop` by default, or the path passed to [`--state-dir`](llmtop.1.md#options) / `LLMTOP_STATE_DIR`).

---

## `/slots` endpoint is off

**Symptom:**

- An info alert:
	```text
	/slots for <preset> returned HTTP 404: the server's /slots endpoint is off (it is on unless the server was started with --no-slots)
	```
	(or `HTTP 501`).
- `win-llmtop --check` shows `/slots error: HTTP 404` on the `server` line, and the **Live** page preset section shows `slot data unavailable: HTTP 404`.

**Cause:** The `/slots` endpoint is on by default in `llama-server` (build b11669). `HTTP 404` or `HTTP 501` means the server was started with `--no-slots`, which disables the endpoint that reports in-flight prefill/decode progress, ETA and live tokens/s.

**Fix:** Remove `--no-slots` from your `llama-server` command line (or from each preset's arguments in router mode) and restart `llama-server`. See [`usage.md#live-slot-telemetry`](usage.md#live-slot-telemetry).

---

## `unexpected /slots reply`

**Symptom:**

- An info alert:
	```text
	/slots for <preset> returned unexpected /slots reply
	```
- The **Live** page preset section shows `slot data unavailable: unexpected /slots reply`, and `win-llmtop --check` shows `/slots error: unexpected /slots reply` on the `server` line.

**Cause:** The `/slots` endpoint answered, but its reply was not a list of slot objects. This usually means the watched URL is not the intended `llama-server` — a proxy or a different program is listening on the port, or the server is a different build whose `/slots` reply changed shape.

**Fix:** Verify the watched URL is the intended `llama-server`, passing [`--router`](llmtop.1.md#options) `<URL>` to be explicit. `win-llmtop` reports the error and keeps sampling rather than stopping.

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

**Cause:** `llama-server` requires a Bearer API key for HTTP requests. When started with `--api-key`, `/models` (and everything except `/health`) requires authentication, not only `/slots`. `win-llmtop` sends its key only to a URL you gave with `--router` or one read from a running `llama-server`'s command line; the probe of the default address `http://127.0.0.1:8080` carries no key, and when it is refused and a key is set `win-llmtop` looks for a running `llama-server` whose command line names that address. If none does, the message reads `HTTP 401 (no API key was sent to the default address: pass --router with --api-key or set LLMTOP_API_KEY / LLAMA_API_KEY)` (the alert says `HTTP 401: pass --router with --api-key or LLAMA_API_KEY`).

**Fix:** Pass [`--api-key`](llmtop.1.md#options) `<KEY>`, or set `LLMTOP_API_KEY` or `LLAMA_API_KEY` in your environment before starting `win-llmtop`; when the message mentions the default address, also pass [`--router`](llmtop.1.md#options) `http://127.0.0.1:<port>` with the server's URL. A server that answers with a redirect is reported as an error (`HTTP Error 302`): pass the URL it redirects to as `--router`.

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
	llama-fit-params   --    not found next to the running server (looked in <dir>)
	```

**Cause:** `llama-fit-params.exe` is not in the same folder as the running `llama-server.exe` that serves the watched URL. `win-llmtop` looks only there: not on `PATH`, not in the current folder, and not in any folder a server reports. Without it, `win-llmtop` still derives model weights, KV cache and recurrent state from the GGUF header, but cannot cross-check them against `llama.cpp`'s own projection or show `llama.cpp`'s compute-buffer figure.

**Fix:** Keep `llama-fit-params.exe` (included in official `llama.cpp` Windows release archives) in the same directory as `llama-server.exe`. If the line reads `off: ...` instead, see [Helper programs and server paths are off](#helper-programs-and-server-paths-are-off).

---

## Helper programs and server paths are off

**Symptom:**

- A notice prints:
	```text
	no helper program is run and no file path from the server is used: <reason>
	```
- `win-llmtop --check` prints:
	```text
	llama-fit-params   --    off: no helper program is run and no file path from the server is used, because <reason>
	```
- The **Memory** and/or **Tensors** page shows:
	```text
	could not analyse: the model file and the helper programs are not used because <reason>
	```

**Cause:** `win-llmtop` runs `llama-server --help` and `llama-fit-params`, and opens the model, draft-model and log-file paths a server reports, only for the server it can show is this machine's own `llama-server`: a loopback URL (`127.0.0.1`, `::1`, `localhost`) whose port is listened on by exactly one process named `llama-server*`, with a readable image path. `<reason>` says which part failed:

- `the server at <URL> is not at a loopback address`: the server is on another machine, or `--router` names a host rather than a loopback address.
- `no single process listens on port <N> of the loopback address`: nothing listens there (the server just stopped, or runs in a container or WSL whose sockets Windows does not list), or two processes do.
- `the process listening on port <N> (pid <PID>) is not named llama-server*`: a wrapper, proxy or renamed binary is in front of the server.
- `the image path of llama-server (pid <PID>) could not be read`: `llama-server` runs under another account with restricted permissions.

The server is still monitored over HTTP; only the memory map, the defaults read from `--help`, the projection and the log paths a server reports are unavailable.

**Fix:** Run `win-llmtop` on the machine that runs `llama-server`, give [`--router`](llmtop.1.md#options) a loopback URL, and run the server as a plain `llama-server` process (or `llama-server-<suffix>`) from your own account. Per-model logs can still be given with [`--log-dir`](llmtop.1.md#options) and the router log with [`--router-log`](llmtop.1.md#options), which are never checked.

---

## `llama-fit-params` skipped or off (Memory page)

**Symptom:**

- The **Memory** page shows a yellow line:
	```text
	▲ <reason>: weights, KV and state come from the GGUF and the flags alone
	```
	where `<reason>` is `llama-fit-params skipped: resident VRAM is within 256 MiB of the OS budget; retrying in 60 s` (the default [`--fit-params`](llmtop.1.md#options) `auto`) or `llama-fit-params is off (--fit-params never)` (when [`--fit-params`](llmtop.1.md#options) `never` was passed).

**Cause:**

- `llama-fit-params skipped: ...`: with [`--fit-params`](llmtop.1.md#options) `auto`, `win-llmtop` skips the helper while the resident VRAM is within `fit_headroom_mib` (`256` MiB by default, set with [`--threshold`](llmtop.1.md#options) `fit_headroom_mib=N`) of the OS budget, because starting the helper's GPU backend uses VRAM the card may not have; it retries after 60 s.
- `llama-fit-params is off (--fit-params never)`: [`--fit-params`](llmtop.1.md#options) `never` was passed, so the helper is never run.

**Fix:**

- The `skipped` case is transient: the helper runs automatically once the VRAM headroom clears, and [`--plan`](llmtop.1.md#options) always runs it. No action is needed.
- The `off` case is intentional: remove `--fit-params never` from your command line unless you deliberately want to skip the helper.

---

## A reported file path was ignored

**Symptom:**

- A notice prints:
	```text
	<preset>: the model path is ignored: it is not an absolute path on a local drive
	```
	(or `the draft model path`, `the log file path`; for the router's own log the name before the colon is `router`), and the **Memory** page shows `could not analyse: the model path is not an absolute path on a local drive` when it is the model.

**Cause:** The model, draft-model and log-file paths a server or its command line reports are used only when they are absolute on a local drive letter (`X:\...` or `X:/...`). A network path (`\\host\share\...`, `//host/...`), a `\\?\` or `\\.\` path, a device name such as `NUL`, a relative path and a path with a stream or wildcard are skipped. The rest of the page still works: a skipped draft model is left out of the memory map, and a skipped log file means that preset has no log (see [Missing per-model log](#missing-per-model-log-unrecognised-format-or-missing-timestamps)).

**Fix:** Start the preset with a full local path for its model (for example `D:\models\name.gguf`). For a log on a network share, pass [`--log-dir`](llmtop.1.md#options) or [`--router-log`](llmtop.1.md#options) yourself.

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

**Fix:** Check the valid threshold names (`spill_mib`, `burst_mib`, `avail_warn`, `avail_crit`, `commit_warn`, `commit_crit`, `hold_s`, `clear_s`, `near_budget_mib`, `at_budget_mib`, `idle_headroom_mib`, `fit_headroom_mib`, `rival_gpu_pct`, `card_full_pct`, `pagefile_pct`, `ws_drop_mib`, `burst_window_s`, `load_grace_s`, `prompt_gap_s`, `hz`, `history_hours`) documented in [`llmtop.1.md#alerts`](llmtop.1.md#alerts).

---

## Garbled display or boxes in the charts

**Symptom:** Trend charts show empty boxes (`▯` / `?`) instead of block characters, or raw ANSI escape codes appear on screen.

**Cause:** The classic Windows console (`conhost.exe`) with a legacy raster font lacks the one-eighth block characters (`▁`–`▇`) and the braille dots used by the finer chart styles, or virtual terminal processing is disabled in the console host.

**Fix:**

- Run `win-llmtop` in **Windows Terminal** (`wt.exe`) with a Unicode monospace font such as Cascadia Mono (see [`install.md#recommended-terminal`](install.md#recommended-terminal)); it draws the finest `braille` charts.
- In the classic console, `win-llmtop` now defaults to the `coarse` style (`▄`/`█`), which the raster font draws. If you still see boxes, press `s` or pass [`--graph-style`](llmtop.1.md#options) `coarse` to force it.
- Pass [`--no-color`](llmtop.1.md#options) to turn off ANSI colour escape sequences.
