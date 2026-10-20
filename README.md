# win-llmtop

A live terminal panel for llama.cpp's `llama-server` on Windows. It shows where the memory goes while a model runs: dedicated and shared GPU memory per process against the OS budget, system RAM, commit charge and the page file, the running request's progress and speed, the history of finished requests, and the model's memory map. It is read-only: it reads the server's API and logs and Windows' performance counters, and never changes the server.

## Requirements

- Windows 10 version 1709 or later (the per-process GPU counters appear in that release);
- 64-bit Python 3.11 or later;
- no other packages (the standard library only);
- a llama.cpp `llama-server` — in router mode or serving a single model — on the same machine.

## Install

```shell
pipx install win-llmtop
```

(recommended: an isolated install with `win-llmtop` on PATH), or `uv tool install win-llmtop`, or `py -m pip install win-llmtop`.

Getting 64-bit Python on Windows: python.org's Windows installer, or `winget install Python.Python.3.13`.

From source: `py -m pip install git+https://github.com/insane-kane/win-llmtop`, or clone the repository and run `py llmtop.py`.

A signed standalone `win-llmtop.exe` and a winget package are planned for 0.1.1.

## First run

Run `win-llmtop --check`. It reports on the prerequisites and the sources it can reach, one line each, and exits 0 when the server is reachable and the GPU telemetry is present, 1 otherwise:

| Line | What it checks |
| :--- | :--- |
| `windows` | the Windows build and the Python version and bitness; ok on 64-bit Windows |
| `gpu` | the DXGI adapters (name, VRAM, the OS budget), which adapter it is watching, and whether the PDH GPU counters are present |
| `processes` | the llama-server processes it found (pid and executable) |
| `server` | the URL it uses and how it found it, the mode (router or single), how many models the server reports, and whether `/slots` is reachable |
| `logs` | for each model, the log path and whether it exists and has timestamps; and the router log |
| `llama-fit-params` | where it found the `llama-fit-params` helper (next to the server, on PATH, or not found) |
| `state` | the state folder and whether it is writable |
| `notice` | any notices, such as single-model mode, a server found by discovery, a missing log, or a log without timestamps |

## Getting the most from it

llmtop shows whatever it can reach. A few flags on the llama-server, and options on llmtop, unlock more:

- `--log-file` (one per model; in router mode, the preset's `log-file`) and `--log-timestamps`: these give llmtop the per-model log it reads for the request history — per-request timings, draft acceptance, prefix reuse. Without a log file llmtop can find (give `--log-file`, or `--log-dir` so it can build `<log-dir>\<preset>.log`), the request history is empty and llmtop prints a notice. Without `--log-timestamps` the log lines carry no timestamps, so the history keeps its relative timings but has no wall-clock times, and llmtop prints a notice suggesting the flag.
- `--slots` enabled: the running request's live progress — its phase, how far prefill or decode has got, the current t/s and the context fill — is read from the server's `/slots` endpoint. Without it the endpoint is off, so llmtop shows the memory and system figures but not the in-flight request, and raises a warning telling you to start the server with `--slots`.
- For the router's own events — model loads, queueing, evictions — point `--router-log` at the router's log file, or `--log-dir` at the folder it writes its `llama-server-<date>-<time>.log` files into. Without either, llmtop cannot find the router log and those events are not shown.

## Usage

Run `win-llmtop` for the live view. `win-llmtop --once` prints one snapshot and exits; `--once --page all` prints every page. `win-llmtop --plan [PRESET]` projects each preset's VRAM against the OS budget without loading anything.

Keys: `1`–`6`, `Tab`, or `←` `→` switch pages; `[` `]` change the history window; `g` sets the graph height; `s` switches the graph style; `↑` `↓` `PgUp` `PgDn` `Home` `End` scroll every page except Live; `p` or `Space` pauses the display (collection continues); `q` quits. On the Memory and Tensors pages `m` cycles models; on Requests `m` filters by preset; on Events `f` filters by level; on Tensors `t` switches between layers and every tensor and `d` filters by device.

| Page | Content |
| :--- | :--- |
| **Live** | alerts; GPU and RAM with trend charts; the running request's phase, progress and t/s; recent requests and events |
| **Memory** | VRAM by component against the OS budget, llama-server's own RAM, the layer overview |
| **System** | system RAM by consumer, the page file, RAM and paging trends |
| **Requests** | per-preset volume and speed, prefill by prompt size, decode by context depth, every request |
| **Events** | alerts, model loads, queueing, evictions, slot picks, errors |
| **Tensors** | every layer or tensor with its type, shape, size and device |

Options, with their environment variables and defaults:

| Option | Environment variable | Default |
| :--- | :--- | :--- |
| `-h`, `--help` | — | show the help and exit |
| `--check` | — | off |
| `--once` | — | off |
| `--page` | — | `live` |
| `--plan [PRESET]` | — | off |
| `--window` | — | `5m` |
| `--router URL` | `LLMTOP_ROUTER` | find it |
| `--log-dir DIR` | `LLMTOP_LOG_DIR` | (none) |
| `--router-log PATH` | — | (none) |
| `--state-dir DIR` | `LLMTOP_STATE_DIR` | `%LOCALAPPDATA%\win-llmtop` |
| `--api-key KEY` | `LLMTOP_API_KEY` or `LLAMA_API_KEY` | (none) |
| `--gpu INDEX\|NAME` | — | the adapter the model runs on |
| `--hz HZ` | — | `4.0` |
| `--history-hours H` | — | `24` |
| `--spill-mib N` | — | `256` |
| `--threshold NAME=VALUE` | — | (none) |
| `--no-persist` | — | off |
| `--graph-style` | — | braille in Windows Terminal, blocks otherwise |
| `--no-color` | — | off |
| `--width N` | — | the terminal width |

## Where it keeps its files

By default, `%LOCALAPPDATA%\win-llmtop\` (override with `--state-dir` or `LLMTOP_STATE_DIR`). It holds:

- `samples-<date>.jsonl` — one sample per 5 s plus alert events, 14 days kept, so the sparklines and the event list span restarts;
- `requests.jsonl` — finished requests, kept because llama-server rewrites a model's log each time that model loads;
- `calibration.json` — the VRAM overhead learned per configuration.

`--no-persist` turns the writing off; a read-only run still replays whatever is already there.

## Limits

- Which allocation or tensor is in shared GPU memory cannot be told from the counters; only how much.
- The OS budget comes from DXGI for llmtop's own process; the model's budget may differ.
- GPU temperature and power are not exposed by Windows' counters and are not shown.
- The address-space walk shows what kind of memory the process holds, not which allocation is the prompt cache or a checkpoint; the log carries no cache sizes at its default verbosity, so only the configured limits are shown.
- Prefill progress advances once per batch (`--batch-size`), so the live prefill rate is the rate over the last completed batch.
- KV and state figures are derived; they were checked against `llama-fit-params` on a hybrid model and a sliding-window mixture-of-experts model, and on any architecture the Memory page reports whether they agree.
- Fixed assumptions: llama.cpp's tensor naming, its offload rules, the GGUF type sizes, and the log line formats of build b11669. The alert thresholds, the colour thresholds for t/s and the size buckets of the Requests page are display or policy choices, not measurements.

## Licence

GNU General Public License, version 3 or (at your option) any later version (`GPL-3.0-or-later`); see `LICENSE`.
