#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live terminal panel for llama.cpp's llama-server on Windows: what's loaded, what it's doing, how fast, and where the memory goes.

	win-llmtop                    live view, [ ] change the history window, q quits
	win-llmtop --once             one snapshot of the live page and exit
	win-llmtop --once --page all  every page
	win-llmtop --plan             project each preset's VRAM against the OS budget (loads nothing)
	win-llmtop --plan <preset>    the full memory map of one preset

Pages:
	1 Live      GPU and RAM with trend charts, the running request, recent requests and events
	2 Memory    VRAM by component against the OS budget, llama-server's own RAM, layer overview
	3 System    system RAM by consumer, page file, RAM trends
	4 Requests  per-preset volume and speed, prefill by prompt size, decode by context depth, every request
	5 Events    alerts, model loads, router evictions, slot picks, errors
	6 Tensors   every layer or tensor with its device (t layers/tensors, d device filter)
Keys: 1-6 or left/right switch pages, [ ] the time window, g graph height, s graph style, p pause, arrows and PgUp/PgDn scroll, q quits.

Sources:
	/models, /slots?model=<preset>   router API: loaded preset and its flags, context in use, prompt progress, tokens decoded (live)
	<preset>.log                     per-request timings from the model process; the router log has loads, queueing and evictions
	Windows performance counters     dedicated and shared (system RAM) VRAM per process, GPU engine load, paging; DXGI gives the OS VRAM budget
	GGUF header + llama-fit-params   the model's tensors and llama.cpp's own memory projection

History is kept for 24 h in memory and mirrored to disk so it survives restarts (--no-persist to disable).
"""

import argparse
import os
import platform
import shutil
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llmtop_lib import model, pages, state, win  # noqa: E402
from llmtop_lib import ui  # noqa: E402
from llmtop_lib.ui import C  # noqa: E402

WINDOWS = {label: i for i, (_, label) in enumerate(pages.View.WINDOWS)}
PAGE_NAMES = [n.lower() for n in pages.View.PAGES]


def read_key():
	"""One key press as a name, or None. Windows console only."""
	import msvcrt

	if not msvcrt.kbhit():
		return None
	ch = msvcrt.getwch()
	if ch in ("\x00", "\xe0"):
		return {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn", "G": "home", "O": "end", "K": "left", "M": "right"}.get(msvcrt.getwch())
	key = {"\x03": "q", "\x1b": "q", "\t": "tab"}.get(ch, ch)
	return key.lower() if len(key) == 1 else key


def handle(key, c, view, snap, H):
	page = pages.View.PAGES[view.page]
	n = len(pages.View.PAGES)
	if key in [str(i + 1) for i in range(n)]:
		view.page, view.scroll = int(key) - 1, 0
	elif key in ("tab", "right"):
		view.page, view.scroll = (view.page + 1) % n, 0
	elif key == "left":
		view.page, view.scroll = (view.page - 1) % n, 0
	elif key in ("[", "-", "_"):
		view.win = max(0, view.win - 1)
	elif key in ("]", "=", "+"):
		view.win = min(len(pages.View.WINDOWS) - 1, view.win + 1)
	elif key in ("p", " "):
		view.paused = not view.paused
	elif key == "s":
		ui.STYLE["mode"] = "blocks" if ui.STYLE["mode"] == "braille" else "braille"
		view.msg = "graph style: %s" % ui.STYLE["mode"]
	elif key == "g":
		view.gh = (view.gh + 1) % 5
		view.msg = "graph height: %s" % ("auto" if view.gh == 0 else "%d row%s" % (view.gh, "s" if view.gh > 1 else ""))
	elif key == "m" and page in ("Memory", "Tensors"):
		view.msel, view.scroll = view.msel + 1, 0
	elif key == "m" and page == "Requests":
		view.rsel, view.scroll = (view.rsel + 1) % (len({r["preset"] for r in c.requests}) + 1), 0
	elif key == "t" and page == "Requests":
		view.turns = not view.turns
	elif key == "t" and page == "Live":
		view.live_prompts = not view.live_prompts
	elif key == "f" and page == "Events":
		view.level, view.scroll = (view.level + 1) % 3, 0
	elif key == "t" and page == "Tensors":
		view.tmode, view.scroll = (view.tmode + 1) % 2, 0
	elif key == "d" and page == "Tensors":
		view.dev, view.scroll = (view.dev + 1) % 3, 0
	elif key == "up":
		view.scroll = max(0, view.scroll - 1)
	elif key == "down":
		view.scroll += 1
	elif key == "pgup":
		view.scroll = max(0, view.scroll - max(1, H - 6))
	elif key == "pgdn":
		view.scroll += max(1, H - 6)
	elif key == "home":
		view.scroll = 0
	elif key == "end":
		view.scroll = 10**9


def frame_text(c, snap, view, W, H):
	if snap is None:
		return ["llmtop: collector error (press q to quit)", view.msg or "collector error"]
	try:
		return pages.render(c, snap, view, W, H)
	except Exception:
		tb = traceback.format_exc().splitlines()
		return ["llmtop: render error (press q to quit)"] + tb[-(H - 2):]


def live(c, view):
	"""Full-screen loop. Redraws in place (cursor home) so there is no flicker."""
	out = sys.stdout
	out.write("\x1b[?1049h\x1b[?25l")
	snap, frozen, prev, last_size = None, None, None, None
	try:
		while True:
			t0 = time.time()
			err = None
			try:
				snap = c.tick(t0)
			except Exception as e:
				err = traceback.format_exc()
				view.msg = "collector error: %s" % e
			size = shutil.get_terminal_size((120, 40))
			if (size.columns, size.lines) != last_size:  # a resize reflows what is on screen: start from a clean one
				out.write("\x1b[2J")
				prev, last_size = None, (size.columns, size.lines)
			if snap is None:
				err_lines = err.splitlines() if err else ([view.msg] if view.msg else ["unknown error"])
				lines = ["llmtop: collector error (press q to quit)"] + err_lines[-(size.lines - 2):]
			else:
				if view.paused and frozen is None:
					frozen = snap
				elif not view.paused:
					frozen = None
				lines = frame_text(c, frozen or snap, view, size.columns, size.lines)
			text = "\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J"
			if text != prev:
				out.write(text)
				out.flush()
				prev = text
			deadline = t0 + 1.0 / c.cfg["hz"]
			while time.time() < deadline:
				key = read_key()
				if key == "q":
					return
				if key:
					handle(key, c, view, snap, size.lines)
					prev = None
					break
				time.sleep(0.01)
	finally:
		out.write("\x1b[?25h\x1b[?1049l")
		out.flush()


def snapshot(c, view, which, width):
	"""Warm the collector up (rates need two samples) and print pages once."""
	snap = None
	for _ in range(4):
		snap = c.tick()
		time.sleep(0.3)
	deadline = time.time() + 5
	while any(e.get("state") == "pending" for e in c.mem.values()) and time.time() < deadline:
		time.sleep(0.1)
	snap = c.tick()
	for i, name in enumerate(PAGE_NAMES):
		if which in (name, "all"):
			view.page = i
			if which == "all":
				print("\n" + "=" * width)
			print("\n".join(frame_text(c, snap, view, width, 200 if which == "all" or name != "live" else 60)).rstrip())


def plan(c, view, which, width):
	"""Memory projection of presets against the OS VRAM budget, without loading anything."""
	snap = c.tick()
	g = snap["gpu"]
	names = [which] if which in c.flags else list(c.flags)
	for name in names:
		c.ensure_map(name)
	deadline = time.time() + 120
	while any(c.mem.get(n, {}).get("state") == "pending" for n in names) and time.time() < deadline:
		time.sleep(0.1)
	if which in c.flags:
		view.page = 1
		loaded = [n for n, p in snap.get("presets", {}).items() if p.get("status") == "loaded"]
		order = loaded + [n for n in c.flags if n not in loaded]
		view.msel = order.index(which) if which in order else 0
		print("\n".join(frame_text(c, snap, view, width, 200)).rstrip())
		return
	print("%s: %s MiB, OS budget %s MiB" % (g["name"], "{:,.0f}".format(g["vram"]), "{:,.0f}".format(g["budget"])) if g else "no GPU telemetry")
	print("\n%-26s %9s %9s %9s %9s %9s %10s  %s" % ("preset", "weights", "KV+state", "compute", "draft", "overhead", "projected", "vs OS budget"))
	for name in names:
		e = c.mem.get(name, {})
		if e.get("state") != "ready":
			print("%-26s %s" % (name, e.get("error", "no projection")))
			continue
		cal = c.calib.get(c.calib_key(e["key"]))
		rows = model.memory_rows(e["map"], None, cal["overhead"] if cal else None)
		by = lambda pred, rows=rows: sum(r[1] for r in rows if pred(r[0]))
		total = sum(r[1] for r in rows)
		head = g["budget"] - total if g else float("nan")
		print("%-26s %9.0f %9.0f %9.0f %9.0f %9s %10.0f  %+.0f MiB%s%s" % (name, by(lambda n: n.startswith("weights")), by(lambda n: n.startswith(("KV", "recurrent"))), by(lambda n: n == "compute buffers"),
			by(lambda n: n.startswith(("MTP", "draft"))), "%.0f" % cal["overhead"] if cal else "?", total, head, "  over the budget" if head < 0 else "", "" if cal else "  (not calibrated)"))
	print("\nFigures are MiB. 'overhead' is resident VRAM minus the projected rows, measured on an earlier load of the same model file, context size and cache types (shared by every preset with that configuration).")
	print("'?' means no such load has been measured yet, so the projection has no overhead term. 'vs OS budget' compares the projected resident VRAM with the DXGI budget; shared GPU memory is not part of it.")


def check(c, args):
	"""One-shot diagnostic report on prerequisites, reachable sources, and exit code."""
	snap, tick_error = None, None
	try:
		c.load_history()  # opens and reads the logs, as a normal run does; writes nothing with persistence off
		for _ in range(3):  # like --once: log tails and rates need more than one sample
			snap = c.tick()
			time.sleep(0.3)
	except Exception as e:
		tick_error = "%s: %s" % (type(e).__name__, e)
	if snap is None:
		snap = {"presets": {}, "notices": [], "gpu": None, "llama": None}

	# 1. Windows build, Python version and bitness
	if win.WIN:
		wv = sys.getwindowsversion()
		win_ver = "Windows %d.%d.%d" % (wv.major, wv.minor, wv.build)
	else:
		win_ver = sys.platform
	py_ver = "Python %s %s" % (platform.python_version(), "64-bit" if win.IS_64 else "32-bit")
	win_ok = win.WIN and win.IS_64
	print("%-18s %-5s %s" % ("windows", "ok" if win_ok else "FAIL", "%s, %s" % (win_ver, py_ver)))
	if tick_error:
		print("%-18s %-5s %s" % ("collector", "FAIL", tick_error))

	# 2. GPU: DXGI adapters, watched adapter, PDH GPU counters
	dx = c.dx
	adapters = dx.adapters if dx else []
	pd = c.counters.sample() if c.counters else {}
	pdh_procs = pd.get("procs") or {}
	pdh_adapters = pd.get("adapters") or {}
	pdh_ok = bool(pdh_procs or pdh_adapters)
	watched = c.gpu
	gpu_telemetry_ok = bool(win.WIN and adapters and watched and pdh_ok)
	if not win.WIN:
		gpu_status, gpu_fact = "FAIL", "Windows only"
	elif not adapters:
		gpu_status, gpu_fact = "--", "no GPU adapters (CPU-only)"
	else:
		ad_strs = ["%s (%.0f MiB, budget %.0f MiB)" % (a["name"], a["vram"], a["budget"]) for a in adapters]
		watch_str = "watching %s" % watched["name"] if watched else "no adapter selected"
		pdh_str = "PDH counters present" if pdh_ok else "PDH counters absent"
		gpu_fact = "%s, %s; %s" % ("; ".join(ad_strs), watch_str, pdh_str)
		gpu_status = "ok" if gpu_telemetry_ok else "FAIL"
	print("%-18s %-5s %s" % ("gpu", gpu_status, gpu_fact))

	# 3. llama-server processes (pid, exe from proc_info, or the reason it couldn't be read)
	if not win.WIN:
		proc_status, proc_fact = "--", "Windows only"
	elif not c.pids:
		proc_status, proc_fact = "--", "none running"
	else:
		proc_status = "ok"
		items = []
		for pid in c.pids:
			info = win.proc_info(pid)
			exe = info.get("exe")
			if exe:
				items.append("pid %d: %s" % (pid, exe))
			else:
				err = win.errors.get(("proc_info", pid))
				reason = err[1] if err else "cannot read process info"
				items.append("pid %d: %s" % (pid, reason))
		proc_fact = "; ".join(items)
	print("%-18s %-5s %s" % ("processes", proc_status, proc_fact))

	# 4. the server: URL used and how it was found, mode, models reported (count), /slots reachable for a loaded model (or its slot_error)
	server_ok = bool(c.router_up)
	if server_ok:
		server_status = "ok"
		url = c.cfg.get("router") or ""
		how = "explicit" if c.router_explicit else (c.router_discovery or "discovered")
		mode_str = "%s mode" % (c.mode or "unknown")
		m_count = len(c.models)
		count_str = "%d model%s reported" % (m_count, "" if m_count == 1 else "s")
		loaded = [n for n, p in snap["presets"].items() if p.get("status") == "loaded"]
		if loaded:
			slot_errs = [p.get("slot_error") for n, p in snap["presets"].items() if p.get("status") == "loaded" and p.get("slot_error")]
			if slot_errs:
				slot_str = "/slots error: %s" % ", ".join(slot_errs)
			else:
				slot_str = "/slots reachable"
		else:
			slot_str = "/slots not queried (no loaded models)"
		server_fact = "%s (%s), %s, %s, %s" % (url, how, mode_str, count_str, slot_str)
	elif getattr(c, "router_auth_error", None):
		server_status = "FAIL"
		url = c.cfg.get("router") or ""
		how = "explicit" if c.router_explicit else (c.router_discovery or "discovered")
		server_fact = "%s (%s): %s (pass --api-key or set LLMTOP_API_KEY / LLAMA_API_KEY)" % (url, how, c.router_auth_error)
	else:
		server_status = "FAIL"
		url = c.cfg.get("router") or ""
		tried = ", ".join(c.router_tried) if c.router_tried else url
		err = (": " + c.router_error) if c.router_error else ""
		server_fact = "server unreachable (tried %s)%s" % (tried, err)
	print("%-18s %-5s %s" % ("server", server_status, server_fact))

	# 5. log files: per model, the path and whether it exists and has timestamps; the router log
	log_items = []
	for name in c.models:
		path = c.preset_log_path(name)
		if path:
			exists = os.path.isfile(path)
			has_ts = c.follow[name][1].timestamps if name in c.follow else False
			ts_str = ", timestamps" if has_ts else ", no timestamps"
			log_items.append("%s: %s (%s%s)" % (name, path, "exists" if exists else "absent", ts_str if exists else ""))
		else:
			log_items.append("%s: no log path" % name)
	rl_path = c.cfg.get("router_log") or (c.router_tail.path if c.router_tail else None)
	if rl_path:
		log_items.append("router log: %s (%s)" % (rl_path, "exists" if os.path.isfile(rl_path) else "absent"))
	elif c.mode == "router":
		log_items.append("router log: none found (model loads and evictions need it: pass --router-log or --log-dir)")
	if not log_items:
		log_status, log_fact = "--", "no log files configured or found"
	else:
		log_fact = "; ".join(log_items)
		log_status = "ok" if any("exists" in item for item in log_items) else "--"
	print("%-18s %-5s %s" % ("logs", log_status, log_fact))

	# 6. llama-fit-params: found next to the server, on PATH, or not found
	fit_found = None
	fit_where = None
	candidates = []
	for pid in c.pids:
		info = win.proc_info(pid) if win.WIN else {}
		exe = info.get("exe")
		if exe:
			d = os.path.dirname(exe)
			candidates.extend([os.path.join(d, "llama-fit-params.exe"), os.path.join(d, "llama-fit-params")])
	for _, exe in c.flags.values():
		if exe:
			d = os.path.dirname(exe)
			candidates.extend([os.path.join(d, "llama-fit-params.exe"), os.path.join(d, "llama-fit-params")])
	for cand in candidates:
		if os.path.isfile(cand):
			fit_found = cand
			fit_where = "found next to server"
			break
	if not fit_found:
		w = shutil.which("llama-fit-params.exe") or shutil.which("llama-fit-params")
		if w:
			fit_found = w
			fit_where = "found on PATH"
	if fit_found:
		fit_status = "ok"
		fit_fact = "%s (%s)" % (fit_where, fit_found)
	else:
		fit_status = "--"
		fit_fact = "not found"
	print("%-18s %-5s %s" % ("llama-fit-params", fit_status, fit_fact))

	# 7. the state folder: path and whether it is writable (check without creating it when --no-persist)
	state_dir = c.state_dir
	if args.no_persist:
		if os.path.isdir(state_dir):
			try:
				tf = os.path.join(state_dir, ".test_write_%d" % os.getpid())
				with open(tf, "w") as f:
					f.write("")
				os.remove(tf)
				writable = True
			except Exception:
				writable = False
		else:
			p = os.path.abspath(state_dir)
			while p and not os.path.exists(p):
				prev = p
				p = os.path.dirname(p)
				if p == prev:
					break
			try:
				tf = os.path.join(p, ".test_write_%d" % os.getpid())
				with open(tf, "w") as f:
					f.write("")
				os.remove(tf)
				writable = True
			except Exception:
				writable = False
		if writable:
			state_status, state_fact = "ok", "%s (writable)" % state_dir
		else:
			state_status, state_fact = "FAIL", "%s (not writable)" % state_dir
	else:
		try:
			os.makedirs(state_dir, exist_ok=True)
			tf = os.path.join(state_dir, ".test_write_%d" % os.getpid())
			with open(tf, "w") as f:
				f.write("")
			os.remove(tf)
			state_status, state_fact = "ok", "%s (writable)" % state_dir
		except Exception as e:
			state_status, state_fact = "FAIL", "%s (%s)" % (state_dir, e)
	print("%-18s %-5s %s" % ("state", state_status, state_fact))

	# 8. then every snap["notices"] line
	for n in snap.get("notices", []):
		print("%-18s %-5s %s" % ("notice", "--", n))

	gpu_ok_or_cpu = bool(win.WIN and (gpu_telemetry_ok or not adapters))
	return 0 if (server_ok and gpu_ok_or_cpu) else 1


def main():
	if sys.version_info < (3, 11):
		sys.exit("win-llmtop needs Python 3.11 or later (this is %d.%d)" % sys.version_info[:2])
	ap = argparse.ArgumentParser(prog="win-llmtop", description="Live terminal panel for llama.cpp's llama-server on Windows: GPU and shared memory, RAM, requests.")
	ap.add_argument("--check", action="store_true", help="report on prerequisites and reachable sources, then exit")
	ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
	ap.add_argument("--page", choices=PAGE_NAMES + ["all"], default="live", help="page for --once")
	ap.add_argument("--plan", nargs="?", const="all", metavar="PRESET", help="project VRAM per preset against the OS budget")
	ap.add_argument("--window", choices=list(WINDOWS), default="5m", help="initial history window")
	ap.add_argument("--router", default=os.environ.get("LLMTOP_ROUTER") or None, metavar="URL", help="llama-server URL (default: find it; env LLMTOP_ROUTER)")
	ap.add_argument("--log-dir", default=os.environ.get("LLMTOP_LOG_DIR") or None, metavar="DIR", help="directory of llama-server logs (env LLMTOP_LOG_DIR)")
	ap.add_argument("--router-log", default=None, metavar="PATH", help="router log file path")
	ap.add_argument("--state-dir", default=os.environ.get("LLMTOP_STATE_DIR") or state.CFG["state_dir"], metavar="DIR", help="directory for persisted history and calibration (env LLMTOP_STATE_DIR)")
	ap.add_argument("--api-key", default=os.environ.get("LLMTOP_API_KEY") or os.environ.get("LLAMA_API_KEY") or None, metavar="KEY", help="Bearer token for llama-server (env LLMTOP_API_KEY or LLAMA_API_KEY)")
	ap.add_argument("--gpu", default=None, metavar="INDEX|NAME", help="adapter index or name substring")
	ap.add_argument("--hz", type=float, default=state.CFG["hz"], help="sampling and redraw rate")
	ap.add_argument("--history-hours", type=int, default=state.CFG["history_hours"])
	ap.add_argument("--spill-mib", type=int, default=state.CFG["spill_mib"], help="excess shared GPU memory (MiB) that raises an alert")
	ap.add_argument("--threshold", action="append", default=[], metavar="NAME=VALUE", help="override an alert threshold (see CFG in llmtop_lib/state.py); repeatable")
	ap.add_argument("--no-persist", action="store_true", help="do not mirror history to disk")
	ap.add_argument("--graph-style", choices=("braille", "blocks"), default=None, help="braille line plots, or block-character charts if the font lacks braille")
	ap.add_argument("--no-color", action="store_true")
	ap.add_argument("--width", type=int, default=0, help="line width for --once and --plan")
	args = ap.parse_args()
	if not win.WIN:
		sys.exit("llmtop reads Windows performance counters and runs on Windows only.")
	if not win.IS_64:
		sys.exit("win-llmtop needs 64-bit Python (32-bit Python cannot read a 64-bit llama-server's memory)")
	if args.graph_style is None:
		args.graph_style = "braille" if "WT_SESSION" in os.environ else "blocks"
	ui.STYLE["mode"] = args.graph_style
	vt_ok = win.enable_vt()
	for stream in (sys.stdout, sys.stderr):
		try:
			stream.reconfigure(encoding="utf-8", errors="replace")
		except (AttributeError, ValueError):
			pass
	interactive = sys.stdout.isatty() and not args.once and not args.check and args.plan is None
	if not vt_ok and sys.stdout.isatty():
		C.enable(False)
	else:
		C.enable(not args.no_color and sys.stdout.isatty())
	user_thresholds = set()
	cfg = dict(state.CFG, router=args.router, log_dir=args.log_dir, router_log=args.router_log, state_dir=args.state_dir,
		api_key=args.api_key, gpu=args.gpu, hz=args.hz, history_hours=args.history_hours, spill_mib=args.spill_mib,
		persist=not (args.no_persist or args.once or args.plan is not None or args.check),
		calibrate=not (args.no_persist or args.plan is not None or args.check),
		user_thresholds=user_thresholds)
	num_thresholds = {k: v for k, v in state.CFG.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
	pagein_warn_noted = False
	for item in args.threshold:
		name, _, value = item.partition("=")
		if name == "pagein_warn":
			if not pagein_warn_noted:
				print("win-llmtop: threshold pagein_warn was removed in 0.2.0 and is ignored", file=sys.stderr)
				pagein_warn_noted = True
			continue
		if name not in num_thresholds or not value:
			sys.exit("unknown or non-numeric threshold %r; thresholds: %s" % (item, ", ".join(num_thresholds)))
		cfg[name] = type(num_thresholds[name])(float(value))
		user_thresholds.add(name)
	c = state.Collector(cfg)
	if not args.check:
		c.load_history()
	view = pages.View(WINDOWS[args.window])
	width = args.width or shutil.get_terminal_size((140, 40)).columns
	try:
		if args.check:
			rc = check(c, args)
			sys.exit(rc)
		elif args.plan is not None:
			plan(c, view, args.plan, width)
		elif interactive:
			live(c, view)
		else:
			snapshot(c, view, args.page, width)
	except KeyboardInterrupt:
		pass
	finally:
		c.close()


if __name__ == "__main__":
	main()
