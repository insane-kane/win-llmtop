# win-llmtop(1) — Live Panel for the Local llama-server

**Contents:** [NAME](#name) · [SYNOPSIS](#synopsis) · [OPTIONS](#options) · [DESCRIPTION](#description) · [Where every figure comes from](#where-every-figure-comes-from) · [Shared GPU memory](#shared-gpu-memory) · [Alerts](#alerts) · [EXIT STATUS](#exit-status) · [FILES](#files) · [LIMITS](#limits) · [SEE ALSO](#see-also)

## NAME
`win-llmtop` — Terminal panel for llama.cpp's `llama-server` on Windows (router mode or a single model): what is loaded, what it is doing, how fast, where the VRAM and RAM go, and when Windows starts paging GPU memory into system RAM.

## SYNOPSIS
```shell
win-llmtop [--window 1m|5m|15m|1h|6h|24h] [--hz N] [--no-persist] [--no-color]
win-llmtop --check
win-llmtop --once [--page live|memory|system|requests|events|tensors|all] [--graph-style braille|blocks]
win-llmtop --plan [PRESET]
```

## OPTIONS
Options, with their environment variables and defaults:

| Option | Environment variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `-h`, `--help` | — | show the help and exit | show the help message and exit |
| `--check` | — | off | report on prerequisites and reachable sources, then exit |
| `--once` | — | off | print one snapshot and exit |
| `--page {live,memory,system,requests,events,tensors,all}` | — | `live` | page for `--once` |
| `--detail` | — | off | with `--once --page events`, show each event's detail line(s); the `d` key does this in the interactive Events page |
| `--plan [PRESET]` | — | off | project VRAM per preset against the OS budget |
| `--window {1m,5m,15m,1h,6h,24h}` | — | `5m` | initial history window |
| `--router URL` | `LLMTOP_ROUTER` | find it | `llama-server` URL |
| `--log-dir DIR` | `LLMTOP_LOG_DIR` | (none) | directory of `llama-server` logs |
| `--router-log PATH` | — | (none) | router log file path |
| `--state-dir DIR` | `LLMTOP_STATE_DIR` | `%LOCALAPPDATA%\win-llmtop` | directory for persisted history and calibration |
| `--api-key KEY` | `LLMTOP_API_KEY` or `LLAMA_API_KEY` | (none) | Bearer token for `llama-server` |
| `--gpu INDEX\|NAME` | — | the adapter the model runs on | adapter index or name substring |
| `--hz HZ` | — | `4.0` | sampling and redraw rate |
| `--history-hours HISTORY_HOURS` | — | `24` | hours of per-second history kept in memory and replayed |
| `--spill-mib SPILL_MIB` | — | `256` | excess shared GPU memory (MiB) that raises an alert |
| `--threshold NAME=VALUE` | — | (none) | override an alert or grouping threshold (see `CFG` in `llmtop_lib/state.py`); repeatable |
| `--no-persist` | — | off | do not mirror history to disk |
| `--graph-style {braille,blocks}` | — | braille in Windows Terminal, blocks otherwise | braille line plots, or block-character charts if the font lacks braille |
| `--no-color` | — | off | disable ANSI colour escapes |
| `--width WIDTH` | — | the terminal width | line width for `--once` and `--plan` |

`--router` finds the server when no URL is given: it tries the default `http://127.0.0.1:8080`, then the host and port read from each running llama-server's command line, and says how it found the one that answers. When several independent `llama-server` instances are running, pass `--router` to choose which one to watch. `--api-key` (or `LLMTOP_API_KEY` / `LLAMA_API_KEY`) passes a Bearer token for HTTP requests; a server started with `--api-key` requires it for `/models` and `/slots`. `--log-dir` is the folder the per-model logs and the router log live in; `--router-log` names the router log file directly. `--state-dir` is the folder for the persisted history and calibration.

## DESCRIPTION
The tool is read-only. It talks to the llama-server API (router or single-model), reads the llama-server logs, and reads Windows performance counters; it never changes the server. Windows only (Python 3.11+, standard library only).

It samples at 4 Hz. The server's `/slots` endpoint is polled at that rate only while a request is running (every 3 s when idle) because the router logs every proxied request.

### Single-model servers
A `llama-server` serving a single model directly, rather than as a router, is detected from the `/models` response (its entries carry no `status` object, which a router adds to each preset) and runs in single-model mode: a `single-model mode` notice is shown, the header says `server up` rather than `router up`, `/slots` is polled without a model name, and `win-llmtop` matches the command-line flags and log of the process whose `--host` and `--port` correspond to the watched URL. Everything else — the pages, the alerts, the memory figures — works the same.

### Notices
Notices are grey `· ` lines that say why a figure is missing or a source is absent, not what to do about it. They are printed at the top of the Live page (at most four, the rest counted) and in full on the Events page. They cover: the state folder being unavailable; a server found by discovery rather than given, or unreachable (with the URLs tried); several independent `llama-server` instances found (with the watched URL); single-model mode; a `--gpu` that matched no adapter; more than one model process resident (VRAM calibration paused); a loaded preset with no log file; a log whose lines are read but none match the expected format; and a log with no timestamps (pass `--log-timestamps` for wall-clock history).

### Pages (keys `1`–`6`, `Tab`, or `←` `→`)
| Page | Content |
| :--- | :--- |
| **Live** | Alerts; GPU (VRAM bar with the OS budget marked, llama-server resident and shared memory, engine load, other apps' VRAM), RAM (in use, commit, file cache, page file) and CPU (load, llama-server cores, clock, processor queue) with framed trend charts; per loaded preset: phase, prefill or decode progress and live t/s, context fill, last draft acceptance, charts; recent turns or recent prompts (`t` toggles; `▸` marks the first turn of each prompt) and events. |
| **Memory** | For one preset (`m` cycles presets, loaded or not): VRAM by component against the OS budget, with what would fit; llama-server's own RAM as a short table (working set, commit by kind, mapped files, heap against the `cache-ram` and checkpoint ceilings); layer overview. |
| **System** | CPU (model, cores and threads, per-core load, L1/L2/L3 cache topology, load and clock trends); system RAM by consumer (llama-server, other processes, kernel, file cache, free) with the largest working sets; llama-server working set and private commit; page file size, use and peak with the commit limit; RAM and paging trends. |
| **Requests** | Per preset: volume (prompts, turns, busy time, input/new/cached/output tokens, cache hit, loads with mean load time, issues), speed (prefill and decode median, slow 10% and sample count `n`, draft acceptance and mean length), prefill and decode t/s by context depth (`<8k`, `8–32k`, `32–64k`, `64k+`; coloured by rank within each column, green fastest and red slowest), per-turn charts for qualifying turns, and every prompt newest first with its cache hit and draft acceptance (`m` filters by preset, `t` toggles nested turns). |
| **Events** | Alerts raised and cleared, model loads, queueing, evictions, prompt-cache misses, request errors, truncation. Each event is a short headline; `f` filters by level and `d` toggles the detail line(s) under each event (the comparisons, thresholds and deductions the headline leaves out). |
| **Tensors** | Every layer (`t` switches to every tensor) with type, shape, size and device as a ■ GPU / ■ CPU badge, plus a short tag where something deliberate placed it on the CPU; `d` filters by device. |

Keys: `[` `]` change the history window; `g` sets the graph height (auto, 1–4 lines); `s` switches the graph style; `↑` `↓` `PgUp` `PgDn` `Home` `End` scroll every page except Live; on Live `t` switches the bottom table between recent turns and recent prompts; on Events `f` filters by level and `d` toggles the detail line(s); on Tensors `d` filters by device; `p` or `Space` pauses the display (collection continues); `q` quits.

### Prompts, turns and notes
- **Turn:** one `llama-server` request.
- **Prompt:** a run of consecutive turns on the same preset and slot that continue one conversation without a long pause. A prompt's prefill and decode rates are time-weighted across its turns (`Σpn / Σpms` and `Σtn / Σtms`, not a mean of per-turn rates); its `total` is total server busy time (`Σms`). Turn *b* starts a new prompt after turn *a* on the same preset and slot when any of:
	1. *b* was an LRU slot pick that evicted held tokens (`sel == "LRU"` and `evicted > 0`), or kept less than half of the slot's cached tokens (`f_keep < 0.5`);
	2. the idle gap `b.wall0 - a.wall` is at least `prompt_gap_s` (`30` s by default, settable with `--threshold prompt_gap_s=N`);
	3. the model was reloaded between them (`session` differs).
	When wall-clock times are missing (logs without timestamps), relative `t0`/`t` timestamps are used within one session; if neither is usable, only the cache and reload rules apply.
- **Context:** tokens in the KV cache after a turn (`kv` = `cached + pn + tn`); a prompt row shows its last turn's. Prefill speed is bucketed by the depth when prefill ends (`cached + pn`).
- **hit:** the share of a turn's prompt tokens served from the slot's cache (`cached / (cached + new)`); a prompt's is token-weighted across its turns. A cache miss shows as the `cache reset` note. The column is never coloured: a prompt's first turn is often a `0%` hit by nature.
- **draft:** the share of speculative draft tokens accepted (`accepted / generated`); a prompt's is weighted by draft tokens (`Σaccepted / Σgenerated`; for older history without counts, the per-turn rates are weighted by generated tokens).
- **Notes** (exceptions only; normal cache reuse is the `context − new − gen` difference, shown by the `hit` share):
	- `cache reset Nk`: an LRU pick dropped a slot holding `Nk` tokens, so the whole prompt was recomputed;
	- `cut off`: the prompt did not fit and `llama-server` truncated it;
	- `shared +X.X GiB`: `llama-server` held `X.X GiB` of shared GPU memory over its baseline (≥ `spill_mib`, `256` MiB) during the turn;
	- `resized`: compute buffers were reallocated with changed sizes.

### Graphs
Each chart is a frame: labels and the current value sit beside the first line, the scale's high is printed on the top border and its low on the bottom border, and ticks on the borders mark quarters of the time window (the header says how far back the left edge is). The default style is a braille line trace (two samples per character, four dot rows per line); rate series are filled. `--graph-style blocks` or the `s` key uses block characters for fonts without braille. Each chart is scaled to what its series did in the window, so a steady value is a flat line at mid-height; charts anchored at zero (rates, context tokens) say so with a low of 0. A missing stretch of data is blank inside the frame. Auto height picks 3 lines, then 2, then a single line per chart, whichever leaves room for the request table. On the Requests page, the per-turn charts plot only qualifying turns (`256+` new tokens for prefill, `20+` generated for decode) without gaps, and the shared-memory chart is hidden when no turn in the window exceeded the baseline by `spill_mib`.

### Colour of t/s
Every prefill and decode t/s value in the prompt and turn tables is coloured against the **same preset's median at a similar context depth** (`<8k`, `8–32k`, `32–64k`, `64k+`; prefill buckets by context `cached + pn`, decode by `kv`), from ≥ 3 qualifying turns: blue is 120% or more of that median, green 80–120%, yellow 50–80%, red under 50%. A turn is compared against the median for its own bucket. A prompt is compared against the time its turns would usually take — each turn's new (or generated) tokens over that turn's own bucket median, summed, over the same turns' time — so a long prompt is not judged at its final depth; it is dim grey when the turns with a reference hold under half its new (or generated) tokens. Prefill is compared only for turns with `pn` ≥ 256 (smaller prefills are dominated by fixed overhead) and decode only for `tn` ≥ 20; anything not compared (below those cutoffs or fewer than three qualifying turns in the bucket) is dim grey, never the default terminal colour. In the bucket tables, the presets with 3+ turns in a column are ranked fastest first and grouped into near-tie clusters (a cell stays in a cluster while within 5% of that cluster's fastest value), and every cell in a cluster takes the colour its fastest member would get by rank: the fastest clusters are green, the slowest red, the middle yellow; cells with fewer turns and `-` cells are dim grey, and a column with a single ranked preset is plain.

### Where every figure comes from
llmtop shows what it can measure or read, and marks what it derives. It never states a cause: where a number needs interpreting, the page prints the numbers and the threshold, not an explanation.

| Shown | Source | Kind |
| :--- | :--- | :--- |
| VRAM resident, shared and committed per process; GPU engine load | PDH counters `GPU Process Memory`, `GPU Engine` | measured |
| VRAM capacity and the OS budget | DXGI `DedicatedVideoMemory` and `QueryVideoMemoryInfo`; the budget is reported for the calling process (llmtop), so llama-server's own may differ | measured |
| CPU load (total and per logical processor), effective clock, processor queue length | PDH counters `Processor Information` (`% Processor Utility`, `% Processor Performance`, `Processor Frequency`), `System\Processor Queue Length` | measured |
| CPU model name, core and thread counts (with P/E split), L1/L2/L3 cache topology | registry `CentralProcessor\0`, `GetLogicalProcessorInformationEx` (`RelationProcessorCore`, `RelationCache`) | measured |
| System RAM, file cache, free, modified, page-ins and page-outs | PDH `Memory` counters, `GlobalMemoryStatusEx` | measured |
| Page file size, use and peak | the kernel's page file table (`NtQuerySystemInformation`) | measured |
| Page file configured sizes and the commit maximum | registry `PagingFiles` | configuration |
| llama-server working set, commit, CPU time; every process's working set | psapi | measured |
| Private, mapped-file, image and reserved memory; mapped file names | an address-space walk (`VirtualQueryEx`) | measured |
| Slot state, token counts, prompt progress, sampler settings | router `/slots` | measured |
| Live prefill and decode t/s | change in the `/slots` counters between polls | derived by arithmetic |
| ETA | remaining prompt tokens divided by the rate over the last batch | derived estimate |
| Turn timings, draft acceptance, prefix similarity, kept share (`f_keep`), slot id and pick | llama-server log lines, kept in `requests.jsonl` because llama-server rewrites a preset's log each time that model loads | reported by llama.cpp |
| Cached tokens per turn | tokens in the slot at release, minus generated, minus newly evaluated | derived (within a token) |
| Model structure, tensor names, shapes, types and sizes | the GGUF header | read from the file |
| Flags of each preset | router `/models` status arguments | read from the router |
| Value of a flag the preset does not set | `llama-server --help`, which states each default | read from the runtime; if it cannot be read, a labelled assumption |
| Tensor placement | the flags with llama.cpp's offload rules (`--override-tensor`, `--cpu-moe`, `--n-gpu-layers`, input embeddings on the host) | derived, checked against `llama-fit-params` |
| KV cache and recurrent state size | tensor shapes and flags | derived, checked against `llama-fit-params` |
| MTP head KV | derived like the KV cache | estimate, marked `est.`, not checked |
| Compute buffers, host figures | `llama-fit-params` (run with `-lv 4`, allocates nothing) | reported by llama.cpp |
| Overhead on the Memory page | resident VRAM minus the projected rows, kept per configuration | measured on a load |
| Shared memory when the model became resident | the lowest shared figure of the llama-server process while resident VRAM is at least 90% of the projected rows | measured, per process |
| CPU-side buffer baseline | the lowest shared GPU memory seen on an earlier load of the same configuration while resident plus shared memory was at least 1 GiB under the budget; until then llama.cpp's host projection, labelled unverified | measured or projected |
| Alerts | thresholds applied to the figures above | thresholds in `CFG`; messages state values |
| Colour of t/s | this preset's median for turns at a similar context depth (`<8k`, `8–32k`, `32–64k`, `64k+`); a prompt is compared against the usual time of its turns, turn by turn | derived |

Some lines are deductions from those figures; each says so and gives its basis:

- *Working set fell*: commit that falls with it means the process released the memory (commit only falls when memory is released); commit that stays means the pages left the working set while staying committed, which is what reclaiming by Windows looks like.
- *Prompt cache bound*: private commit left after GPU resident, shared, and llama.cpp's projected host weights and buffers is compared with `cache-ram`, which `--help` defines as the maximum cache size. Above it, at least the excess is something other than prompt cache; within it, the figure is consistent with being all cache, which does not prove it.
- *Commit limit*: when headroom is below the critical threshold the alert says Windows fails commit requests beyond the limit, a documented rule.
- *Near the OS budget*: the shared-memory alert's detail states how near resident VRAM is to the OS budget (`near_budget_mib`), the condition under which Windows documents demoting allocations. The message states the condition, not that demotion happened.

The Memory page says whether the derived weights, KV and state agree with `llama-fit-params` (within 3%) and lists anything assumed, for example a flag left on *auto* that the analysis had to give a value.

### Placement
Every tensor is placed from the preset's flags: `--override-tensor` rules, `--cpu-moe`, `--n-cpu-moe`, `--n-gpu-layers`, and llama.cpp's rule that input embeddings stay on the host (checked with `llama-fit-params` on presets without `-ot`, on two architectures). The Tensors page shows a tag where a flag or that rule put a tensor on the CPU. The `--n-gpu-layers` rule for partial offload is not exercised by the current presets, which offload everything; it is covered by the same check against `llama-fit-params`.

### Host RAM, system RAM and page file
- **llama-server's address space** is walked every 5 s (about 150 ms, off the display thread). The page shows what the walk found: private memory, memory-mapped files (named), images, reserved memory, and the difference between private commit and GPU resident plus shared memory. It does not say what that difference is used for. The configured `cache-ram` and checkpoint count are shown as configured; the log does not give their current size.
- **System RAM** is split into llama-server's working set, other processes' working sets, what is in use but in no working set (a remainder, which includes shared pages counted once), file cache (standby) and free.
- **Page file**: size, use and peak per file, the configured initial and maximum size, the commit limit now, and the commit limit if the page file reaches its configured maximum.

### Shared GPU memory
Windows reports, per process, *dedicated* memory (resident in VRAM), *shared* memory (resident in system RAM) and *committed* memory. llmtop reports all three and alerts on thresholds. The Live row (`llama-server shared`) reports the GPU memory backed by system RAM that `llama-server` holds, its expected share, and what is beyond it (along with growth since the model became resident when known). It does not state why memory is shared; the counters cannot say which allocation or tensor is in shared memory.

Two baselines are shown against the shared figure, because neither alone settles what it is:

- **When the model became resident.** CPU-side buffers are allocated when the model loads, so the shared figure at that moment (lowest seen since) is what this process held before any request. Growth beyond it is what the alert reports. This is measured for the process, but it is unknown if llmtop first saw the process after it had already grown.
- **llama.cpp's projection of CPU-side buffers** (see `--no-host`), or, if llmtop has measured one, the lowest shared figure seen on an earlier load of the same configuration while resident plus shared memory was at least 1 GiB under the budget (so nothing could have been demoted). It is labelled unverified when projected.

A **demotion** is reported (critical) only when it is observed: shared memory rose and resident VRAM fell by at least half as much within the window. Growth of shared memory without that exchange is a warning; shared memory above a baseline, without that growth, is informational (headline: `shared X, Y beyond expected`). Neither is a conclusion. The OS budget is the DXGI figure for llmtop itself; Microsoft documents it as the amount of video memory the process can use before the video memory manager may demote its allocations.

### Alerts
Each rule holds while its condition is true and clears after 10 s of quiet. Every threshold is in `CFG` in `llmtop_lib/state.py` and can be set with `--threshold NAME=VALUE` (including `prompt_gap_s`, `30` s, for grouping turns into prompts). Six of them scale with the machine unless set: `avail_warn` = the smaller of 4 GiB and 7% of installed RAM, `avail_crit` = 2 GiB or 3.5%, `commit_warn` = 8 GiB or 10% of the commit limit at the page file's configured maximum, `commit_crit` = 3 GiB or 4% of it, `near_budget_mib` = 512 MiB or 3% of the VRAM budget, `idle_headroom_mib` = 1 GiB or 6% of it; the table shows those caps. Messages state the counter values; none states a cause.

| Alert | Level | Condition (threshold name) |
| :--- | :--- | :--- |
| shared GPU memory | info / warn | above the CPU-side baseline by ≥ `spill_mib` (256 MiB): info; grown by ≥ `spill_mib` above the figure it held when the model became resident: warn; headline reports amount beyond expected and detail states both comparisons and how near resident VRAM is to the OS budget (`near_budget_mib`, 512 MiB) |
| memory moved to shared | crit | shared memory rose by ≥ `burst_mib` (128 MiB) within `burst_window_s` (4 s), more than `load_grace_s` (90 s) after the model loaded, and resident VRAM fell by at least half as much; held `hold_s` (30 s) |
| shared memory rose | warn | the same rise without a matching fall in resident VRAM |
| shared memory fell | info | shared memory fell by ≥ `burst_mib` within the same window |
| committed, not resident | info / crit | committed minus resident minus shared ≥ `spill_mib`; info within `load_grace_s` (90 s) of the load, crit after |
| near the OS budget | warn | resident VRAM within `at_budget_mib` (64 MiB) of the budget, no excess |
| GPU used by another process | warn | another process uses ≥ `rival_gpu_pct` (30%) of the GPU while a request runs |
| GPU memory used | warn | adapter dedicated memory ≥ `card_full_pct` (97%) of capacity |
| available RAM | warn / crit | below `avail_warn` (4 GiB) / `avail_crit` (2 GiB) |
| commit headroom | info / warn / crit | against the current limit below `commit_warn` (8 GiB): info; against the limit at the page file's configured maximum below `commit_warn` / `commit_crit` (3 GiB) |
| page file use | warn | a page file is ≥ `pagefile_pct` (50%) used |
| working set fell | warn / info | llama-server working set fell by ≥ `ws_drop_mib` (1 GiB) within 10 s; info when commit fell with it, warn when commit stayed |
| router / exit | crit | router unreachable; a model process exited non-zero |

### History
Rings hold one value per second for 24 h. They are mirrored to the state folder's `samples-<date>.jsonl` (one sample per 5 s plus alert events, 14 days kept) and reloaded at start, so sparklines and the event list span restarts. Finished requests are kept in `requests.jsonl` (llama-server rewrites a preset's log file each time that model loads, so the logs alone lose them); model events are rebuilt from the logs each time; wall-clock times of old sessions come from the router log's file name plus its `spawning server instance` lines. `--no-persist` turns writing off; read-only runs still replay what exists.

## EXIT STATUS
In interactive mode, `--once`, and `--plan`, `win-llmtop` exits 0 on normal termination.

It exits `1` with a message, before doing anything else, when it can't run: not Windows, 32-bit Python, Python older than 3.11, or an unknown or non-numeric `--threshold`.

With `--check`, `win-llmtop` exits `0` when the server is reachable and either GPU telemetry is present or the machine has no GPU adapters (CPU-only), and exits `1` otherwise (the server is unreachable or requires an API key, or GPU adapters are present but PDH counters are missing).

## FILES
| Path | Purpose |
| :--- | :--- |
| `llmtop.py` | Entry point, key handling, `--once`, `--plan` |
| `llmtop_lib/win.py` | PDH counters, DXGI budget, process and system memory, address-space walk, page files (ctypes) |
| `llmtop_lib/model.py` | GGUF reader, placement and its reasons, KV and state estimates, tensor roster, `llama-fit-params` wrapper |
| `llmtop_lib/log.py`, `prompts.py` | Request assembler, log tailing, router events, session start times, and prompt grouping |
| `llmtop_lib/state.py` | Collector, time series, persistence, alert rules |
| `llmtop_lib/ui.py`, `pages.py` | Drawing primitives and the six pages |
| `%LOCALAPPDATA%\win-llmtop\` (the state folder, `--state-dir`) | `samples-*.jsonl`, `requests.jsonl`, `calibration.json` |

## LIMITS
- Which allocation or tensor is in shared GPU memory cannot be told from the counters; only how much.
- The OS budget comes from DXGI for llmtop's own process. llama-server's budget may differ.
- GPU temperature and power are not exposed by Windows' counters and are not shown.
- CPU cache hit and miss rates are not shown because Windows provides them only through hardware performance counters that need administrator rights.
- The address-space walk shows what kind of memory the process holds, not which allocation is the prompt cache or a checkpoint: the llama-server log carries no cache sizes at its default verbosity, so no checkpoint or cache size is shown, only the configured limits.
- Prefill progress advances once per batch (`--batch-size`), so the live prefill rate is the rate over the last completed batch.
- KV and state figures are derived. They were checked against `llama-fit-params` on a hybrid model and a sliding-window mixture-of-experts model; on any architecture the Memory page reports whether they agree.
- Fixed assumptions: llama.cpp's tensor naming (`attn_`, `ffn_`, `ssm_`, `_exps`, `nextn`), its offload rules, the GGUF type sizes, and the log line formats of build b11669, matched with or without timestamps. Alert thresholds, the colour thresholds for t/s (80% and 50%) and the size buckets of the Requests page are display or policy choices, not measurements.

## SEE ALSO
- [Documentation index (`README.md`)](README.md)
- [Installation guide (`install.md`)](install.md)
- [Usage guide (`usage.md`)](usage.md)
- [Troubleshooting (`troubleshooting.md`)](troubleshooting.md)
- [llama.cpp's server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
