# Using win-llmtop

**Contents:** [First run (`--check`)](#first-run) · [Pages and keyboard controls](#pages-and-keyboard-controls) · [Getting the most from it](#getting-the-most-from-it) · [Snapshots and VRAM planning](#snapshots-and-vram-planning) · [Where it keeps its files](#where-it-keeps-its-files)

Every `win-llmtop` command-line option mentioned below links to its entry in the [`win-llmtop(1)` manual (`llmtop.1.md#options`)](llmtop.1.md#options).

---

## First run

Run `win-llmtop` with [`--check`](llmtop.1.md#options):

```shell
win-llmtop --check
```

It inspects the prerequisites and every data source it can reach, prints one status line per check, and exits `0` when the server is reachable and either GPU telemetry is present or the machine has no GPU adapters (`1` otherwise):

| Line | What it checks |
| :--- | :--- |
| `windows` | The Windows build and the Python version and bitness; `ok` on 64-bit Windows |
| `gpu` | The DXGI adapters (name, VRAM, the OS budget), which adapter it is watching ([`--gpu`](llmtop.1.md#options)), and whether the PDH GPU counters are present (or `--  no GPU adapters (CPU-only)` on machines without a GPU) |
| `processes` | The `llama-server` processes it found (PID and executable path) |
| `server` | The URL it uses ([`--router`](llmtop.1.md#options)) and how it found it, the mode (`router` or `single`), how many models the server reports, and whether `/models` and `/slots` are reachable ([`--api-key`](llmtop.1.md#options)) |
| `logs` | For each model, the log path and whether it exists and has timestamps; and the router log ([`--log-dir`](llmtop.1.md#options), [`--router-log`](llmtop.1.md#options)) |
| `llama-fit-params` | Where it found the `llama-fit-params` helper (next to the server executable, on `PATH`, or not found) |
| `state` | The state folder ([`--state-dir`](llmtop.1.md#options)) and whether it is writable ([`--no-persist`](llmtop.1.md#options)) |
| `notice` | Any active notices, such as single-model mode, several llama-server instances found, a server found by discovery, a missing log, or a log without timestamps |

If any line prints `FAIL` or an unexpected notice, see [`troubleshooting.md`](troubleshooting.md).

---

## Pages and keyboard controls

Run `win-llmtop` with no arguments for the interactive full-screen view (sampled at [`--hz`](llmtop.1.md#options), `4.0` Hz by default).

### Pages

| Key | Page | What it shows |
| :--- | :--- | :--- |
| `1` | **Live** | Active alerts; GPU VRAM bar against the OS budget, `llama-server` resident and shared memory (`llama-server shared`: expected share and what is beyond it), engine load and other apps' VRAM; system RAM, commit charge, paging and CPU load (`llama-server` cores, clock, processor queue) with trend charts; running request phase, progress, ETA and tokens/s; recent turns or recent prompts (`t` toggles; `▸` marks the first turn of each prompt) and events |
| `2` | **Memory** | For one preset (`m` cycles presets, loaded or unloaded): VRAM by component (weights, KV cache, recurrent state, compute buffers, draft, overhead) against the OS budget; `llama-server` host RAM breakdown; layer placement overview |
| `3` | **System** | CPU model, core and thread counts, per-core load, L1/L2/L3 cache topology, and CPU load and clock trend charts; system RAM by consumer (`llama-server`, other processes, kernel remainder, file cache, free) with top working sets; page file size, use, peak and commit limit; RAM and paging trend charts |
| `4` | **Requests** | Per-preset prompt and turn volume, busy time, loads and issues; median, slow-10% and sample-count speeds; prefill and decode t/s by context depth (`<8k`, `8–32k`, `32–64k`, `64k+`); per-turn charts; and every prompt with nested turns, each with its cache hit and draft acceptance (`m` filters by preset, `t` toggles nested turns) |
| `5` | **Events** | Threshold alerts raised and cleared, model loads, request queueing, preset evictions, prompt-cache misses, request errors and context truncation. Each event is a short headline; `f` filters by severity level and `d` toggles the detail line(s) (the comparisons, thresholds and deductions the headline leaves out) |
| `6` | **Tensors** | Every layer (`t` toggles individual tensors) with GGUF type, shape, size, `■ GPU` / `■ CPU` badge, and the flag or rule that placed CPU tensors (`d` filters by device) |

### Keyboard controls

| Key | Where it works | Action |
| :--- | :--- | :--- |
| `1`–`6`, `Tab`, `←` `→` | All pages | Switch between the six pages |
| `[` `]` (or `-` `+`) | All pages | Step the history time window (`1m`, `5m`, `15m`, `1h`, `6h`, `24h`; set the startup window with [`--window`](llmtop.1.md#options)) |
| `g` | All pages | Cycle trend chart height (`auto`, `1`, `2`, `3` or `4` rows) |
| `s` | All pages | Switch chart rendering between `braille` and `blocks` (startup default: [`--graph-style`](llmtop.1.md#options)) |
| `p` or `Space` | All pages | Pause or resume the screen redraw while background sampling continues |
| `↑` `↓`, `PgUp` `PgDn`, `Home` `End` | All except **Live** | Scroll long tables vertically |
| `m` | **Memory**, **Tensors** | Cycle through presets (loaded first, then unloaded) |
| `m` | **Requests** | Filter the Requests page by preset |
| `t` | **Live** | Switch the bottom table between recent turns and recent prompts |
| `t` | **Requests** | Show or hide individual turns nested under each prompt |
| `f` | **Events** | Cycle the event severity filter (`all`, `warn+`, `crit`) |
| `d` | **Events** | Toggle the detail line(s) under each event (the comparisons, thresholds and deductions the headline leaves out) |
| `t` | **Tensors** | Switch between per-layer summary and every individual tensor |
| `d` | **Tensors** | Filter tensors by device (`all`, `GPU`, `CPU`) |
| `q` or `Esc` | All pages | Quit `win-llmtop` |

### Reading the Requests page

- **Turns, prompts and context:** A **turn** is one `llama-server` request. A **prompt** is a run of consecutive turns on the same preset and slot that continue one conversation without a long pause; its prefill and decode rates are time-weighted (`Σpn / Σpms` and `Σtn / Σtms`). **Context** is the tokens in the KV cache after a turn (`cached + pn + tn`); prefill speed is bucketed by the depth when prefill ends (`cached + pn`). A turn starts a new prompt when it is an LRU slot pick that evicted tokens (`evicted > 0`) or kept less than half of the slot's cached tokens (`f_keep < 0.5`), when the idle gap since the previous turn on that slot is at least `prompt_gap_s` (`30` s by default, configurable via [`--threshold`](llmtop.1.md#options) `prompt_gap_s=N`), or when the model was reloaded between them. A compaction — the agent's conversation grew to the context limit, was summarised, and the first turn after it restarted from the summary — continues the same prompt even though the context shrinks, and the prompt gets a `compacted` note (`compacted ×2` when it happens twice). Press `t` on **Requests** to expand or collapse the turns inside each prompt.
- **Colours:** Prefill and decode t/s values in the prompt and turn tables are coloured against the **same preset's median at a similar context depth** (`<8k`, `8–32k`, `32–64k`, `64k+`), from ≥ 3 qualifying turns: blue ≥ 120%, green 80–120%, yellow 50–80%, red < 50%. A turn is compared against the median for its own bucket. A prompt is compared against the time its turns would usually take — each turn's new (or generated) tokens over that turn's own bucket median, summed, over the same turns' time — so a long prompt is not judged at its final depth; it is dim grey when the turns with a reference hold under half its new (or generated) tokens. Prefill is compared only over turns with `256+` new tokens and decode only over `20+` generated tokens; anything not compared is dim grey. In the context-depth bucket tables, the presets with 3+ turns in a column are ranked fastest first and grouped into near-tie clusters (a cell stays in a cluster while within 5% of that cluster's fastest value), and every cell in a cluster takes the colour its fastest member would get by rank: the fastest clusters are green, the slowest red, the middle yellow; cells with 1-2 turns and `-` cells are dim grey, and a column with a single ranked preset is plain. The `hit` and `draft` columns are never coloured: a prompt's first turn is often a `0%` hit by nature, and the `cache reset` note already marks real misses.
- **Notes:** Row notes show exceptions only: `cache reset Nk` (an LRU pick dropped a slot holding `Nk` tokens), `cut off` (`llama-server` truncated the prompt), `shared +X.X GiB` (`llama-server` held that much shared GPU memory over its baseline during the turn), and `resized` (compute buffers were reallocated).
- **Cache hit (`hit`):** the share of a turn's prompt tokens served from the slot's cache (`cached / (cached + new)`); a prompt's is token-weighted across its turns. A cache miss shows as the `cache reset` note.
- **Draft acceptance (`draft`):** the share of speculative draft tokens accepted; a prompt's is weighted by draft tokens (`Σaccepted / Σgenerated`; for older history without counts, the per-turn rates are weighted by generated tokens).

---

## Getting the most from it

`win-llmtop` shows whatever telemetry is available. Configuring three things on `llama-server` and `win-llmtop` unlocks the complete view.

### Per-model logs and timestamps

Pass `--log-file` (in router mode, each preset's `log-file`) and `--log-timestamps` to `llama-server`, and point [`--log-dir`](llmtop.1.md#options) (or `LLMTOP_LOG_DIR`) at the log directory if the preset does not specify an full path so `win-llmtop` can open `<log-dir>\<preset>.log`.

- **What it unlocks:** finished turn and prompt history on **Live** and **Requests** — per-turn prefill and decode speeds, prompt cache hit tokens, speculative draft acceptance rates, and wall-clock completion times.
- **What you lose without `--log-file` / [`--log-dir`](llmtop.1.md#options):** the request history is empty and `win-llmtop` prints a `no log file for preset <name>` notice.
- **What you lose without `--log-timestamps`:** log lines carry no timestamps, so turn durations and speeds are still parsed, but entries have no wall-clock times and `win-llmtop` prints a notice suggesting `--log-timestamps`.

### Live slot telemetry

Start `llama-server` with `--slots` enabled (and pass [`--api-key`](llmtop.1.md#options) or set `LLMTOP_API_KEY` / `LLAMA_API_KEY` if the server requires authentication).

- **What it unlocks:** live in-flight request progress from the server's `/slots` endpoint — whether a slot is in `PREFILL` or `DECODE`, how many prompt tokens have been processed, the prefill ETA, live tokens/s, and current KV cache fill.
- **What you lose without `--slots`:** `/slots` is disabled (`HTTP 404` or `HTTP 501`), so `win-llmtop` still shows GPU memory, host RAM and finished requests from logs, but cannot show the currently running request and raises a warning to start the server with `--slots`.

### Router event log

Point [`--router-log`](llmtop.1.md#options) directly at the router's log file, or pass [`--log-dir`](llmtop.1.md#options) pointing at the directory where the router writes its `llama-server-<date>-<time>.log` files.

- **What it unlocks:** router lifecycle events on **Live** and **Events** — model spawns and loads, queued requests, LRU preset evictions, and historical session start timestamps.
- **What you lose without [`--router-log`](llmtop.1.md#options) or [`--log-dir`](llmtop.1.md#options):** `win-llmtop` cannot find the router log and router-level load, queue and eviction events are omitted.

---

## Snapshots and VRAM planning

- **Single snapshot ([`--once`](llmtop.1.md#options)):** `win-llmtop --once` collects four warmup samples (so rate counters have two points) and prints the **Live** page once to standard output. Combine with [`--page`](llmtop.1.md#options) (`live`, `memory`, `system`, `requests`, `events`, `tensors`, or `all` for every page), [`--detail`](llmtop.1.md#options) to show each event's detail line(s) on the Events page, [`--width`](llmtop.1.md#options) to fix the column width, and [`--no-color`](llmtop.1.md#options) when piping to a file.
- **VRAM projection ([`--plan`](llmtop.1.md#options)):** `win-llmtop --plan` inspects every preset's GGUF header and runs `llama-fit-params` to project weights, KV cache and recurrent state, compute buffers, draft model and learned VRAM overhead against the DXGI OS budget without loading any model. Pass a preset name (`win-llmtop --plan <PRESET>`) to print the full **Memory** page map for that preset.

---

## Where it keeps its files

By default, `win-llmtop` stores persisted history in `%LOCALAPPDATA%\win-llmtop\` (override with [`--state-dir`](llmtop.1.md#options) or `LLMTOP_STATE_DIR`):

- `samples-<date>.jsonl` — one telemetry sample every 5 s (covering [`--history-hours`](llmtop.1.md#options) in memory) plus alert transitions ([`--spill-mib`](llmtop.1.md#options), [`--threshold`](llmtop.1.md#options)), retained for 14 days so trend charts and the event list span restarts.
- `requests.jsonl` — finished request records, preserved across restarts because `llama-server` truncates and rewrites a preset's log file every time that model loads.
- `calibration.json` — the per-configuration VRAM overhead and idle shared-memory baseline learned when a single model is resident.

Pass [`--no-persist`](llmtop.1.md#options) to disable all disk writes (persistence is also disabled automatically for [`--check`](llmtop.1.md#options), [`--once`](llmtop.1.md#options) and [`--plan`](llmtop.1.md#options)). Read-only runs still replay whatever history is already present in the state folder.
